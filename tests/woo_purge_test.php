<?php
/**
 * Read-only test of extras/woo-purge on an installed site. Run through ./test.sh woo <site>.
 *
 * Fires the same hook the WooCommerce data store fires after a save and checks which URLs
 * reach Nginx Helper's purger. Every outgoing HTTP request is short-circuited and the unlink
 * method is pointed at a non-existent file, so nothing is purged; no product, order or option
 * is written.
 *
 * Output: lines starting with "@@" (the runner strips everything else that wp-cli prints).
 */

$GLOBALS['gpwt'] = array( 'pass' => 0, 'fail' => 0, 'http' => array(), 'sent' => array() );

function gpwt_out( $s ) {
	echo '@@' . $s . "\n";
}
function gpwt( $name, $ok, $extra = '' ) {
	$GLOBALS['gpwt'][ $ok ? 'pass' : 'fail' ]++;
	gpwt_out( '  ' . ( $ok ? 'PASS' : 'FAIL' ) . ' ' . $name . ( $ok || '' === $extra ? '' : "\n@@       " . $extra ) );
}
function gpwt_end() {
	gpwt_out( '' );
	gpwt_out( 'RESULT pass=' . $GLOBALS['gpwt']['pass'] . ' fail=' . $GLOBALS['gpwt']['fail'] . ' http_blocked=' . count( $GLOBALS['gpwt']['http'] ) );
	exit( 0 );
}
function gpwt_rel( $urls ) {
	return implode( ' ', array_map( 'wp_make_link_relative', $urls ) );
}

global $nginx_purger, $nginx_helper_admin;

gpwt_out( 'Setup' );
gpwt( 'mu-plugin loaded (GP_Woo_Purge ' . ( defined( 'GP_Woo_Purge::VERSION' ) ? GP_Woo_Purge::VERSION : '-' ) . ')', class_exists( 'GP_Woo_Purge' ) );
gpwt( 'WooCommerce active', class_exists( 'WooCommerce' ) );
gpwt( 'Nginx Helper active with purge enabled', is_object( $nginx_purger ) && ! empty( $nginx_helper_admin->options['enable_purge'] ) );
$method = $nginx_helper_admin->options['cache_method'] ?? '';
gpwt( 'cache method is FastCGI (the only one this test can neutralise)', 'enable_fastcgi' === $method, 'cache_method=' . $method );
if ( $GLOBALS['gpwt']['fail'] ) {
	gpwt_end();
}

// Neutralise every purge: block all HTTP (GET /purge/...) and redirect unlink to a missing file.
add_filter( 'pre_http_request', static function ( $pre, $args, $url ) {
	$GLOBALS['gpwt']['http'][] = $url;
	return array( 'headers' => array(), 'body' => '', 'response' => array( 'code' => 412, 'message' => 'blocked by test' ), 'cookies' => array(), 'filename' => null );
}, PHP_INT_MAX, 3 );
add_filter( 'rt_nginx_helper_purge_cached_file', static function () {
	return '/nonexistent/gp-woo-purge-test';
}, PHP_INT_MAX );
add_filter( 'rt_nginx_helper_purge_url', static function ( $url ) {
	$GLOBALS['gpwt']['sent'][] = $url;
	return $url;
}, PHP_INT_MAX );

function gpwt_fire( $product, $props ) {
	$GLOBALS['gpwt']['sent'] = array();
	do_action( 'woocommerce_product_object_updated_props', $product, $props );
	return GP_Woo_Purge::flush();
}

$home_on = GP_Woo_Purge::purge_home();
$home    = trailingslashit( home_url() );
// The product archive is listed even without an assigned shop page (then it is /shop/).
$shop = (string) get_post_type_archive_link( 'product' );
gpwt_out( '  shop / product archive: ' . ( $shop ? wp_make_link_relative( $shop ) : '-' ) . ( wc_get_page_id( 'shop' ) > 0 ? '' : ' (no shop page assigned)' ) );
gpwt_out( '  home page purge: ' . ( $home_on ? 'on' : 'off (option gp_woo_purge_home = no)' ) );

