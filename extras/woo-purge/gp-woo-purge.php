<?php
/**
 * Plugin Name: WooCommerce purge for Nginx Helper
 * Description: Purges the page cache of a product, its archives, the shop page and the home page when WooCommerce changes a price or a stock status without a post update (orders, REST API, imports, bulk edit, scheduled sales). Stops orders, refunds and coupons from purging the home page. Installed by gridpane-nginx-marketing-params.
 * Version: 0.1.2
 * Requires PHP: 7.4
 *
 * Why: Nginx Helper purges on `transition_post_status`, i.e. only when WordPress updates the post.
 * WooCommerce saves a price-only or stock-only change straight to the database (no
 * wp_update_post), so Nginx Helper never hears about it and the old page stays cached until
 * it expires. This file listens to WooCommerce itself and hands the URLs to Nginx Helper's
 * purger, so the purge method (GET /purge/…, unlink, Redis) is whatever the site already uses.
 *
 * Filters:
 *   gp_woo_purge_excluded_post_types  array  post types that never trigger Nginx Helper purges
 *   gp_woo_purge_trigger_props        array  WooCommerce props that trigger a purge
 *   gp_woo_purge_max_pages            int    archive pages purged per term / shop (default 5)
 *   gp_woo_purge_urls                 array  final URL list (second arg: product IDs)
 */

defined( 'ABSPATH' ) || exit;

final class GP_Woo_Purge {

	const VERSION = '0.1.2';

	/** @var array<int, string[]> product ID => props that changed */
	private static $queue = array();

	public static function init() {
		add_filter( 'rt_nginx_helper_exclude_post_types', array( __CLASS__, 'exclude_post_types' ) );
		add_filter( 'rt_nginx_helper_comment_change_exclude_post_types', array( __CLASS__, 'exclude_post_types' ) );
		add_action( 'woocommerce_product_object_updated_props', array( __CLASS__, 'on_updated_props' ), 10, 2 );
		add_action( 'shutdown', array( __CLASS__, 'flush' ) );
	}

	/**
	 * Orders (HPOS off), refunds and coupons are posts, so every status change and order note
	 * made Nginx Helper purge the home page, the author archive and the feeds.
	 */
	public static function exclude_post_types( $types ) {
		$ours = apply_filters(
			'gp_woo_purge_excluded_post_types',
			array( 'shop_order', 'shop_order_refund', 'shop_order_placehold', 'shop_coupon' )
		);
		return array_values( array_unique( array_merge( (array) $types, $ours ) ) );
	}

	/**
	 * Fired by the WooCommerce data store after every product / variation save, with the list
	 * of props that actually changed. A stock quantity change alone does not purge; it purges
	 * only when it flips the stock status (in stock <-> out of stock <-> on backorder).
	 */
	public static function on_updated_props( $product, $props ) {
		if ( ! $product instanceof WC_Product || empty( $props ) ) {
			return;
		}
		$triggers = apply_filters(
			'gp_woo_purge_trigger_props',
			array( 'price', 'regular_price', 'sale_price', 'date_on_sale_from', 'date_on_sale_to', 'stock_status' )
		);
		$hit = array_values( array_intersect( (array) $props, $triggers ) );
		if ( ! $hit ) {
			return;
		}
		$id = $product->is_type( 'variation' ) ? $product->get_parent_id() : $product->get_id();
		if ( ! $id || 'publish' !== get_post_status( $id ) ) {
			return;
		}
		self::$queue[ $id ] = array_values( array_unique( array_merge( self::$queue[ $id ] ?? array(), $hit ) ) );
	}

