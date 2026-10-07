# GridPane FastCGI cache: ignore marketing query parameters

This gives you WP Rocket's "ignored parameters" behaviour in GridPane's nginx:

```
example.com/about-us/?gclid=something
example.com/about-us/?gclid=somethingelse&utm_source=fb
// both are served from the cache entry of:
example.com/about-us/
```

- **No redirect.** The parameters stay in the browser URL, so GA4, Google Ads (gclid/gbraid/wbraid), Meta Pixel (fbclid → `_fbc`), Microsoft Ads etc. keep working. The [GridPane KB article](https://gridpane.com/kb/remove-specific-query-strings-to-load-the-cached-version-of-a-page/) instead uses a 301 that drops the parameters, which breaks attribution.
- **One cache entry.** A request whose query contains only marketing parameters is served from the clean URL's entry (HIT, plus the header `X-Grid-Cache-Mkt: ignored`).
- **Every other parameter behaves as in stock GridPane.** `/?s=foo&gclid=1` and `/x/?page=2&utm_source=x` → BYPASS, and PHP gets the original URL.
- **No cache poisoning.** When a response is going to be cached, PHP sees the clean URL. The first visitor's `gclid=...` never ends up in links, pagination, hidden fields or cookies of the stored response.
- **WordPress redirects keep the parameters.** This covers missing trailing `/`, www → apex, canonical redirects and 404 URL guessing: nginx re-appends the original marketing parameters to `Location` (same host only).
- **Purge is unchanged.** The clean URL's key is byte-identical to stock GridPane's, so Nginx Helper (`/purge/<path>`) also clears the gclid/utm variants.
- **Pure nginx** (`map` + PCRE). No Lua or njs, and no GridPane-managed file is edited.

## Installation

On the GridPane server, as root:

```bash
git clone https://github.com/SHIFT64/gridpane-nginx-marketing-params.git /root/gridpane-nginx-marketing-params
```

```bash
cd /root/gridpane-nginx-marketing-params && ./test.sh && ./install.sh yoursite.com
```

To update later: `git pull && ./test.sh && ./install.sh` (your list is kept).

The script:

1. **Once per server:** installs the engine and the list in `/etc/nginx/marketing-params/`, plus a one-line stub in `/etc/nginx/conf.d/`. An existing list is never overwritten.
2. **Per site:** adds two one-line files in `/var/www/<site>/nginx/` (the switch and a FastCGI flag; either one alone does nothing).
3. **Checks and reloads:** runs `nginx -t`. If it passes, nginx is reloaded. If it fails, the previous files are restored.

```bash
./install.sh site1.com site2.com          # several sites
./install.sh --status                     # health check: files + checksums, running config, GridPane template hooks, nginx -t
./install.sh --disable yoursite.com       # switch off for a site
./install.sh --apply-list                 # after editing the list (see below)
./install.sh --uninstall                  # remove everything (list saved to /root/marketing-params.list.bak)
./install.sh                              # update the engine after git pull (the list is kept)
MAX_PARAMS=32 MAX_LEN=4096 ./install.sh   # other limits (saved in limits.env)
```

Skipped: sites without FastCGI caching (Redis or no cache), and sites with a customised cache key (GeoIP, GridPane's Lua feature).

## Tests

```bash
./test.sh
```

**Sandbox.** Starts a throw-away nginx on a random `127.0.0.1` port. It uses the server's own binary, **GridPane's real template from this server** (`/etc/nginx/common/wpfc.conf`), the files from this repo, and a fake PHP backend (`tests/fake_php.py`). It runs 125 checks and then cleans up. Covered:
- cache sharing and no poisoning
- BYPASS for other parameters
- limits
- POST, cookies, excluded URIs, your own skip rules and your own rewrites taking precedence
- redirects, including foreign hosts, relative URLs and nginx's own `return 301`
- purge
- a site without the switch, and a vhost without the FastCGI flag
- `nginx -t` after a clone to a server without the engine, without `params.list`, and on a site without page cache
- a clean error log

**It never touches the live nginx or the sites.** Run it after every GridPane update: if GridPane changes its template, the test catches it.

```bash
./test.sh --site yoursite.com
```

Same, using that site's rendered template.

```bash
./test.sh live yoursite.com /some-page/
```

**Live test after installation.** It sends GET requests only, straight to the local nginx (127.0.0.1, so no proxy/CDN in front is involved), and purges nothing. It checks:
- HIT with gclid
- a shared entry with the clean URL
- BYPASS for another parameter
- that the missing-trailing-slash and www → apex redirects keep the parameters

Give it a page with a trailing `/` so the redirect test runs too.

Exit codes: `0` = all PASS, `1` = at least one FAIL, `2` = setup error (e.g. GridPane changed its template).

## Parameter list

`/etc/nginx/marketing-params/params.list`, one line per parameter:

```nginx
gclid 1;        # exact name (case-insensitive)
~*^utm_ 1;      # regex on the name: every utm_* (~* = case-insensitive)
```

After editing:

```bash
./install.sh --apply-list
```

This runs `nginx -t` and reloads. If the test fails (e.g. a missing `;`), it restores the last good list and keeps your edit in `params.list.rejected`, so a typo can never block nginx reloads on the server. There is one list per server, and it applies to every site with the switch.

**Golden rule:** only list parameters that are read by **JavaScript**, not PHP. On cached pages PHP never sees them, just like on any cache HIT.
- Off by default (optional, documented in the file): `mc_cid`/`mc_eid` (Mailchimp for WooCommerce sets cookies from them in PHP) and `ck_subscriber_id` (Kit).
- Never add: `ref`, `s`, `p`, `page_id`, `lang`, `add-to-cart`, `cn-reloaded`, `age-verified`.

## How it works

```
request /about-us/?gclid=1&utm_source=x
  │
  ├─ GridPane wpfc.conf (server):  query string ≠ ""  → skip_cache=1, skip_reason="-query_string"
  │                                 + your own skip rules (they set/append skip_reason)
  │                                 + marketing-params-skip-fcgi-cache-context.conf: $gp_mkt_fcgi=1
  │
  ├─ location / → try_files … /index.php?$args   (plus any rewrites of your own)
  │
  └─ location ~ \.php$  →  marketing-params-php-context.conf  (the decision happens HERE)
        un-skip only if:  FastCGI flag set
                          skip_reason == "-query_string" (the only reason)
                          GET/HEAD
                          $args == the original query (nothing rewrote it)
                          ≤ 2048 bytes, ≤ 24 params, ALL of them on the list
        → skip_cache=0, key …$host/about-us/  (= the clean URL's key)
        → PHP: REQUEST_URI=/about-us/, QUERY_STRING=""
        → 3xx Location (same host) + "?gclid=1&utm_source=x"
```

| File | Context | Role |
|---|---|---|
| `/etc/nginx/marketing-params/params.list` | include in `map` | **the list you edit** |
| `/etc/nginx/marketing-params/engine.conf` | http | `map` chain, generated by `build-engine.py` |
| `/etc/nginx/marketing-params/php-context.conf` | php location | decision, cache key, clean URL for PHP, `Location` fix |
| `/etc/nginx/marketing-params/limits.env` | – | saved limits |
| `/etc/nginx/conf.d/marketing-params.conf` | http | stub: `include …/engine[.]conf;` |
| `/var/www/<site>/nginx/marketing-params-php-context.conf` | php location | **site switch**: `include …/php-context[.]conf;` |
| `/var/www/<site>/nginx/marketing-params-skip-fcgi-cache-context.conf` | server | **FastCGI flag**: `set $gp_mkt_fcgi 1;` (GridPane includes this pattern only in its FastCGI template) |

### Robustness

- **What GridPane leaves alone, and what it doesn't** (from an audit of GridPane's scripts, gp-cli 1.2.1518 / configs 1.2.90, 2026-10-07):
  - **Kept:** the nightly jobs, `gp update` (including force runs), per-site config regeneration and gridpane-nginx package upgrades never delete or rewrite these custom-named files. They only reset permissions: 640 under `/etc/nginx`, and root:root 644 in the site's `nginx/` dir, where symlinks are deleted, so the switch files must stay regular files. GridPane's update also strips every line containing certain directive names from custom files; `install.sh` refuses to ship such a line.
  - **Removed or replaced:** the per-site files go away when the site is deleted, when it is fully restored from a backup (they return to whatever the backup held), or when another server's site is cloned or migrated into it. A Migrately move does not carry the server-wide engine.
  - **Not under our control:** GridPane can change its own template on any update. Run `./install.sh --status` after GridPane updates (it checks file checksums, the running config, the template hooks and `nginx -t`), and re-run `./install.sh <site>` after a restore or clone.
- **Clone to a server without the engine.** GridPane only copies `/var/www/<site>/nginx/`, and the include uses an exact-name glob. On such a server nothing happens and `nginx -t` passes.
- **Site switched to Redis or no cache.** The switch stays inert because the FastCGI flag file is only included by GridPane's FastCGI template.
- **No dependency on GridPane's variables.** The engine never references them and the switch declares them itself, so changing a site's cache type in the GridPane UI cannot break `nginx -t`.
- **Your own rules win.** This holds for a skip rule that sets/appends `$skip_reason` (as all GridPane KB examples do) and for a rewrite that changes `$args`.
- **Stray copies are ignored.** Files like `engine.old.conf` or `php-context.orig.conf` are not loaded. A missing `params.list` means "nothing counts as marketing".
- **Long queries cost nothing.** Above 2048 bytes (or with a path longer than 8 KB) the chain does not run. It also stops at the first parameter that is not on the list.

## Limitations and behaviour worth knowing

- **Skip rules without a reason.** A rule that sets `$skip_cache 1` **without** setting or appending `$skip_reason` is overridden for marketing-only URLs. Always give a reason: `set $skip_reason "${skip_reason}-my_reason";`.
- **The cache key is forced to the stock formula.** On an enabled site, the switch sets `fastcgi_cache_key` to `$scheme$request_method$host$request_uri` for **every** request, so `install.sh` refuses sites with a different key formula. The solution also relies on GridPane's literal `-query_string` reason, so re-run `./test.sh` after GridPane updates.
- **Redirects to other hosts.** Parameters are re-appended only to a `Location` on the same host (or its www/apex twin) or to a `/…` path. A PHP redirect to another domain (alias → primary, S3, payment gateways, an affiliate link that passes the query on) gets nothing appended, so the params are lost there. A same-host redirect that drops the query gets them back. If the target already has its own `utm_*`, the parameters end up duplicated. nginx's own redirects inside the PHP location are sent with a relative `Location` (valid HTTP).
- **Limits.** Defaults are 24 params (empty `&&` segments don't count), 2048 bytes of query and 8 KB of path. Beyond that you get stock BYPASS. Each param of the limit costs 4 nginx variables and GridPane's variables hash limit (2048, set in `common/basics.conf`) is shared by the whole server, so don't raise it without need.
- **Matching.** Names are matched raw (undecoded) and case-insensitively. `utm%5Fsource` is a BYPASS (safe). `UTM_SOURCE` shares the cache entry, although PHP treats it as a different name.
- **Clones and staging on the same server.** GridPane copies the switch, so the copy is enabled straight away. Pushing staging to production re-enables the feature on a production site where it was disabled. `./install.sh --status` lists every site that has the switch.
- **`Set-Cookie` in the cache.** Stock GridPane caches responses together with `Set-Cookie`. PHP never sees the marketing values, so no cookie can be built from them.
- **Purge has to reach the local nginx** with the same `$scheme` and `$host` as the cache key. If the site's domain points somewhere else (a proxy/CDN) and `/etc/hosts` has no `127.0.0.1` entry for it, Nginx Helper's purge requests never reach this server.

## GridPane Lua (hidden) vs this repo

GridPane ships an undocumented feature that does something similar: `gp stack nginx -lua query-param-cache …` and `gp site <site> -lua-query-param-cache on`. We compared the two on the same WooCommerce site. The same scripted matrix ran in three modes, one after another: stock, this repo, GridPane Lua. Details:
- date: 2026-10-06
- versions: GridPane nginx 1.30.4, gp-cli 1.2.1518
- the cookie and redirect cases used small test PHP scripts

GridPane may change its feature, so treat the Lua column as a snapshot of that version.

| | GridPane Lua (hidden) | This repo |
|---|---|---|
| Setup | `gp stack nginx -lua query-param-cache ensure-ready`, then `gp site <site> -lua-query-param-cache on`. Needs GridPane nginx 1.28.x/1.30.x. nginx reloads; it restarts fully only if LuaJIT is not loaded yet (on the test server it already was, so it reloaded). | `./install.sh <site>` (`nginx -t`, reload, rollback) |
| Docs / maintenance | Only a changelog entry (v1.2.1514, "documentation coming soon"); maintained by GridPane | This README + tests; maintained by you |
| Page-cache types | FastCGI, Redis, proxy-Redis | FastCGI only |
| No redirect, shared entry, unknown param → BYPASS, purge | ✅ | ✅ |
| What PHP sees when the page is cached (MISS) | **The original URL.** Whatever the page renders from the first visitor's parameters is cached for everyone. Live: the WooCommerce home page was cached with the first visitor's tracking value 34× and served to later clean visitors. A cookie that PHP built from gclid was replayed from the cache. | **The clean URL** (0 echoes, no cookie). Trade-off: listed params never reach PHP on cacheable pages (see the golden rule). |
| WordPress redirects (trailing slash, www → apex) | ✅ keep params | ✅ keep params |
| Same-host redirect that drops the query | Params lost (as in stock) | Params re-appended |
| Custom skip rules | Any rule wins (it never lowers `$skip_cache`) | Rules must set a `$skip_reason`, otherwise overridden |
| Default list | 4 prefixes + 32 names. Misses e.g. dclid, srsltid, ttclid, twclid, li_fat_id, yclid, `mtm_*`, `pk_*`, _hsenc, fbadid (some ship commented out) | 4 prefixes + 72 names, covering those. Misses GridPane's `tw_*`, njclid, `cq_*`, `aaa_*`, gtm_debug. Includes campaignid/adgroupid, which GridPane deliberately leaves out |
| Matching | Case-sensitive. Any `;` or malformed `%` → BYPASS | Case-insensitive. `;` / malformed `%` inside values accepted |
| Limits | 64 params / 4096 bytes | 24 / 2048 by default (configurable up to 48 / 8192) |
| Editing the list | Strict `exact/prefix` grammar, then `gp … query-param-cache regen` (probe, `nginx -t`, rollback) | nginx map syntax, then `./install.sh --apply-list` (`nginx -t`, last good list restored on error) |
| Lifecycle | GridPane-managed: clones, migrations, staging, aliases. In the tested version `on` snapshots the site's cache template, so later GridPane template fixes need `on` / `regenerate -force-rewrite` again (this may change) | Never edits GridPane templates, but forces the stock cache key and relies on the `-query_string` reason. Re-run `./test.sh` after GridPane updates |
| Observability | `X-Grid-Cache-Skip: ignored_query_string` on every request with a query (also on HITs and POST); bypass reason `-query_param` | `X-Grid-Cache-Mkt: ignored`; GridPane's reasons untouched otherwise |
| Performance (HIT) | ≈ 830–840 req/s. Plus ≈ 2 MB RSS per nginx process (single snapshot) | ≈ 820–830 req/s. Stock for reference: 849 req/s clean HIT, but **12 req/s** for a marketing URL (BYPASS → PHP) |
| Revert | `off` + `gp stack nginx -lua disable`. Leaves inert files behind. Purge afterwards | `./install.sh --disable` / `--uninstall` |

**When to choose which**
- **GridPane Lua** is the better fit for Redis/proxy-Redis caching or when you want everything vendor-managed. Use it only if your pages provably don't render query parameters into HTML or cookies. To check, request a page with a unique `gclid`, then the clean URL, and search the HTML and `Set-Cookie`.
- **This repo** is the better fit for FastCGI sites whose theme or plugins echo parameters (WooCommerce did here), or when you want the broader default list. In exchange you accept: FastCGI only, PHP never sees listed params, custom skip rules need a reason, and you maintain it yourself.
- **Never run both on the same site.**

## Repository layout

```
install.sh                      install / enable / disable / apply list / status / uninstall
test.sh                         tests: sandbox (default) or `live <site> [/path/]`
build-engine.py                 engine.conf generator
server/marketing-params/        → /etc/nginx/marketing-params/  (php-context.conf, params.list)
server/stubs/                   → /etc/nginx/conf.d/marketing-params.conf
site/                           → /var/www/<site>/nginx/  (switch + FastCGI flag)
tests/                          sandbox_test.py, live_test.py, fake_php.py (PHP-FPM stand-in)
```
