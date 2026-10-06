# GridPane FastCGI cache: ignore marketing query parameters

This gives you WP Rocket's "ignored parameters" behaviour in GridPane's nginx:

```
example.com/about-us/?gclid=something
example.com/about-us/?gclid=somethingelse&utm_source=fb
// both are served from the cache entry of:
example.com/about-us/
```

- **No redirect.** The parameters stay in the browser URL, so GA4, Google Ads (gclid/gbraid/wbraid), Meta Pixel (fbclid → `_fbc`), Microsoft Ads etc. keep working. The [GridPane KB article](https://gridpane.com/kb/remove-specific-query-strings-to-load-the-cached-version-of-a-page/) instead issues a 301 that drops the parameters, which breaks attribution.
- **One cache entry.** A request whose query contains only marketing parameters is served from the clean URL's entry: HIT, plus the header `X-Grid-Cache-Mkt: ignored`.
- **Every other parameter behaves as in stock GridPane.** `/?s=foo&gclid=1` and `/x/?page=2&utm_source=x` → BYPASS, and PHP gets the original URL.
- **No cache poisoning.** When a response is going to be cached, PHP sees the clean URL. The first visitor's `gclid=...` never ends up in links, pagination or hidden fields of the stored HTML.
- **WordPress redirects keep the parameters.** This covers the missing trailing `/`, www → apex, canonical redirects and 404 URL guessing. nginx re-appends the original marketing parameters to `Location`, for the same host only.
- **Purge is unchanged.** The clean URL's key is byte-identical to stock GridPane's, so Nginx Helper (`/purge/<path>`) also clears the gclid/utm variants.
- **Pure nginx** (`map` + PCRE). No Lua or njs, and no GridPane-managed files are edited.

## Installation

On the GridPane server, as root:

```bash
git clone git@github.com:kayasparrow/gridpane-marketing-params.git /root/gp-marketing-params
```

```bash
cd /root/gp-marketing-params && ./install.sh yoursite.com
```

The script does three things:

1. **Once per server:** installs the engine and the list in `/etc/nginx/marketing-params/`, plus a one-line stub in `/etc/nginx/conf.d/`. An existing list is never overwritten.
2. **Per site:** adds one one-line switch in `/var/www/<site>/nginx/`.
3. **Checks and reloads:** runs `nginx -t`. If it passes, it reloads nginx. If it fails, it restores the previous files.

```bash
./install.sh site1.com site2.com          # several sites
./install.sh --status                     # what is installed / enabled / suspicious
./install.sh --disable yoursite.com       # switch off for a site
./install.sh --uninstall                  # remove everything (list saved to /root/marketing-params.list.bak)
./install.sh                              # update the engine after git pull (the list is kept)
MAX_PARAMS=32 MAX_LEN=4096 ./install.sh   # other limits (saved in limits.env)
```

The script skips sites without FastCGI caching (Redis or no cache). It also skips sites with a customised cache key, such as GeoIP or GridPane's native Lua feature.

## Tests

```bash
./test.sh
```

**Sandbox.** Starts a throw-away nginx on a random `127.0.0.1` port. It uses the server's binary and **GridPane's real template from this server** (`/etc/nginx/common/wpfc.conf`), the files from this repo and a fake PHP backend (`tests/fake_php.py`). It runs 121 checks, then cleans up after itself. Covered:
- cache sharing and no poisoning,
- BYPASS for other parameters,
- limits,
- POST, cookies, excluded URIs, your own skip rules and your own rewrites all take precedence,
- redirects, including foreign hosts and relative URLs,
- purge,
- a site without the switch,
- `nginx -t` after a clone to a server without the engine, without `params.list`, and on a site without page cache,
- a clean error log.

**It never touches the live nginx or the sites.** Run it after every GridPane update: if GridPane changes its template, the test catches it.

```bash
./test.sh --site yoursite.com
```

Same, but using that site's rendered template.

```bash
./test.sh live yoursite.com /some-page/
```

**Live test after installation.** Sends GET requests only, to the local nginx (bypassing Cloudflare), and purges nothing. It checks:
- HIT with gclid,
- a shared entry with the clean URL,
- BYPASS for another parameter,
- redirects (missing trailing slash, www → apex) that keep the parameters.

Pass a page with a trailing `/` so the redirect test runs too.

Exit codes: `0` = all PASS, `1` = at least one FAIL, `2` = setup error (e.g. GridPane changed its template).

## Parameter list

`/etc/nginx/marketing-params/params.list`, one line per parameter:

```nginx
gclid 1;        # exact name (case-insensitive)
~*^utm_ 1;      # regex on the name: every utm_* (~* = case-insensitive)
```

After editing:

```bash
nginx -t && systemctl reload nginx
```

There is one list per server. It applies to every site with the switch enabled.

**Golden rule:** only list parameters that are read by **JavaScript**, not PHP. On cached pages PHP never sees them, just like on any cache HIT.
- Off by default (optional, documented in the file): `mc_cid`/`mc_eid` (Mailchimp for WooCommerce sets cookies from them in PHP) and `ck_subscriber_id` (Kit).
- Never add: `ref`, `s`, `p`, `page_id`, `lang`, `add-to-cart`, `cn-reloaded`, `age-verified`.

## How it works

```
request /about-us/?gclid=1&utm_source=x
  │
  ├─ GridPane wpfc.conf (server):  query string ≠ ""  → skip_cache=1, skip_reason="-query_string"
  │                                 + your own skip rules (they append to skip_reason)
  │
  ├─ location / → try_files … /index.php?$args   (plus any rewrites of your own)
  │
  └─ location ~ \.php$  →  marketing-params-php-context.conf  (the decision happens HERE)
        un-skip only if:  skip_reason == "-query_string" (the only reason)
                          GET/HEAD
                          $args == the original query (nothing rewrote it)
                          ≤ 2048 bytes, ≤ 24 params, ALL of them on the list
        → skip_cache=0, key …$host/about-us/  (= the clean URL's key)
        → PHP: REQUEST_URI=/about-us/, QUERY_STRING=""
        → 3xx Location (same host) + "?gclid=1&utm_source=x"
```

| File | Context | Role |
|---|---|---|
| `/etc/nginx/marketing-params/params.list` | include inside `map` | **the list you edit** |
| `/etc/nginx/marketing-params/engine.conf` | http | `map` chain, generated by `build-engine.py` |
| `/etc/nginx/marketing-params/php-context.conf` | php location | decision, cache key, clean URL for PHP, `Location` fix |
| `/etc/nginx/marketing-params/limits.env` | – | saved limits |
| `/etc/nginx/conf.d/marketing-params.conf` | http | stub: `include …/engine[.]conf;` |
| `/var/www/<site>/nginx/marketing-params-php-context.conf` | php location | **the site switch**: `include …/php-context[.]conf;` |

### Robustness

- **GridPane's files are not edited.** Custom-named files survive the nightly sync and `gp update`.
- **Cloning to a server without the engine.** GridPane only copies `/var/www/<site>/nginx/`, and the include uses an exact-name glob. On a server without the engine nothing happens and `nginx -t` passes.
- **No dependency on GridPane's variables.** The engine never references them and the switch declares them itself, so changing the site's cache type in the GridPane UI cannot break `nginx -t`.
- **Your own rules win.** A skip rule that appends to `$skip_reason` (as in GridPane's KB examples) disables the cache with gclid too. A rewrite that changes `$args` also leads to stock behaviour.
- **Stray copies are ignored.** Files like `engine.old.conf` or `php-context.orig.conf` are not loaded. A missing `params.list` simply means "nothing counts as marketing".
- **Long queries cost nothing.** Above 2048 bytes (or with a path longer than 8 KB) the chain does not run at all. It also stops at the first parameter that is not on the list.

## Limitations and behaviour worth knowing

- **Skip rules without a reason.** A rule that sets `$skip_cache 1` **without** appending to `$skip_reason` is overridden for requests carrying only marketing parameters. Always append a reason: `set $skip_reason "${skip_reason}-my_reason";`.
- **The cache key is overridden for every request.** On an enabled site the switch sets `fastcgi_cache_key` to the stock formula `$scheme$request_method$host$request_uri`. That is why `install.sh` refuses sites with a different key formula.
- **Redirects to other hosts.** Parameters are re-appended only to a `Location` on the same host (or its www/apex twin) or to a `/…` path. Redirects to other domains (alias → primary domain, S3, payment gateways) get nothing appended. If the redirect target already has its own `utm_*`, the parameters end up duplicated.
- **Limits.** Defaults are 24 params (empty `&&` segments don't count), 2048 bytes of query and 8 KB of path. Beyond that you get stock BYPASS. Each param of the limit costs 4 nginx variables, so don't raise it without need (GridPane sets `variables_hash_max_size 2048`).
- **Raw names.** Names are matched in their raw (undecoded) form: `utm%5Fsource` = BYPASS (safe).
- **Clones and staging on the same server.** GridPane copies the switch, so the copy is enabled straight away. Pushing staging to production re-enables the feature on a production site where it was disabled. `./install.sh --status` lists every site that has the switch.
- **`Set-Cookie` in the cache.** Stock GridPane caches responses together with `Set-Cookie`. PHP never sees the marketing values, so no cookie can be built from gclid or utm.
- **GridPane's native feature.** GridPane has an undocumented Lua feature (`gp stack nginx -lua query-param-cache`). It normalises the key only, so PHP sees the parameters and cached HTML can be poisoned. Don't combine it with this solution on the same site.
- **Purge has to reach the local nginx** with the same `$scheme` and `$host`. If the site's domain goes through Cloudflare and `/etc/hosts` has no `127.0.0.1` entry for it, Nginx Helper gets a 403 from Cloudflare and the purge never happens.

## Cloudflare

**Watch out for Cloudflare rules that strip parameters.** If a Transform Rule in the zone removes e.g. `fbclid`, the server never sees it. With a mixed query (`/?fbclid=1&other=1`) the origin then receives `/?&other=1`, and WordPress answers with a canonical 301 to `/?other=1`, so the browser loses fbclid. With this solution such Cloudflare rules are unnecessary; turn them off.

**Cloudflare as an alternative.** A Transform Rule with `remove_query_args()` works on every plan. Its drawbacks:
- exact names only (no `utm_*`),
- configured per zone,
- the origin never sees the parameters at all, so it cannot put them back on redirects.

Excluding specific parameters from the cache key without rewriting the URL is Enterprise-only.

## Repository layout

```
install.sh                      install / enable / disable / status / uninstall
test.sh                         tests: sandbox (default) or `live <site> [/path/]`
build-engine.py                 engine.conf generator
server/marketing-params/        → /etc/nginx/marketing-params/  (php-context.conf, params.list)
server/stubs/                   → /etc/nginx/conf.d/marketing-params.conf
site/                           → /var/www/<site>/nginx/marketing-params-php-context.conf
tests/                          sandbox_test.py, live_test.py, fake_php.py (PHP-FPM stand-in)
```