	/**
	 * Runs once per request (shutdown), so a checkout that changes ten products or a variation
	 * save that also syncs its parent purges every URL only once.
	 *
	 * @return string[] purged URLs
	 */
	public static function flush() {
		global $nginx_purger, $nginx_helper_admin;

		if ( ! self::$queue ) {
			return array();
		}
		$queue       = self::$queue;
		self::$queue = array();

		if ( ! is_object( $nginx_purger ) || ! method_exists( $nginx_purger, 'purge_url' )
			|| empty( $nginx_helper_admin->options['enable_purge'] ) ) {
			return array();
		}

		$max_pages = max( 1, (int) apply_filters( 'gp_woo_purge_max_pages', 5 ) );
		$urls      = array( trailingslashit( home_url() ) );

		// The shop page's URL, or /shop/ (WooCommerce's product archive) when no shop page is assigned.
		$shop = get_post_type_archive_link( 'product' );
		if ( $shop ) {
			$urls = array_merge( $urls, self::paged( $shop, (int) wp_count_posts( 'product' )->publish, $max_pages ) );
		}
		foreach ( array_keys( $queue ) as $id ) {
			$urls = array_merge( $urls, self::product_urls( $id, $max_pages ) );
		}

		$urls = apply_filters( 'gp_woo_purge_urls', array_values( array_unique( array_filter( $urls ) ) ), array_keys( $queue ) );

		$what = array();
		foreach ( $queue as $id => $props ) {
			$what[] = $id . ' (' . implode( ',', $props ) . ')';
		}
		$nginx_purger->log( '* WooCommerce purge: products ' . implode( ' ', $what ) . ' -> ' . count( $urls ) . ' URLs' );

		foreach ( $urls as $url ) {
			$nginx_purger->purge_url( $url, false );   // false: no /feed/ variants, a shop has none worth caching
		}
		return $urls;
	}

	/**
	 * The product page plus every public archive it is listed in. Nginx Helper only purges the
	 * terms a product is assigned to directly; here parent categories are included, because
	 * WooCommerce lists child-category products on the parent's page too.
	 */
	private static function product_urls( $id, $max_pages ) {
		$urls = array( get_permalink( $id ) );

		foreach ( get_object_taxonomies( 'product', 'objects' ) as $tax ) {
			if ( empty( $tax->public ) || empty( $tax->publicly_queryable ) ) {
				continue;   // product_type, product_visibility, attributes without archives
			}
			$terms = get_the_terms( $id, $tax->name );
			if ( ! $terms || is_wp_error( $terms ) ) {
				continue;
			}
			$term_ids = array();
			foreach ( $terms as $term ) {
				$term_ids[] = $term->term_id;
				if ( $tax->hierarchical ) {
					$term_ids = array_merge( $term_ids, get_ancestors( $term->term_id, $tax->name, 'taxonomy' ) );
				}
			}
			foreach ( array_unique( $term_ids ) as $term_id ) {
				$term = get_term( $term_id, $tax->name );
				$link = $term && ! is_wp_error( $term ) ? get_term_link( $term ) : '';
				if ( ! $link || is_wp_error( $link ) ) {
					continue;
				}
				// WooCommerce keeps a count that includes child terms; the core count does not.
				$count = (int) get_term_meta( $term_id, 'product_count_' . $tax->name, true );
				$urls  = array_merge( $urls, self::paged( $link, max( $count, (int) $term->count ), $max_pages ) );
			}
		}
		return $urls;
	}

	/** The archive URL and its /page/N/ URLs, as many as the product count needs (capped). */
	private static function paged( $link, $count, $max_pages ) {
		global $wp_rewrite;

		$urls = array( $link );
		if ( ! $link || false !== strpos( $link, '?' ) || ! $wp_rewrite || ! $wp_rewrite->using_permalinks() ) {
			return $urls;   // plain permalinks: paged URLs carry a query and are never cached
		}
		$per_page = (int) apply_filters( 'loop_shop_per_page', wc_get_default_products_per_row() * wc_get_default_product_rows_per_page() );
		$pages    = $per_page > 0 ? min( $max_pages, (int) ceil( $count / $per_page ) ) : 1;
		for ( $n = 2; $n <= $pages; $n++ ) {
			$urls[] = trailingslashit( $link ) . $wp_rewrite->pagination_base . '/' . $n . '/';
		}
		return $urls;
	}
}

add_action( 'plugins_loaded', static function () {
	if ( class_exists( 'WooCommerce' ) ) {
		GP_Woo_Purge::init();
	}
} );
