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

$home = trailingslashit( home_url() );
$shop = wc_get_page_id( 'shop' ) > 0 ? get_permalink( wc_get_page_id( 'shop' ) ) : '';

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
$want = array( $home, get_permalink( $simple->get_id() ) );
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
gpwt( 'stock status change -> home, shop, product, its categories + parent categories', $u && ! $missing, 'missing: ' . gpwt_rel( $missing ) );
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
gpwt( 'queue is empty after the flush', ! GP_Woo_Purge::flush() );

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