gpwt_out( '' );
gpwt_out( 'Nginx Helper trimmed (in memory, this request)' );
gpwt( 'feed purges off (GridPane never caches /feed/)', 0 === ( $nginx_helper_admin->options['purge_feeds'] ?? null ), 'purge_feeds=' . var_export( $nginx_helper_admin->options['purge_feeds'] ?? null, true ) );
$amp_plugin = defined( 'AMP__VERSION' ) || defined( 'AMPFORWP_VERSION' ) || function_exists( 'amp_is_request' ) || function_exists( 'is_amp_endpoint' );
if ( $amp_plugin ) {
	gpwt_out( '  SKIP an AMP plugin is active: AMP purges stay on' );
} else {
	gpwt( 'AMP "purges" off (no AMP plugin; GridPane\'s fork fetches the /amp/ page instead of purging it)', 0 === ( $nginx_helper_admin->options['purge_amp_urls'] ?? null ), 'purge_amp_urls=' . var_export( $nginx_helper_admin->options['purge_amp_urls'] ?? null, true ) );
}
foreach ( array( 'edit_term', 'delete_term' ) as $hook ) {
	gpwt(
		"$hook: Nginx Helper's handler replaced by the add-on's",
		false === has_action( $hook, array( $nginx_purger, 'purge_on_term_taxonomy_edited' ) ) && false !== has_action( $hook, array( 'GP_Woo_Purge', 'on_term_changed' ) )
	);
}
// One real call into Nginx Helper's purger, as on a page view (is_page() is what enables the AMP path).
$GLOBALS['gpwt']['http'] = array();
$was_page                 = $GLOBALS['wp_query']->is_page;
$GLOBALS['wp_query']->is_page = true;
$nginx_purger->purge_url( $home );   // Nginx Helper's default: $feed = true
$GLOBALS['wp_query']->is_page = $was_page;
gpwt( "one Nginx Helper purge_url() = 1 request (no feed, no /amp/ fetch)", 1 === count( $GLOBALS['gpwt']['http'] ), implode( ' ', array_map( 'wp_make_link_relative', $GLOBALS['gpwt']['http'] ) ) );

$GLOBALS['gpwt']['sent'] = array();
for ( $i = 0; $i < 176; $i++ ) {
	GP_Woo_Purge::on_term_changed();   // what 176 edit_term calls do (called directly: no other handlers run)
}
$u = GP_Woo_Purge::flush();
gpwt( '176 term edits in one request -> ' . ( $home_on ? 'one home-page purge' : 'no purge (home page off)' ), $home_on ? array( $home ) === $u : ! $u, gpwt_rel( $u ) );

// A published simple product, preferably in a child category (to see the parent purged too).
$simple = null;
foreach ( wc_get_products( array( 'status' => 'publish', 'type' => 'simple', 'limit' => 50 ) ) as $p ) {
	$simple = $simple ?: $p;
	foreach ( (array) get_the_terms( $p->get_id(), 'product_cat' ) as $t ) {
		if ( $t && ! empty( $t->parent ) ) {
			$simple = $p;
			break 2;
		}
	}
}
$variation = null;
foreach ( wc_get_products( array( 'status' => 'publish', 'type' => 'variable', 'limit' => 20 ) ) as $p ) {
	$kids = $p->get_children();
	if ( $kids ) {
		$variation = wc_get_product( $kids[0] );
		break;
	}
}

gpwt_out( '' );
gpwt_out( 'Triggers (simple product #' . ( $simple ? $simple->get_id() . ' ' . wp_make_link_relative( get_permalink( $simple->get_id() ) ) : '-' ) . ')' );
if ( ! $simple ) {
	gpwt( 'a published simple product exists', false );
	gpwt_end();
}

$u = gpwt_fire( $simple, array( 'stock_quantity' ) );
gpwt( 'stock quantity change alone -> no purge', ! $u && ! $GLOBALS['gpwt']['sent'], gpwt_rel( $u ) );

