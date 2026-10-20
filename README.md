# GridPane page cache (FastCGI and Redis): ignore marketing query parameters

> **Several sites on one GridPane server? Use the Redis page cache.** GridPane's FastCGI cache is a single zone for the whole server: a "purge all" on any site (Nginx Helper's button, a theme switch, "purge on update", a plugin calling `purge_all`) empties **every** site's cache. The Redis page cache keeps each site in its own database and every purge stays within the site. Evidence, the list of `purge_all` triggers and measurements: [issue #1](https://github.com/SHIFT64/gridpane-nginx-marketing-params/issues/1). This repo works with both cache types; see [Redis page cache](#redis-page-cache).

This gives you WP Rocket's "ignored parameters" behaviour in GridPane's nginx, for both page-cache types GridPane offers for WordPress: **FastCGI** and **Redis**.

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
- **Purge is unchanged.** The clean URL's key is byte-identical to stock GridPane's, so Nginx Helper (FastCGI: `/purge/<path>`; Redis: `DEL` of the URL's key) and GridPane's own purge also clear the gclid/utm variants.
- **Pure nginx** (`map` + PCRE). No Lua or njs, and no GridPane-managed file is edited.

## Installation

On the GridPane server, as root:

```bash
git clone https://github.com/SHIFT64/gridpane-nginx-marketing-params.git /root/gridpane-nginx-marketing-params
```

```bash
cd /root/gridpane-nginx-marketing-params && ./test.sh && ./install.sh yoursite.com
```

To update later: `git pull && ./test.sh && ./install.sh` (your list is kept), or just `./install.sh`, which updates itself to the newest release (below).

Versions follow [semantic versioning](https://semver.org): one version for the whole repository, the WooCommerce add-on's `Version:` header included. While it is `0.x`, a minor bump (`0.2.0`) may change behaviour; patch releases (`0.1.1`) only fix things or change docs.

**Update check.** Every install command (`./install.sh [sites]`, `--full`, `--woo-purge`) first asks GitHub for the newest [release](https://github.com/SHIFT64/gridpane-nginx-marketing-params/releases) tag, `vX.Y.Z` (`git ls-remote`: refs only, nothing is downloaded). Commits on `main` that are not released yet never count as an update. If the newest release is not in this checkout yet, the script stops before changing anything, fast-forwards to that release, prints the new commits, runs the tests (`./test.sh`: sandbox + installer) and, only if they pass, starts again with the same arguments. It does not update when the checkout has local changes (it stops and says so), when the checkout already contains the release (e.g. you pulled `main`), or when GitHub is unreachable (it warns and continues with the local copy). `--status`, `--disable`, `--apply-list` and `--uninstall` never check. Skip the check with `GP_MKT_NO_UPDATE_CHECK=1 ./install.sh …`.

The script:

1. **Once per server:** installs the engine and the list in `/etc/nginx/marketing-params/`, plus a one-line stub in `/etc/nginx/conf.d/`. An existing list is never overwritten.
2. **Per site:** adds two small files in `/var/www/<site>/nginx/`: the switch and a flag for the site's cache type (FastCGI or Redis, detected from the vhost). Both carry the site's name; either file alone, or a pair of two different sites, does nothing. Every `./install.sh [sites]` / `--full` run also rewrites the files of all sites that already have them for the current version (they stay on or off as they were).
3. **Checks and reloads:** runs `nginx -t`. If it passes, nginx is reloaded. If it fails, the previous files are restored.

```bash
./install.sh site1.com site2.com          # several sites
./install.sh --status                     # health check: files + checksums, running config, GridPane template hooks, nginx -t
./install.sh --disable yoursite.com       # switch off for a site (no purge needed)
./install.sh --apply-list                 # after editing the list (see below)
./install.sh --uninstall                  # remove everything (list saved to /root/marketing-params.list.bak)
./install.sh                              # update the engine after git pull (the list is kept)
MAX_PARAMS=32 MAX_LEN=4096 ./install.sh   # other limits (saved in limits.env)
```

Skipped, with the reason: sites without page cache, sites behind GridPane's ModSecurity proxy (proxy-Redis has no PHP-location hook), a customised cache key (GeoIP), GridPane's Lua feature, GridPane's add-on-domain overlays (a template that includes another site's files), and a template or another `*-php-context.conf` of the site (or a file it includes) that sets the cache key or `REQUEST_URI` again after the switch.

If you change a site's cache type in GridPane, run `./install.sh <site>` again; see [Switching off, removing, changing](#switching-off-removing-changing).

## Switching off, removing, changing

Every command below runs `nginx -t` before the reload and puts the previous files back if it fails, so a failed change never reaches the running nginx. All of it is covered by `./test.sh install` (an `install.sh` run in each of these modes in a throw-away root, with the server's real templates and the real `nginx -t`).

| What | Command | What happens |
|---|---|---|
| Switch off one site | `./install.sh --disable site.com` | Removes the site's switch and flag (both cache types). Takes effect with the reload (a request already being handled by an old nginx worker can still get the old behaviour for about a second). **No purge needed:** everything cached while it was on is an ordinary entry of the clean URL, rendered exactly as for a visitor without parameters. |
| Remove everything | `./install.sh --uninstall` | Removes every site's switch and flag, the conf.d stub, the engine directory and the add-on copies; your list is saved to `/root/marketing-params.list.bak`. No purge needed either. |
| Change a site's cache type in GridPane (FastCGI ↔ Redis, or page cache off) | `./install.sh site.com` afterwards | GridPane keeps the custom files, but each flag is only included by its own template, so the site falls back to stock behaviour until you re-run the installer, which writes the switch and flag for the new type and removes the old flag. Verified live in both directions with the GridPane panel (it runs `gp site <site> -fastcgi-caching`, and `-cache-off` then `-redis-caching` on the way back): both files stayed in place, the site kept its Redis database, `nginx -t` passed, the site behaved exactly like stock until the installer was re-run, and the live test passed afterwards. `--status` reports such sites as a problem (the site expects the feature and doesn't get it). A leftover switch never changes the other cache type's key (only the FastCGI switch sets `fastcgi_cache_key`); a Redis switch on a non-Redis site makes nginx raise an "uninitialized variable" warning per PHP request, which GridPane's default `error` log level does not write. |
| A site the installer cannot handle (no page cache, customised key, GridPane's Lua feature, add-on-domain overlay, another file changing the key or `REQUEST_URI`) | `./install.sh site.com` | Skipped with the reason. If the site had a switch from before (e.g. GridPane changed its template since), those files are removed, so nothing half-working stays on. |
| Clone / staging copy | `./install.sh copy.com` | GridPane copies the site's `nginx/` directory, so the copy is on straight away and works (on Redis it uses its own database: the key prefix is read from GridPane's key at runtime). Its files still carry the original site's name, so `--status` asks you to run the installer, which writes the copy's own name (every `./install.sh [sites]` / `--full` run does this for every site). Until then the copy is not protected if it is later attached as an add-on domain of the original. |
| Update after `git pull` | `./install.sh` | Updates the engine and rewrites every site's switch files for the new version; sites stay on or off as they were. An engine and site files from before the Redis support are updated in place (until then, with the new engine, such old site files are inert and `--status` says so). |
| After a GridPane update | `./install.sh --status` (and `./test.sh`) | Checks checksums, the running config, the template hooks of every enabled site, and `nginx -t`. |

For WooCommerce shops, also add the [WooCommerce purge add-on](#woocommerce-purge-add-on):

```bash
./install.sh --full yourshop.com          # engine + switch + add-on, no questions
./install.sh --woo-purge yourshop.com     # only the add-on
./install.sh --remove-woo-purge yourshop.com
```

A plain `./install.sh yourshop.com` run from a terminal asks about the add-on for each WooCommerce site. `./install.sh` without sites (the update after `git pull`) refreshes every installed copy.

## Tests

```bash
./test.sh
```

Runs the sandbox test and the installer test below (`./test.sh --cache all` = sandbox only, `./test.sh install` = installer only).

**Sandbox.** Starts a throw-away nginx on a random `127.0.0.1` port. It uses the server's own binary, **GridPane's real templates from this server** (`/etc/nginx/common/wpfc.conf` and `/etc/nginx/common/wp-redis.conf`), the files from this repo, and a fake PHP backend (`tests/fake_php.py`). For Redis it also starts a throw-away `redis-server` on a unix socket inside its temp directory (one database per test vhost, as GridPane does). It runs 129 FastCGI and 144 Redis checks (144 more for GridPane's legacy Redis template, see below) and then cleans up. Covered, for both cache types:
- cache sharing and no poisoning
- BYPASS for other parameters
- limits, and an absolute-form request line with an empty path (`GET https://host?gclid=1`)
- POST, cookies, excluded URIs, your own skip rules and your own rewrites taking precedence
- redirects, including foreign hosts, relative URLs and nginx's own `return 301`
- purge
- a site without the switch, and a vhost without the cache-type flag
- `nginx -t` after a clone to a server without the engine, without `params.list`, and on a site without page cache
- a clean error log
- FastCGI only: the switch of one site with the flag of another, switch files of an older version, and the site's FastCGI flag with its Redis switch, stay stock
- Redis only: the entry is stored under the clean URL's stock key (`dbN:httpGET<host>/path/`) with GridPane's TTL; encoded paths; HEAD; no key ever contains a marketing parameter; every key keeps its own database's `dbN:` prefix; a customised key formula, a FastCGI switch left on a Redis vhost, a Redis switch without its flag, and the switch of one site with the flag of another stay stock

**Installer.** `./test.sh install` copies this repo into a throw-away root under `/tmp` (with `install.sh`'s `/etc/nginx`, `/var/www` and `/root` moved into it), renders GridPane's real templates from this server for a few fake sites, and runs `install.sh` with the server's real `nginx -t` on that copy (`systemctl` only records the call; nginx's own messages go to the throw-away root, not to the server's error log). 133 checks: install and re-install (byte-identical); FastCGI, Redis, legacy-Redis and no-cache sites; site names containing "lua"; fourteen kinds of template or site file the installer must refuse (ten on Redis, four on FastCGI), including GridPane's add-on-domain overlay for both cache types (skipped, old switch removed, nothing else touched); a site file added later (reported as unsafe); a trailing `/` and an invalid site argument; cache-type changes in both directions and to no cache; a clone (reported, rewritten by the next install run); a staging push that leaves the other cache type's flag behind; a flag/switch pair of two sites; an `nginx -t` failure (every file restored, no reload); `--apply-list` (broken and valid); `--disable`; `--uninstall`; and `--status` / `--disable` / update / `--uninstall` with an engine and site files from before the Redis support. Needs root; never touches the live config. Without GridPane's Redis template or the Redis nginx modules it is skipped with a notice.

`./test.sh --cache fcgi`, `--cache redis` or `--cache redis-legacy` runs one cache type only. When GridPane keeps its legacy Redis template next to the current one (`wp-redis-legacy.conf`), the default run tests it too (one database and the `nginx-cache:` prefix for every site, redirects stored as well). On a server without `redis-server` or the Redis nginx modules, the Redis part is skipped with a notice.

**It never touches the live nginx or the sites.** Run it after every GridPane update: if GridPane changes its template, the test catches it.

```bash
./test.sh --site yoursite.com
```

Same, using that site's rendered template (FastCGI or Redis, whichever the site uses).

```bash
./test.sh live yoursite.com /some-page/
./test.sh live yoursite.com /some-page/ --fresh
```

**Live test after installation.** It sends GET requests only, straight to the local nginx (127.0.0.1, so no proxy/CDN in front is involved). It checks:
- HIT with gclid
- a shared entry with the clean URL
- BYPASS for another parameter
- that the missing-trailing-slash and www → apex redirects keep the parameters
- FastCGI: that **purge requests reach nginx the way WordPress sends them**: `GET https://<site>/purge/<probe>/` through normal DNS (often the server's public IP, not 127.0.0.1) must answer 412/200. A 401/403 means Basic Auth or an IP allowlist blocks the server's own requests, so Nginx Helper cannot purge anything (seen for 23 hours after a domain swap). The probe URL is never cached, so nothing is purged.
- Redis: in the site's own database (read with the credentials nginx uses; nothing is printed), that the page is stored under its clean stock key and that no key contains the test's token.

`--fresh` purges that one page first, so the first marketing request is rendered by PHP and the test really checks that no parameter value ends up in the stored HTML. Without it, the page may already be cached and that check proves nothing.

Give it a page with a trailing `/` so the redirect test runs too.

Exit codes: `0` = all PASS, `1` = at least one FAIL (the live test also returns `1` when the site is not switched on); the sandbox and installer tests return `2` on a setup error (e.g. GridPane changed its template).

```bash
./test.sh woo yourshop.com
```

**WooCommerce add-on test** (FastCGI sites only: on Redis, Nginx Helper deletes keys directly and the test could not keep them). Runs inside the site's WordPress (`gp wp … eval-file`). It fires the hook the WooCommerce data store fires after a save, for a real product and variation, and checks which URLs reach Nginx Helper's purger. Every outgoing HTTP request is short-circuited and the unlink method is pointed at a missing file, so **nothing is purged and nothing is saved**. Covered: quantity-only change → no purge; stock status / price / scheduled sale → purge; variation → parent; parent categories; one purge per URL per request; drafts; order and order-note exclusions; direct `_stock_status` / `_price` meta writes (and `_stock` alone → no purge); the home-page setting (both values); Nginx Helper trimmed (no feeds, no AMP fetch: one `purge_url()` = one request; `edit_term`/`delete_term` handler replaced; 176 term edits → one home-page purge). Not covered (needs a real web request): purging after the response.

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
  ├─ GridPane wpfc.conf / wp-redis.conf (server):
  │      query string ≠ ""  → skip_cache=1, skip_reason="-query_string"
  │      + your own skip rules (they set/append skip_reason)
  │      + the site's flag: $gp_mkt_fcgi="<site>" (FastCGI template) or $gp_mkt_redis="<site>" (Redis)
  │
  ├─ location / → try_files … /index.php?$args   (plus any rewrites of your own)
  │
  └─ location ~ \.php$  →  marketing-params-php-context.conf  (the decision happens HERE)
        Redis only, before it: GridPane has set $key "dbN:$scheme$request_method$host$request_uri"
        un-skip only if:  the flag matching this vhost's cache type is set, and the switch
                          ($gp_mkt_sw) is of the same site and cache type
                          Redis: $key has exactly GridPane's stock formula
                          skip_reason == "-query_string" (the only reason)
                          GET/HEAD
                          $args == the original query (nothing rewrote it)
                          ≤ 2048 bytes, ≤ 24 params, ALL of them on the list
        → skip_cache=0, key …$host/about-us/  (= the clean URL's key)
             FastCGI: fastcgi_cache_key   Redis: $key and $escaped_key, same dbN: prefix
        → PHP: REQUEST_URI=/about-us/, QUERY_STRING=""
        → 3xx Location (same host) + "?gclid=1&utm_source=x"
```

| File | Context | Role |
|---|---|---|
| `/etc/nginx/marketing-params/params.list` | include in `map` | **the list you edit** |
| `/etc/nginx/marketing-params/engine.conf` | http | `map` chain, generated by `build-engine.py` |
| `/etc/nginx/marketing-params/php-context.conf` | php location | decision, clean URL for PHP, `Location` fix (shared by both cache types) |
| `/etc/nginx/marketing-params/php-context-fcgi.conf` | php location | FastCGI: `php-context.conf` plus the cache key |
| `/etc/nginx/marketing-params/php-context-redis.conf` | php location | Redis: `php-context.conf` plus the new `$key` / `$escaped_key` |
| `/etc/nginx/marketing-params/limits.env` | – | saved limits |
| `/etc/nginx/conf.d/marketing-params.conf` | http | stub: `include …/engine[.]conf;` |
| `/var/www/<site>/nginx/marketing-params-php-context.conf` | php location | **site switch**: `set $gp_mkt_sw "f:<site>"; include …/php-context-fcgi[.]conf;` (FastCGI) or `set $gp_mkt_sw "r:<site>"; include …/php-context-redis[.]conf;` (Redis) |
| `/var/www/<site>/nginx/marketing-params-skip-fcgi-cache-context.conf` | server | **FastCGI flag**: `set $gp_mkt_fcgi "<site>";` (GridPane includes this pattern only in its FastCGI template) |
| `/var/www/<site>/nginx/marketing-params-skip-redis-cache-context.conf` | server | **Redis flag**: `set $gp_mkt_redis "<site>";` (GridPane includes this pattern only in its Redis template) |

The engine only accepts a flag and a switch of the same site and the same cache type. GridPane's add-on-domain overlays include one site's `*-php-context.conf` in another site's vhost and then set `REQUEST_URI` again, which would hand PHP the original URL on a request that gets cached; with two different names nothing is un-skipped there. A staging push copies the source's files over the target's without deleting any, which can leave a flag of the other cache type next to the switch; such a pair is ignored too, and the next install run removes the extra flag.

### Robustness

- **What GridPane leaves alone, and what it doesn't** (from an audit of GridPane's scripts, gp-cli 1.2.1518 / configs 1.2.90, 2026-10-07):
  - **Kept:** the nightly jobs, `gp update` (including force runs), per-site config regeneration and gridpane-nginx package upgrades never delete or rewrite these custom-named files. They only reset permissions: 640 under `/etc/nginx`, and root:root 644 in the site's `nginx/` dir, where symlinks are deleted, so the switch files must stay regular files. GridPane's update also strips every line containing certain directive names from custom files; `install.sh` refuses to ship such a line.
  - **Removed or replaced:** the per-site files go away when the site is deleted, when it is fully restored from a backup (they return to whatever the backup held), or when another server's site is cloned or migrated into it. A Migrately move does not carry the server-wide engine.
  - **Not under our control:** GridPane can change its own template on any update. Run `./install.sh --status` after GridPane updates (it checks file checksums, the running config, the template hooks and `nginx -t`), and re-run `./install.sh <site>` after a restore or clone.
- **Clone to a server without the engine.** GridPane only copies `/var/www/<site>/nginx/`, and the include uses an exact-name glob. On such a server nothing happens and `nginx -t` passes.
- **Site switched to another cache type or to no cache.** The switch stays inert, because each flag file is only included by its own GridPane template. Run `./install.sh <site>` again to switch it on for the new type; `--status` lists such sites (see [Switching off, removing, changing](#switching-off-removing-changing)).
- **No dependency on GridPane's variables.** The engine never references them and the switches declare them themselves, so changing a site's cache type in the GridPane UI cannot break `nginx -t`, and a server without any Redis site has no `$key` at all.
- **Your own rules win.** This holds for a skip rule that sets/appends `$skip_reason` (as all GridPane KB examples do) and for a rewrite that changes `$args`.
- **Stray copies are ignored.** Files like `engine.old.conf` or `php-context.orig.conf` are not loaded. A missing `params.list` means "nothing counts as marketing".
- **Long queries cost nothing.** Above 2048 bytes (or with a path longer than 8 KB) the chain does not run. It also stops at the first parameter that is not on the list.

## Limitations and behaviour worth knowing

- **Skip rules without a reason.** A rule that sets `$skip_cache 1` **without** setting or appending `$skip_reason` is overridden for marketing-only URLs. Always give a reason: `set $skip_reason "${skip_reason}-my_reason";`.
- **The cache key is forced to the stock formula.** On an enabled FastCGI site, the switch sets `fastcgi_cache_key` to `$scheme$request_method$host$request_uri` for **every** request, so `install.sh` refuses sites with a different key formula (on Redis the key is only rewritten when GridPane's own `$key` has the stock formula). The solution also relies on GridPane's literal `-query_string` reason, so re-run `./test.sh` after GridPane updates.
- **Redirects to other hosts.** Parameters are re-appended only to a `Location` on the same host (or its www/apex twin) or to a `/…` path. A PHP redirect to another domain (alias → primary, S3, payment gateways, an affiliate link that passes the query on) gets nothing appended, so the params are lost there. A same-host redirect that drops the query gets them back. If the target already has its own `utm_*`, the parameters end up duplicated. nginx's own redirects inside the PHP location are sent with a relative `Location` (valid HTTP).
- **Limits.** Defaults are 24 params (empty `&&` segments don't count), 2048 bytes of query and 8 KB of path. Beyond that you get stock BYPASS. Each param of the limit costs 4 nginx variables and GridPane's variables hash limit (2048, set in `common/basics.conf`) is shared by the whole server, so don't raise it without need.
- **Matching.** Names are matched raw (undecoded) and case-insensitively. `utm%5Fsource` is a BYPASS (safe). `UTM_SOURCE` shares the cache entry, although PHP treats it as a different name.
- **Clones and staging on the same server.** GridPane copies the switch, so the copy is enabled straight away. Pushing staging to production re-enables the feature on a production site where it was disabled. `./install.sh --status` lists every site that has the switch.
- **`Set-Cookie` in the cache.** Stock GridPane caches responses together with `Set-Cookie`. PHP never sees the marketing values, so no cookie can be built from them.
- **Purge has to reach the local nginx** (FastCGI) with the same `$scheme` and `$host` as the cache key. If the site's domain points somewhere else (a proxy/CDN) and `/etc/hosts` has no `127.0.0.1` entry for it, Nginx Helper's purge requests never reach this server. With Redis, Nginx Helper talks to Redis directly, so this does not apply.
- **New variables and nginx's variables hash.** Every variable of the server shares one hash whose size GridPane caps (in `common/basics.conf`; GridPane strips that setting from any other file). Too many or too long variable names make every `nginx -t` warn "could not build optimal variables_hash" (harmless, but noisy). The Redis support adds 6 short names; on the test server the engine with a 24-param limit stays below the cap. Raising `MAX_PARAMS` adds 4 variables per param.

## Redis page cache

The same switch works on sites with GridPane's Redis page cache (srcache + redis2, `/etc/nginx/common/<site>-wp-redis.conf`). `./install.sh <site>` detects the cache type and installs the Redis switch and flag. What is different from FastCGI:

- **The key.** GridPane computes `set $key "dbN:$scheme$request_method$host$request_uri";` and `$escaped_key` in the PHP location, before the site's `*-php-context.conf` files. The Redis switch recomputes both after the decision: the clean URL's stock key, with the site's own `dbN:` prefix. The prefix is read from GridPane's `$key` at runtime, never written into a file, because clones and staging copies get a different database but copy `/var/www/<site>/nginx/` verbatim.
- **Fail-safe.** If `$key` does not have exactly the stock formula (GeoIP variant, a customised key), nothing is un-skipped and GridPane's key is left untouched. As for FastCGI, the flag and the switch must carry the same site name (see [How it works](#how-it-works)), and `install.sh` refuses the templates and site files listed under [Installation](#installation), plus the ModSecurity proxy front (no PHP-location hook).
- **Isolation stays.** Entries stay in the site's own database with the site's own prefix, so the per-site purges described in [issue #1](https://github.com/SHIFT64/gridpane-nginx-marketing-params/issues/1) keep working: verified on a live site with Nginx Helper's per-URL purge (a post save), "purge all" (`wp nginx-helper purge-all`, the neighbour site's entries survived with their TTL still counting down) and the GridPane panel (`gp fix cache <site>`). Each of them also cleared entries first created by a gclid/utm request.
- **What srcache never stores.** `Set-Cookie` (unlike FastCGI), 404s, HEAD responses, and redirects (the current template stores status 200 only; the legacy one also stores 301/302/307/308, which works the same way because the `Location` fix is applied per response). The headers this repo adds (`X-Grid-Cache-Mkt`, the `Location` fix) are not stored either; they are added again on every response.
- **Headers.** `X-Grid-SRCache-Fetch: HIT` plus `X-Grid-Cache-Mkt: ignored`; `X-Grid-SRCache-Skip` disappears when nothing is skipped (as for a clean URL).
- **Measured on a live site** (WordPress 7.1, `ab -k -c 8`): `/hello-world/?gclid=…&utm_source=…` went from ~64 req/s (BYPASS to PHP) to ~6,300 req/s (HIT; clean URL ~7,000). With WooCommerce: `/shop/` and a product page with gclid share the clean entry; `?add-to-cart=13&utm_source=x&gclid=…` stays BYPASS and the product lands in the cart.
- **Why PHP must see the clean URL here too.** On that site WordPress core's comment form echoes the query string into `/hello-world/` (the "cancel reply" link), and a WooCommerce product page echoed the gclid once. Without the clean URL the first visitor's gclid would be cached for everyone. With it: 0 occurrences (`./test.sh live <site> /hello-world/ --fresh`).
- **Legacy template** (servers without the `redis-page` instance: prefix `nginx-cache:`, one shared database) works the same way, but there is no per-site isolation to keep.
- **Not behind GridPane's HTTP auth / IP allowlist.** GridPane's `common/acl.conf` uses `satisfy any`: nginx leaves the access phase as soon as the allowlist or the password check passes, and srcache's cache lookup always runs last in that phase, so it is skipped. A protected site (typically a staging copy) reports `X-Grid-SRCache-Fetch: BYPASS` with an empty `X-Grid-SRCache-Skip` on every request and always runs PHP; each nginx worker still writes a page once after a start or reload, but nothing is ever read. This is GridPane/nginx behaviour, not this repo's, and FastCGI is not affected. The parameter handling still works there (verified live: entries under the clean key, nothing rendered from the query), but you can't see cache hits: test on an unprotected site, or protect the site with a per-site `allow …; deny all;` (`*-main-context.conf`, without `satisfy any`) instead of GridPane's HTTP auth. `--status` and `./test.sh live` point this out. The same `satisfy any` lets a cached direct `*.php` URL (one that GridPane's skip rule does not exclude, i.e. not `index.php` or `wp-*.php`) be served to a visitor who passed neither check; on WordPress that is practically nothing, but keep it in mind for custom PHP endpoints.

## Nginx Helper's "Preload Cache"

Nginx Helper (GridPane's fork, unchanged from upstream) has a "Preload Cache" checkbox. Checked on a live Redis site (2026-10-20):

- **It runs once, not after purges.** After the setting is saved, the next request that reaches wp-admin (any admin page, the heartbeat, even an anonymous `admin-ajax.php?action=…`) fetches every URL listed in WordPress's core sitemaps. It does not run after "purge all", a post save or a single-URL purge, so it does not re-warm anything that was purged, and it does nothing for the cross-site FastCGI purge from [issue #1](https://github.com/SHIFT64/gridpane-nginx-marketing-params/issues/1).
- **It works for FastCGI and Redis** (plain GET requests through nginx, via `/etc/hosts` where the domain is listed there). The entries it warms are clean URLs, so gclid/utm variants hit them right away.
- **Caveats.** It requests nothing when core sitemaps are off (Yoast, Rank Math, `blog_public=0`). The URLs are fetched one after another inside that one request, so a large site can keep it busy for minutes, and a page slower than 1 s is probably not stored. It also renders `/cart/`, `/checkout/` and `/my-account/`, which are never cached. Saving the setting writes Nginx Helper's whole effective options array to the database, including the Redis password that otherwise only lives in `wp-config.php`. And if a sitemap answers with something other than XML (Basic Auth, a WAF challenge), the triggering request fails with a PHP error.
- **Recommendation:** leave it off. At most, use it once as a warm-up after a migration, then switch it off again.

## WooCommerce purge add-on

`extras/woo-purge/gp-woo-purge.php`, installed as `wp-content/mu-plugins/gp-woo-purge.php`. It is independent of the nginx part: it fixes stock GridPane purging and also works on sites without the switch. The add-on matters more once marketing URLs are served from the cache: before that, ad traffic always got a fresh page (BYPASS); now it gets the cached one, so a stale cached page reaches more visitors.

**The problem.** GridPane's Nginx Helper (audited: 9.9.10) purges on `transition_post_status`, i.e. when WordPress updates the post. WooCommerce saves a change that touches only prices or stock straight to the database, without `wp_update_post()`, so Nginx Helper never hears about it. This covers the stock change after an order, REST API / ERP updates, imports, bulk edit, variation saves and scheduled sales. The cached product page keeps the old price or "in stock" until it expires: GridPane's FastCGI default is 1 hour, its **Redis default is 30 days**, so on Redis the add-on (or a much shorter cache TTL for the shop) matters even more. Nginx Helper has no WooCommerce-specific code.

On a live shop (WooCommerce 11.1, HPOS off, 5 days of logs): 53 products and 22 variations changed, and **not one** product or category URL was purged. At the same time, every order status change and order note purged the home page, `/author/admin/` and the feeds: about 190 home-page purges a day, on the page most ads land on.

**What it does.**

| Event | Purged |
|---|---|
| price, regular/sale price, sale dates, or **stock status** (in stock ↔ out of stock ↔ backorder) changes, by any code path | home page, shop page, the product, every public archive it is in (categories **including parent categories**, tags, brands, attributes with archives), each with its `/page/N/` URLs (up to 5) |
| `_stock_status` or `_price` **post meta** changes without a WooCommerce save (code calling `update_post_meta()` directly: stock-sync workers, ERP imports, checkout stock checks) | the same as above. WordPress fires `added_post_meta` / `updated_post_meta` only when the stored value really changes, so rewriting the same value purges nothing. Filter: `gp_woo_purge_meta_keys` |
| stock **quantity** changes but the status stays the same | nothing (a deliberate choice: "12 left" → "11 left" is not worth a purge) |
| variation changes | its parent product (and the parent's archives) |
| order, refund or coupon saved / status changed / note added | nothing any more (excluded from Nginx Helper's triggers) |
| any term edited or deleted (categories, tags, brands, menus…) | **one** home-page purge per request, instead of Nginx Helper's home page + 3 feeds per term (an hourly import of 176 categories made 704 requests); nothing when the home page is switched off |

**It also trims Nginx Helper's own purges** (in memory, for every request; nothing is saved):

- **No feed purges.** GridPane's templates never cache `/feed/` (it is in the `$skip_cache` rule), but Nginx Helper purges `feed/`, `feed/atom/` and `feed/rdf/` next to every URL: 3 of every 4 purge requests could never clear anything. Filter: `gp_woo_purge_disable_feed_purges`.
- **No AMP fetches.** GridPane's fork "purges" the AMP version by **requesting the page** `…/amp/` instead of `/purge/…/amp/`: a full PHP render of a 404 for every purged URL whenever the purge runs during a page view. Seen on a live shop: ~70 a day each of `/amp/`, `/author/admin/amp/` and `/?post_type=shop_order&p=…/amp/`. Switched off unless an AMP plugin is active (filter `gp_woo_purge_disable_amp_purges`).
- **After the response.** Every purge is a blocking HTTPS request from the server to itself (~16 ms each, measured). The add-on calls `fastcgi_finish_request()` first, so a checkout, a payment callback or an admin save no longer waits for 10–30 loopback requests. WP-CLI runs are unchanged.

**Home page setting.** By default the home page is purged with every product change and once per request for term edits. A shop whose home page shows no prices or stock (e.g. a B2B catalogue with its own navigation cache) can switch that off:

```bash
gp wp yourshop.com option update gp_woo_purge_home no      # back on: option delete gp_woo_purge_home
```

- Every URL is purged once per request, at `shutdown`. A checkout that changes ten products purges the home and shop pages once.
- It calls Nginx Helper's own purger, so it uses the site's purge method and appears in Nginx Helper's log. Without Nginx Helper, or with purging switched off, it does nothing.
- On Redis page-cache sites it works the same way: each purge is then a `DEL` in the site's own Redis database instead of an HTTP request.
- Purging the clean URL also clears its gclid/utm variants, because they share its cache entry.
- Only published products. Drafts and private products have no cached page.
- `/page/N/` URLs are estimated from the product count and `loop_shop_per_page`. A page that does not exist costs one purge request that finds nothing; archives with more than 5 pages keep pages 6+ until they expire.
- Filters: `gp_woo_purge_home`, `gp_woo_purge_disable_feed_purges`, `gp_woo_purge_disable_amp_purges`, `gp_woo_purge_trigger_props`, `gp_woo_purge_excluded_post_types`, `gp_woo_purge_max_pages`, `gp_woo_purge_urls` (see the file header).
- The trimming relies on internals of GridPane's Nginx Helper fork (`$nginx_helper_admin->options`, `$nginx_purger`), checked with guards; `./test.sh woo` tells you if a new Nginx Helper version breaks it.

**Not handled yet.**
- **Bulk syncs (ERP, feeds, imports).** A sync that changes hundreds of prices or stock statuses in one request purges every affected product page and archive page once (the first product costs about 20 purge requests on a typical shop, each further one its own page plus any archives not purged yet). These are cheap local GETs, so a few hundred are fine. For shops whose sync changes most of the catalogue at once, a "purge everything above N products" threshold would be better. Check how the shop's sync behaves before installing.
- **Editing a term does not purge its archive** (e.g. `/kategoria/x/` after a rename), as in stock Nginx Helper; only the home page, once per request. Purging archives per edited term would turn an import that rewrites unchanged categories into hundreds of empty purges, so it waits for a queued design (e.g. Action Scheduler, one run after the import). Fix such imports at the source: write only real changes.
- Moving an order to the trash still purges the home page (Nginx Helper's trash hook has no post-type filter).
- Multilingual sites (WPML, Polylang): only the URLs WordPress returns for the current language are purged.
- GridPane's scripts were audited for the nginx files, not for `mu-plugins/`. Check `./install.sh --status` after a restore or clone, as for the switch files.

## GridPane Lua (hidden) vs this repo

GridPane ships an undocumented feature that does something similar: `gp stack nginx -lua query-param-cache …` and `gp site <site> -lua-query-param-cache on`. We compared the two on the same WooCommerce site. The same scripted matrix ran in three modes, one after another: stock, this repo, GridPane Lua. Details:
- date: 2026-10-06
- versions: GridPane nginx 1.30.4, gp-cli 1.2.1518
- the cookie and redirect cases used small test PHP scripts

GridPane may change its feature, so treat the Lua column as a snapshot of that version. Full findings, evidence and a reproduction script: [docs/gridpane-lua-query-param-cache.md](docs/gridpane-lua-query-param-cache.md).

| | GridPane Lua (hidden) | This repo |
|---|---|---|
| Setup | `gp stack nginx -lua query-param-cache ensure-ready`, then `gp site <site> -lua-query-param-cache on`. Needs GridPane nginx 1.28.x/1.30.x. nginx reloads; it restarts fully only if LuaJIT is not loaded yet (on the test server it already was, so it reloaded). | `./install.sh <site>` (`nginx -t`, reload, rollback) |
| Docs / maintenance | Only a changelog entry (v1.2.1514, "documentation coming soon"); maintained by GridPane | This README + tests; maintained by you |
| Page-cache types | FastCGI, Redis, proxy-Redis | FastCGI, Redis |
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
- **GridPane Lua** is the better fit for proxy-Redis (ModSecurity) caching or when you want everything vendor-managed. Use it only if your pages provably don't render query parameters into HTML or cookies. To check, request a page with a unique `gclid`, then the clean URL, and search the HTML and `Set-Cookie`. (srcache never stores `Set-Cookie`, so on Redis only HTML echoes are a risk; WordPress core's comment form is one.)
- **This repo** is the better fit for FastCGI and Redis sites whose theme or plugins echo parameters (WooCommerce and WordPress's comment form did), or when you want the broader default list. In exchange you accept: PHP never sees listed params, custom skip rules need a reason, and you maintain it yourself.
- **Never run both on the same site.**

## Repository layout

```
install.sh                      install / enable / disable / apply list / status / uninstall / add-on
test.sh                         tests: sandbox + installer (default), `install`, `live <site> [/path/] [--fresh]` or `woo <site>`
build-engine.py                 engine.conf generator
server/marketing-params/        → /etc/nginx/marketing-params/  (php-context.conf, php-context-fcgi.conf, php-context-redis.conf, params.list)
server/stubs/                   → /etc/nginx/conf.d/marketing-params.conf
site/                           → /var/www/<site>/nginx/  (switch + flag; FastCGI or Redis variant)
extras/woo-purge/               → /var/www/<site>/htdocs/wp-content/mu-plugins/gp-woo-purge.php
tests/                          sandbox_test.py, install_test.py, live_test.py, fake_php.py (PHP-FPM stand-in), woo_purge_test.php
docs/                           gridpane-lua-query-param-cache.md (GridPane's Lua feature: how it works, findings)
```