$u = gpwt_fire( $simple, array( 'stock_quantity', 'stock_status' ) );
$want = array( get_permalink( $simple->get_id() ) );
if ( $home_on ) {
	$want[] = $home;
}
if ( $shop ) {
	$want[] = $shop;
}
$cats = array();
foreach ( (array) get_the_terms( $simple->get_id(), 'product_cat' ) as $t ) {
	if ( $t ) {
		$cats[] = get_term_link( $t );
		foreach ( get_ancestors( $t->term_id, 'product_cat', 'taxonomy' ) as $a ) {
			$cats[] = get_term_link( (int) $a, 'product_cat' );
		}
	}
}
$missing = array_diff( array_merge( $want, $cats ), $u );
gpwt( 'stock status change -> home, shop/product archive, product, its categories + parent categories', $u && ! $missing, 'missing: ' . gpwt_rel( $missing ) );
gpwt( 'every URL reached the Nginx Helper purger once', $u && $GLOBALS['gpwt']['sent'] === $u, 'sent: ' . count( $GLOBALS['gpwt']['sent'] ) . ' vs ' . count( $u ) );
gpwt( 'no URL carries a query string', $u && ! array_filter( $u, static function ( $x ) { return false !== strpos( $x, '?' ); } ) );
gpwt_out( '       ' . count( $u ) . ' URLs: ' . gpwt_rel( $u ) );

$u = gpwt_fire( $simple, array( 'regular_price' ) );
gpwt( 'regular price change -> purge', in_array( get_permalink( $simple->get_id() ), $u, true ) );
$u = gpwt_fire( $simple, array( 'date_on_sale_to', 'price' ) );
gpwt( 'scheduled sale end (price) -> purge', in_array( get_permalink( $simple->get_id() ), $u, true ) );
$u = gpwt_fire( $simple, array( 'name', 'description', 'total_sales', 'stock_quantity' ) );
gpwt( 'other props (name, description, total_sales) -> no purge from this plugin', ! $u, gpwt_rel( $u ) );

if ( $variation ) {
	$parent = get_permalink( $variation->get_parent_id() );
	$u      = gpwt_fire( $variation, array( 'sale_price' ) );
	gpwt( 'variation #' . $variation->get_id() . ' price change -> parent product ' . wp_make_link_relative( $parent ), in_array( $parent, $u, true ) );
	gpwt( 'variation URL itself is not purged', ! array_filter( $u, static function ( $x ) { return false !== strpos( $x, 'attribute_' ) || false !== strpos( $x, 'product_variation' ); } ) );
} else {
	gpwt_out( '  SKIP no published variable product' );
}

$GLOBALS['gpwt']['sent'] = array();
do_action( 'woocommerce_product_object_updated_props', $simple, array( 'stock_status' ) );
if ( $variation ) {
	do_action( 'woocommerce_product_object_updated_props', $variation, array( 'stock_status' ) );
}
do_action( 'woocommerce_product_object_updated_props', $simple, array( 'regular_price' ) );
$u = GP_Woo_Purge::flush();
gpwt( 'several saves in one request -> each URL purged once', $u && count( $u ) === count( array_unique( $GLOBALS['gpwt']['sent'] ) ) && count( $GLOBALS['gpwt']['sent'] ) === count( $u ) );

// The other value of the home-page setting, through its filter (the option is not touched).
$flip = static function () use ( $home_on ) {
	return ! $home_on;
};
add_filter( 'gp_woo_purge_home', $flip );
$u = gpwt_fire( $simple, array( 'stock_status' ) );
GP_Woo_Purge::on_term_changed();
$t = GP_Woo_Purge::flush();
remove_filter( 'gp_woo_purge_home', $flip );
gpwt(
	'home page ' . ( $home_on ? 'off' : 'on' ) . ' (filter) -> ' . ( $home_on ? 'not' : 'also' ) . ' purged for products and term edits',
	in_array( get_permalink( $simple->get_id() ), $u, true ) && ( $home_on ? ! in_array( $home, $u, true ) && ! $t : in_array( $home, $u, true ) && array( $home ) === $t ),
	'product: ' . gpwt_rel( $u ) . ' | terms: ' . gpwt_rel( $t )
);
gpwt( 'queue is empty after the flush', ! GP_Woo_Purge::flush() );

// Direct meta writes (update_post_meta, no WooCommerce save), e.g. a theme's stock-sync worker.
// The handler is called directly: firing updated_post_meta would run other plugins' handlers.
foreach ( array( 'added_post_meta', 'updated_post_meta' ) as $hook ) {
	gpwt( "$hook hooked (direct _stock_status / _price writes)", false !== has_action( $hook, array( 'GP_Woo_Purge', 'on_meta_changed' ) ) );
}
$GLOBALS['gpwt']['sent'] = array();
GP_Woo_Purge::on_meta_changed( 0, $simple->get_id(), '_stock_status' );
$u = GP_Woo_Purge::flush();
gpwt( 'direct _stock_status meta change -> product purged', in_array( get_permalink( $simple->get_id() ), $u, true ), gpwt_rel( $u ) );
GP_Woo_Purge::on_meta_changed( 0, $simple->get_id(), '_stock' );
GP_Woo_Purge::on_meta_changed( 0, $simple->get_id(), '_edit_lock' );
$u = GP_Woo_Purge::flush();
gpwt( 'direct _stock (quantity) or other meta change -> no purge', ! $u, gpwt_rel( $u ) );
if ( $variation ) {
	GP_Woo_Purge::on_meta_changed( 0, $variation->get_id(), '_price' );
	$u = GP_Woo_Purge::flush();
	gpwt( 'direct variation _price meta change -> parent product purged', in_array( get_permalink( $variation->get_parent_id() ), $u, true ), gpwt_rel( $u ) );
}
do_action( 'woocommerce_product_object_updated_props', $simple, array( 'stock_status' ) );
GP_Woo_Purge::on_meta_changed( 0, $simple->get_id(), '_stock_status' );   // a WooCommerce save fires both
$GLOBALS['gpwt']['sent'] = array();
$u = GP_Woo_Purge::flush();
gpwt( 'WooCommerce save + its meta write -> each URL once', $u && count( $u ) === count( array_unique( $GLOBALS['gpwt']['sent'] ) ) );

$draft = wc_get_products( array( 'status' => 'draft', 'limit' => 1 ) );
if ( $draft ) {
	$u = gpwt_fire( $draft[0], array( 'stock_status' ) );
	gpwt( 'draft product -> no purge', ! $u, gpwt_rel( $u ) );
}

gpwt_out( '' );
gpwt_out( 'Orders and coupons no longer purge through Nginx Helper' );
$ex = apply_filters( 'rt_nginx_helper_exclude_post_types', array( 'nav_menu_item' ) );
gpwt( 'post-status exclusions: nav_menu_item kept, shop_order/shop_order_refund/shop_coupon added', ! array_diff( array( 'nav_menu_item', 'shop_order', 'shop_order_refund', 'shop_coupon' ), $ex ), implode( ',', $ex ) );
$ex = apply_filters( 'rt_nginx_helper_comment_change_exclude_post_types', array() );
gpwt( 'comment exclusions (order notes) include shop_order', in_array( 'shop_order', $ex, true ), implode( ',', $ex ) );

$orders = get_posts( array( 'post_type' => 'shop_order', 'post_status' => 'any', 'numberposts' => 1, 'fields' => 'ids' ) );
if ( $orders && method_exists( $nginx_helper_admin, 'set_future_post_option_on_future_status' ) ) {
	$GLOBALS['gpwt']['sent'] = array();
	$order_post              = get_post( $orders[0] );
	$nginx_helper_admin->set_future_post_option_on_future_status( 'wc-processing', 'wc-pending', $order_post );
	$nginx_helper_admin->set_future_post_option_on_future_status( 'publish', 'publish', $order_post );   // worst case
	gpwt( 'order status change (Nginx Helper\'s own hook) -> no purge', ! $GLOBALS['gpwt']['sent'], gpwt_rel( $GLOBALS['gpwt']['sent'] ) );
} else {
	gpwt_out( '  SKIP no order stored as a post (HPOS on): orders never reach Nginx Helper anyway' );
}

gpwt_end();
