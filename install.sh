#!/usr/bin/env bash
# Marketing params ignored by the GridPane FastCGI or Redis page cache.
#
#   ./install.sh                      install / update the server-wide engine (your list is kept)
#   ./install.sh site.com [site2...]  ...and switch it ON for the given site(s) (FastCGI or Redis
#                                     page cache, detected per site); on a TTY it asks
#                                     whether to add the WooCommerce purge add-on to Woo sites
#   ./install.sh --full site.com      engine + switch + WooCommerce purge add-on, no questions
#   ./install.sh --woo-purge site.com         only the WooCommerce purge add-on (mu-plugin)
#   ./install.sh --remove-woo-purge site.com  remove the add-on from a site
#   ./install.sh --disable site.com   switch it OFF for a site (the add-on stays)
#   ./install.sh --apply-list         after editing params.list: nginx -t + reload, or restore
#                                     the last good list if the test fails
#   ./install.sh --uninstall          remove everything (all sites + engine)
#   ./install.sh --status             show what is installed / enabled / suspicious
#
# Installs first ask GitHub for the newest release tag (vX.Y.Z, refs only). If this checkout is
# older, the script stops before changing anything, fast-forwards to that release, runs
# ./test.sh and starts again with the same arguments only if the test passes.
# Skip with GP_MKT_NO_UPDATE_CHECK=1 (offline server, testing a local change).
#
# Limits (kept in /etc/nginx/marketing-params/limits.env and reused on every update):
#   MAX_PARAMS=32 MAX_LEN=4096 ./install.sh
#
# Never touches GridPane-managed files. Always runs `nginx -t` before reloading
# and restores the previous files if anything fails.
set -Eeuo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DIR=/etc/nginx/marketing-params
STUB_HTTP=/etc/nginx/conf.d/marketing-params.conf
SWITCH=marketing-params-php-context.conf              # the site's switch (php location; FastCGI or Redis variant)
FLAG=marketing-params-skip-fcgi-cache-context.conf    # "this vhost is FastCGI-cached" flag
FLAG_REDIS=marketing-params-skip-redis-cache-context.conf   # "this vhost is Redis-cached" flag
# Sources in site/: the FastCGI switch and both flags have the installed names; @SITE@ becomes the
# site's name (the engine only accepts a flag and a switch of the same site).
SRC_SWITCH_FCGI=marketing-params-php-context.conf
SRC_SWITCH_REDIS=marketing-params-php-context.redis.conf
ENGINE_FILES=(engine.conf php-context.conf php-context-fcgi.conf php-context-redis.conf README.md limits.env)
# files of the round-1 layout, removed on update
OLD_FILES=(/etc/nginx/extra.d/00-marketing-params-skip-fcgi-cache-context.conf "$DIR/skip-context.conf")
OLD_SWITCH=marketing-params-main-context.conf
STOCK_KEY='fastcgi_cache_key "$scheme$request_method$host$request_uri";'
WOO_SRC="$HERE/extras/woo-purge/gp-woo-purge.php"
WOO_NAME=gp-woo-purge.php

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

update_check() {                 # update_check "$@": stop, move to the newest release and re-run
  local head here refs tag sha
  [[ -z "${GP_MKT_NO_UPDATE_CHECK:-}" ]] || return 0
  git -C "$HERE" rev-parse --git-dir >/dev/null 2>&1 || { echo "update check: $HERE is not a git checkout git can use (or git refuses its owner) - skipped"; return 0; }
  git -C "$HERE" remote get-url origin >/dev/null 2>&1 || { echo "update check: no 'origin' remote - skipped"; return 0; }
  head="$(git -C "$HERE" rev-parse HEAD)"
  here="$(git -C "$HERE" describe --tags --always 2>/dev/null)"
  if ! refs="$(timeout 15 git -C "$HERE" ls-remote --tags origin 'refs/tags/v*' 2>/dev/null)"; then
    echo "!! update check: origin unreachable - continuing with this copy ($here)" >&2
    return 0
  fi
  tag="$(sed -nE 's#^[0-9a-f]+[[:space:]]+refs/tags/(v[0-9]+\.[0-9]+\.[0-9]+)(\^\{\})?$#\1#p' <<<"$refs" | sort -uV | tail -1)"
  [[ -n "$tag" ]] || { echo "update check: no vX.Y.Z release on origin - skipped"; return 0; }
  # an annotated tag is listed twice; the peeled ^{} line holds the commit
  sha="$(awk -v t="refs/tags/$tag^{}" '$2 == t { print $1 }' <<<"$refs")"
  [[ -n "$sha" ]] || sha="$(awk -v t="refs/tags/$tag" '$2 == t { print $1 }' <<<"$refs")"
  if [[ "$sha" == "$head" ]] || git -C "$HERE" merge-base --is-ancestor "$sha" HEAD 2>/dev/null; then
    echo "update check: up to date (newest release $tag, this copy: $here)"; return 0
  fi
  echo "update check: release $tag is newer than this copy ($here) - nothing changed yet"
  [[ -z "${GP_MKT_UPDATED:-}" ]] || { echo "!! still not up to date after updating - stopping" >&2; exit 1; }
  if [[ -n "$(git -C "$HERE" status --porcelain --untracked-files=no)" ]]; then
    echo "!! $HERE has local changes - not updating. Commit/stash them, or run with GP_MKT_NO_UPDATE_CHECK=1" >&2
    exit 1
  fi
  if ! git -C "$HERE" fetch -q --no-tags origin "refs/tags/$tag:refs/tags/$tag" \
     || ! git -C "$HERE" merge -q --ff-only "$tag"; then
    echo "!! could not fast-forward to $tag (local commits?) - nothing was changed on the server" >&2
    exit 1
  fi
  git -C "$HERE" log --oneline "$head..HEAD" | sed 's/^/  + /'
  echo "updated - running the sandbox test (./test.sh) before installing the new version"
  if ! out="$("$HERE/test.sh" 2>&1)"; then
    tail -15 <<<"$out" >&2
    echo "!! ./test.sh fails on the new version - nothing was installed or changed (except the git checkout)" >&2
    exit 1
  fi
  tail -1 <<<"$out"
  echo "starting again"
  GP_MKT_UPDATED=1 exec "$HERE/install.sh" "$@"
}
case "${1:-}" in --full|--woo-purge|[^-]*|"") update_check "$@" ;; esac

BACKUP=""
CHANGED=()
key() { echo "$1" | tr / _; }
backup() {                       # remember a file's ORIGINAL state (first touch only)
  local f
  for f in "${CHANGED[@]}"; do [[ "$f" == "$1" ]] && return 0; done
  [[ -n "$BACKUP" ]] || BACKUP="$(mktemp -d /root/.gp-marketing-params-backup.XXXXXX)"
  CHANGED+=("$1")
  if [[ -e "$1" ]]; then cp -a "$1" "$BACKUP/$(key "$1")"; fi
  return 0
}
put() { backup "$2"; install -m "$3" "$1" "$2"; }       # put <src> <dst> <mode>
del() { [[ -e "$1" ]] || return 0; backup "$1"; rm -f "$1"; }
cleanup() { if [[ -n "$BACKUP" ]]; then rm -rf "$BACKUP"; fi; BACKUP=""; CHANGED=(); return 0; }
rollback() {
  # the ERR trap is inherited by $(...) subshells (set -E): only the main shell may restore
  [[ $BASHPID -eq $$ ]] || exit 1
  trap - ERR
  echo "!! $1 - restoring previous files" >&2
  local f
  for f in "${CHANGED[@]}"; do
    if [[ -e "$BACKUP/$(key "$f")" ]]; then cp -a "$BACKUP/$(key "$f")" "$f"; else rm -f "$f"; fi
  done
  cleanup
  nginx -t >/dev/null 2>&1 && echo "previous configuration restored (nginx -t OK)" >&2
  exit 1
}
reload() {
  local out
  if out="$(nginx -t 2>&1)"; then
    if grep -q '\[warn\]' <<<"$out"; then echo "nginx -t warnings:"; grep '\[warn\]' <<<"$out"; fi
    systemctl reload nginx
    trap - ERR                     # the running nginx has the new files now: never roll back after this
    echo "nginx -t OK, nginx reloaded"
    if [[ -e "$DIR/params.list" ]]; then cp -a "$DIR/params.list" "$DIR/params.list.last-good" || true; fi
    if [[ -e "$DIR/engine.conf" ]]; then write_manifest || echo "!! could not write $MANIFEST" >&2; fi
    cleanup
  else
    echo "$out" >&2
    rollback "nginx -t failed"
  fi
}

site_name_ok() { [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; }
redis_tpl() {                    # the Redis page-cache template the site's vhost includes
  [[ -r "/etc/nginx/sites-available/$1" ]] || return 0
  sed -nE 's#^[[:space:]]*include (/etc/nginx/common/[^;[:space:]]*-wp-redis\.conf);.*#\1#p' \
    "/etc/nginx/sites-available/$1" | head -1
}
cache_type() {                   # fcgi | redis | "" (no page cache, proxy, or no vhost)
  local v="/etc/nginx/sites-available/$1"
  [[ -r "$v" ]] || return 0
  if grep -qs -- "-wpfc.conf" "$v"; then echo fcgi; elif [[ -n "$(redis_tpl "$1")" ]]; then echo redis; fi
}
switch_type() { [[ -e "$1" ]] || return 0; if grep -qs 'php-context-redis' "$1"; then echo redis; else echo fcgi; fi; }
first_line() { awk -v p="$1" 'index($0, p) { print NR; exit }' "$2"; }   # never fails (set -e/ERR safe)
site_of() {                      # the site name a flag or switch carries ("" = a file of an older version)
  sed -nE 's/^[[:space:]]*set \$gp_mkt_(fcgi|redis|sw)[[:space:]]+"([fr]:)?([^"]*)";.*/\3/p' "$1" 2>/dev/null | head -1
}
# Every check below prints what is wrong (nothing = OK) and must be safe under set -e and the
# ERR trap, because it runs inside $(...).
#
# template_problem <site> <fcgi|redis>: the override assumes GridPane's stock template.
REDIS_KEY_RE='^[[:space:]]*set \$key "[A-Za-z0-9_-]+:\$scheme\$request_method\$host\$request_uri";'
template_problem() {
  local s="$1" t esc php var
  if [[ "$2" == fcgi ]]; then
    t="/etc/nginx/common/$s-wpfc.conf"
    var="/etc/nginx/common/$s-fcgi-cache-var.conf"
    [[ -e "/etc/nginx/common/_custom/$s-fcgi-cache-var.conf" ]] && var="/etc/nginx/common/_custom/$s-fcgi-cache-var.conf"
    [[ -e "$t" ]] || { echo "no $t"; return; }
    if [[ "$(grep -csE '^[[:space:]]*fastcgi_cache_key[[:space:]]' "$var" || true)" -ne 1 ]] \
       || ! grep -qsE '^[[:space:]]*fastcgi_cache_key[[:space:]]+"\$scheme\$request_method\$host\$request_uri";' "$var"; then
      echo "customised FastCGI cache key; this tool sets GridPane's stock one: $STOCK_KEY"; return
    fi
    grep -qF "include /var/www/$s/nginx/*skip-fcgi-cache-context.conf;" "$t" \
      || { echo "no *skip-fcgi-cache-context.conf include hook"; return; }
  else
    t="$(redis_tpl "$s")"
    [[ -n "$t" && -e "$t" ]] || { echo "no Redis template found in the vhost"; return; }
    if [[ "$(grep -cE '^[[:space:]]*set \$key ' "$t" || true)" -ne 1 || "$(grep -cE "$REDIS_KEY_RE" "$t" || true)" -ne 1 ]]; then
      echo "cache key is not 'set \$key \"<prefix>:\$scheme\$request_method\$host\$request_uri\";' (GeoIP or customised key?)"; return
    fi
    esc="$(first_line 'set_escape_uri $escaped_key $key;' "$t")"
    php="$(first_line "include /var/www/$s/nginx/*-php-context.conf;" "$t")"
    [[ -n "$esc" && -n "$php" && "$esc" -lt "$php" ]] \
      || { echo "\$escaped_key is not computed before the site's *-php-context.conf include"; return; }
    grep -qF "include /var/www/$s/nginx/*-skip-redis-cache-context.conf;" "$t" \
      || { echo "no *-skip-redis-cache-context.conf include hook"; return; }
    grep -qF 'srcache_fetch GET /redis-fetch $key;' "$t" && grep -qF 'srcache_store PUT /redis-store key=$escaped_key;' "$t" \
      || { echo "srcache_fetch / srcache_store lines changed"; return; }
  fi
  grep -qsE '\$cache_uri|_by_lua|lua-qp-cache' "$t" && { echo "GridPane's Lua query-param cache is on"; return; }
  grep -qsF 'gridpane-managed: altdomain-attach' "$t" && { echo "GridPane's add-on-domain overlay"; return; }
  if grep -E 'include /var/www/[^/]+/nginx/\*-php-context\.conf;' "$t" | grep -qvF "/var/www/$s/nginx/"; then
    echo "it includes another site's *-php-context.conf (add-on-domain overlay)"; return
  fi
  grep -qF "include /var/www/$s/nginx/*-php-context.conf;" "$t" || { echo "no *-php-context.conf include hook"; return; }
  if awk '/include[^;]*php-context\.conf;/ { seen = 1; next }
          seen && (/^[[:space:]]*fastcgi_param[[:space:]]+REQUEST_URI/ || /include[^;]*fastcgi(_params|\.conf)/) { f = 1 }
          END { exit !f }' "$t"; then
    echo "it sets REQUEST_URI again after a *-php-context.conf include"; return
  fi
  grep -qF 'if ($query_string != "")' "$t" && grep -qF -- '-query_string"' "$t" \
    || { echo "query-string skip rule missing"; return; }
}
# files_problem <site>: the site's other *-php-context.conf files (and what they include) run after
# the switch; none of them may change the cache key or REQUEST_URI again.
files_problem() {
  local f g p inc list
  for f in /var/www/"$1"/nginx/*-php-context.conf; do
    [[ -e "$f" && "${f##*/}" != "$SWITCH" ]] || continue
    list=("$f")
    inc="$(sed -nE 's/^[[:space:]]*include[[:space:]]+([^;[:space:]]+)[[:space:]]*;.*/\1/p' "$f")"
    for p in $inc; do
      [[ "$p" == /* ]] || p="/etc/nginx/$p"
      for g in $p; do [[ -f "$g" ]] && list+=("$g"); done
    done
    if grep -qsE '^[^#]*(\$(key|escaped_key)([^A-Za-z0-9_]|$)|REQUEST_URI|fastcgi_cache_key)' "${list[@]}"; then
      echo "${f##*/} changes the cache key or REQUEST_URI after the switch"; return
    fi
  done
}

# GridPane's gpupdate deletes every line containing a "baseline" directive name
# (today: the variables hash size) from all files under /etc/nginx and /var/www/*/nginx
# (gridpane::check_nginx_baseline_directive::safety). Never ship such a token.
baseline_guard() {
  local tokens f
  tokens="$(sed -nE 's/^[[:space:]]*([a-z_0-9]+)[[:space:]].*;.*/\1/p' /etc/nginx/common/basics.conf 2>/dev/null | sort -u)"
  [[ -n "$tokens" ]] || return 0
  for f in "$@"; do
    if grep -qwFf <(echo "$tokens") "$f"; then
      echo "!! $f contains a directive name GridPane strips from custom files ($(grep -owFf <(echo "$tokens") "$f" | sort -u | tr '\n' ' '))" >&2
      return 1
    fi
  done
}
MANIFEST="$DIR/.manifest.sha256"
write_manifest() {               # only the files that exist (an older engine has fewer)
  local f l=()
  for f in "${ENGINE_FILES[@]}"; do [[ -e "$DIR/$f" ]] && l+=("$f"); done
  (cd "$DIR" && sha256sum "${l[@]}" > "$MANIFEST" && chmod 640 "$MANIFEST")
}
install_engine() {
  local mp=24 ml=2048
  [[ -e "$DIR/limits.env" ]] && { mp="$(sed -n 's/^MAX_PARAMS=//p' "$DIR/limits.env")"; ml="$(sed -n 's/^MAX_LEN=//p' "$DIR/limits.env")"; }
  mp="${MAX_PARAMS:-$mp}"; ml="${MAX_LEN:-$ml}"
  mkdir -p "$DIR"
  python3 "$HERE/build-engine.py" "$mp" "$ml" > "$BACKUP_TMP/engine.conf"
  printf 'MAX_PARAMS=%s\nMAX_LEN=%s\n' "$mp" "$ml" > "$BACKUP_TMP/limits.env"
  baseline_guard "$BACKUP_TMP/engine.conf" "$HERE/server/marketing-params/php-context.conf" "$HERE/README.md" \
    "$HERE/server/marketing-params/php-context-fcgi.conf" "$HERE/server/marketing-params/php-context-redis.conf" \
    "$HERE/server/marketing-params/params.list" "$HERE/server/stubs/conf.d-marketing-params.conf" \
    "$HERE/site/$SRC_SWITCH_FCGI" "$HERE/site/$FLAG" "$HERE/site/$SRC_SWITCH_REDIS" "$HERE/site/$FLAG_REDIS" \
    || rollback "refusing to install"
  put "$BACKUP_TMP/engine.conf" "$DIR/engine.conf" 640
  put "$BACKUP_TMP/limits.env" "$DIR/limits.env" 640
  put "$HERE/server/marketing-params/php-context.conf" "$DIR/php-context.conf" 640
  put "$HERE/server/marketing-params/php-context-fcgi.conf" "$DIR/php-context-fcgi.conf" 640
  put "$HERE/server/marketing-params/php-context-redis.conf" "$DIR/php-context-redis.conf" 640
  put "$HERE/README.md" "$DIR/README.md" 640
  if [[ -e "$DIR/params.list" ]]; then
    echo "kept existing $DIR/params.list (your edits are preserved)"
  else
    put "$HERE/server/marketing-params/params.list" "$DIR/params.list" 640
    echo "installed $DIR/params.list"
  fi
  put "$HERE/server/stubs/conf.d-marketing-params.conf" "$STUB_HTTP" 640
  local f; for f in "${OLD_FILES[@]}" /var/www/*/nginx/$OLD_SWITCH; do del "$f"; done
  echo "engine: max $mp params, max $ml bytes"
}

put_site() {                     # put_site <source in site/> <site> <installed name>: only if it differs
  local dst="/var/www/$2/nginx/$3"
  sed "s/@SITE@/$2/" "$HERE/site/$1" > "$BACKUP_TMP/site.tmp"
  cmp -s "$BACKUP_TMP/site.tmp" "$dst" || put "$BACKUP_TMP/site.tmp" "$dst" 644
}
skip_site() {                    # skip_site <site> <reason>: say why, and remove a switch left from before
  local d="/var/www/$1/nginx" f had=""
  for f in "$SWITCH" "$FLAG" "$FLAG_REDIS"; do [[ -e "$d/$f" ]] && { had=1; del "$d/$f"; }; done
  echo "!! $1: $2 - skipped${had:+ (its old switch files were removed)}" >&2
}

enable_site() {
  local s="$1" type why
  [[ -d "/var/www/$s/nginx" ]] || rollback "no such site: /var/www/$s/nginx"
  type="$(cache_type "$s")"
  if [[ -z "$type" ]]; then skip_site "$s" "uses neither GridPane FastCGI nor Redis page caching"; return 0; fi
  why="$(template_problem "$s" "$type")"
  if [[ -n "$why" ]]; then
    skip_site "$s" "GridPane's $([[ $type == fcgi ]] && echo FastCGI || echo Redis) page-cache template is not the stock one ($why)"
    return 0
  fi
  why="$(files_problem "$s")"
  if [[ -n "$why" ]]; then skip_site "$s" "$why"; return 0; fi
  if [[ $type == fcgi ]]; then
    put_site "$FLAG" "$s" "$FLAG"
    put_site "$SRC_SWITCH_FCGI" "$s" "$SWITCH"
    del "/var/www/$s/nginx/$FLAG_REDIS"
    echo "ON  for $s (FastCGI)"
  else
    put_site "$FLAG_REDIS" "$s" "$FLAG_REDIS"
    put_site "$SRC_SWITCH_REDIS" "$s" "$SWITCH"
    del "/var/www/$s/nginx/$FLAG"
    echo "ON  for $s (Redis)"
  fi
}

# Rewrites every installed switch and flag from this version, with its own site's name (files of
# an older version, clones and staging copies), keeping each file's type. Switches nothing on or off.
refresh_sites() {
  local d s n
  for d in /var/www/*/nginx; do
    [[ -d "$d" ]] || continue
    s="$(basename "$(dirname "$d")")"
    site_name_ok "$s" || continue
    n=${#CHANGED[@]}
    case "$(switch_type "$d/$SWITCH")" in       # a flag of the other cache type is never needed
      redis) [[ -L "$d/$SWITCH" ]] || put_site "$SRC_SWITCH_REDIS" "$s" "$SWITCH"; del "$d/$FLAG" ;;
      fcgi)  [[ -L "$d/$SWITCH" ]] || put_site "$SRC_SWITCH_FCGI" "$s" "$SWITCH"; del "$d/$FLAG_REDIS" ;;
    esac
    if [[ -e "$d/$FLAG" && ! -L "$d/$FLAG" ]]; then put_site "$FLAG" "$s" "$FLAG"; fi
    if [[ -e "$d/$FLAG_REDIS" && ! -L "$d/$FLAG_REDIS" ]]; then put_site "$FLAG_REDIS" "$s" "$FLAG_REDIS"; fi
    if [[ ${#CHANGED[@]} -gt $n ]]; then echo "updated the switch files of $s to this version"; fi
  done
  return 0
}

# WooCommerce purge add-on: a mu-plugin, independent of the nginx part (it also helps sites
# without the switch). Owned by the site user, like the rest of wp-content.
woo_dir() { echo "/var/www/$1/htdocs/wp-content/mu-plugins"; }
has_woo() { [[ -d "/var/www/$1/htdocs/wp-content/plugins/woocommerce" ]]; }
woo_install() {
  local s="$1" d owner
  d="$(woo_dir "$s")"
  [[ -d "/var/www/$s/htdocs/wp-content" ]] || rollback "no such site: /var/www/$s/htdocs/wp-content"
  if ! has_woo "$s"; then echo "!! $s has no WooCommerce plugin - WooCommerce purge add-on skipped" >&2; return 0; fi
  php -l "$WOO_SRC" >/dev/null || rollback "PHP syntax check of $WOO_SRC failed"
  owner="$(stat -c %U:%G "/var/www/$s/htdocs/wp-content")"
  [[ -d "$d" ]] || install -d -o "${owner%:*}" -g "${owner#*:}" -m 755 "$d"
  backup "$d/$WOO_NAME"
  install -o "${owner%:*}" -g "${owner#*:}" -m 644 "$WOO_SRC" "$d/$WOO_NAME"
  [[ -d "/var/www/$s/htdocs/wp-content/plugins/nginx-helper" ]] \
    || echo "!! $s has no Nginx Helper plugin - the add-on stays inactive until it is installed" >&2
  echo "WOO purge add-on ON for $s ($d/$WOO_NAME)"
}
woo_ask() {                      # interactive installs only; --full answers yes for every site
  local s="$1" a
  has_woo "$s" || return 1
  [[ -e "$(woo_dir "$s")/$WOO_NAME" ]] && return 0     # already there: keep it updated
  [[ -t 0 && -t 1 ]] || return 1
  read -r -p "  $s runs WooCommerce. Also install the WooCommerce purge add-on (see README)? [Y/n] " a
  [[ -z "$a" || "$a" == [Yy]* ]]
}

status() {
  local ok=1 f s d r a b t w fl bad any=0
  echo "engine dir:   $DIR"
  for f in "$STUB_HTTP" "$DIR/engine.conf" "$DIR/php-context.conf" "$DIR/php-context-fcgi.conf" "$DIR/php-context-redis.conf" "$DIR/params.list"; do
    if [[ -e "$f" ]]; then echo "  ok      $f"; else echo "  MISSING $f"; ok=0; fi
  done
  [[ -e "$DIR/limits.env" ]] && echo "  limits: $(tr '\n' ' ' < "$DIR/limits.env")"
  echo "  list:   $(grep -cE '^[^#[:space:]].*;' "$DIR/params.list" 2>/dev/null || echo 0) entries"
  if [[ -e "$MANIFEST" ]]; then
    bad="$(cd "$DIR" && sha256sum -c --quiet "$MANIFEST" 2>/dev/null | cut -d: -f1 | tr '\n' ' ')" || true
    if [[ -n "$bad" ]]; then echo "  !! changed since install: $bad(re-run ./install.sh)"; ok=0; else echo "  ok      files unchanged since install"; fi
  fi
  if ! baseline_guard "$DIR"/engine.conf "$DIR"/php-context*.conf "$DIR"/params.list "$DIR"/README.md 2>/dev/null; then
    echo "  !! an installed file contains a directive name GridPane strips (re-run ./install.sh)"; ok=0
  fi
  if ! grep -qE '^[[:space:]]*include /etc/nginx/conf\.d/\*\.conf;' /etc/nginx/nginx.conf; then
    echo "  !! nginx.conf no longer includes conf.d/*.conf - the engine is not loaded"; ok=0
  elif [[ "$(nginx -T 2>/dev/null | grep -c '^# configuration file /etc/nginx/marketing-params/engine.conf')" -eq 0 ]]; then
    echo "  !! engine.conf is not part of the running config (nginx -T)"; ok=0
  fi
  for f in "${OLD_FILES[@]}" /var/www/*/nginx/$OLD_SWITCH; do
    [[ -e "$f" ]] && { echo "  OLD LAYOUT LEFTOVER: $f (run ./install.sh to clean up)"; ok=0; }
  done
  echo "sites ON (incl. clones/staging that copied the switch):"
  for d in /var/www/*/nginx; do
    [[ -e "$d/$SWITCH" || -e "$d/$FLAG" || -e "$d/$FLAG_REDIS" ]] || continue
    s="$(basename "$(dirname "$d")")"
    if [[ -L "$d/$SWITCH" || -L "$d/$FLAG" || -L "$d/$FLAG_REDIS" ]]; then echo "  $s   !! switch files must be regular files (GridPane deletes symlinks there)"; ok=0; continue; fi
    t="$(cache_type "$s")"; w="$(switch_type "$d/$SWITCH")"
    if [[ -z "$w" ]]; then echo "  $s   !! a flag without the switch (inactive) - re-run ./install.sh $s or --disable it"; ok=0; continue; fi
    if [[ -z "$t" ]]; then echo "  $s   !! switch present, but the site has no FastCGI or Redis page cache (inactive) - re-run ./install.sh $s (it removes the switch) or --disable it"; ok=0; continue; fi
    if [[ "$w" != "$t" ]]; then
      echo "  $s   !! $([[ $w == fcgi ]] && echo FastCGI || echo Redis) switch, but the site now uses $([[ $t == fcgi ]] && echo FastCGI || echo Redis) page cache (inactive) - re-run ./install.sh $s"; ok=0; continue
    fi
    fl="$FLAG"; [[ $t == redis ]] && fl="$FLAG_REDIS"
    if [[ ! -e "$d/$fl" ]]; then echo "  $s   !! switch without $fl (inactive) - re-run ./install.sh $s or --disable it"; ok=0; continue; fi
    a="$(site_of "$d/$fl")"; b="$(site_of "$d/$SWITCH")"
    if [[ -z "$a" || -z "$b" ]]; then echo "  $s   !! switch files of an older version (inactive with this engine) - re-run ./install.sh"; ok=0; continue; fi
    if [[ "$a" != "$b" ]]; then echo "  $s   !! flag ($a) and switch ($b) belong to different sites (inactive) - re-run ./install.sh $s"; ok=0; continue; fi
    r="$(files_problem "$s")"
    if [[ -n "$r" ]]; then echo "  $s   !! $r: the feature is ON and NOT safe - run ./install.sh $s (it switches the site off) or --disable it"; ok=0; continue; fi
    r="$(template_problem "$s" "$t")"
    if [[ -n "$r" ]]; then echo "  $s   !! GridPane's template is not the stock one ($r) - run ./install.sh $s (it switches the site off if needed) and ./test.sh --site $s"; ok=0; continue; fi
    if [[ "$a" != "$s" ]]; then echo "  $s   !! switch files copied from $a (clone/staging copy) - re-run ./install.sh $s so they carry its own name"; ok=0; continue; fi
    if [[ $t == fcgi && ( -e /etc/nginx/common/_custom/wpfc.conf || -e "/etc/nginx/common/_custom/$s-wpfc.conf" ) ]] \
       || [[ $t == redis && ( -e /etc/nginx/common/_custom/wp-redis.conf || -e "/etc/nginx/common/_custom/$s-wp-redis.conf" ) ]]; then
      echo "  $s   (a common/_custom template override exists; its hooks were checked)"; fi
    echo "  $s   ($([[ $t == fcgi ]] && echo FastCGI || echo Redis))"
  done
  echo "WooCommerce purge add-on (mu-plugins/$WOO_NAME):"
  for f in /var/www/*/htdocs/wp-content/mu-plugins/$WOO_NAME; do
    [[ -e "$f" ]] || continue
    any=1; s="${f#/var/www/}"; s="${s%%/*}"
    if [[ ! -d "/var/www/$s/htdocs/wp-content/plugins/nginx-helper" ]]; then echo "  $s   (no Nginx Helper plugin: inactive)"
    elif ! cmp -s "$f" "$WOO_SRC"; then echo "  $s   !! differs from this repo's copy (run ./install.sh to update it)"; ok=0
    else echo "  $s"; fi
  done
  [[ $any -eq 1 ]] || echo "  (none)"
  if ! nginx -t >/dev/null 2>&1; then echo "  !! nginx -t FAILS"; ok=0; fi
  [[ $ok -eq 1 ]] && echo "status: OK" || { echo "status: PROBLEMS FOUND"; return 1; }
}

usage() { awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; }

case "${1:-}" in
  --status) status; exit $? ;;
  -h|--help) usage; exit 0 ;;
  --apply-list)
    if out="$(nginx -t 2>&1)"; then
      systemctl reload nginx
      cp -a "$DIR/params.list" "$DIR/params.list.last-good"
      echo "nginx -t OK, list applied ($(grep -cE '^[^#[:space:]].*;' "$DIR/params.list") entries)"
    else
      echo "$out" >&2
      if [[ -e "$DIR/params.list.last-good" ]]; then
        mv "$DIR/params.list" "$DIR/params.list.rejected"
        cp -a "$DIR/params.list.last-good" "$DIR/params.list"
        echo "!! nginx -t failed: restored the last good list; your edit is in $DIR/params.list.rejected" >&2
        nginx -t >/dev/null 2>&1 && echo "nginx -t OK again (nothing was reloaded)" >&2
      else
        echo "!! nginx -t failed and no last good list exists - fix $DIR/params.list" >&2
      fi
      exit 1
    fi
    ;;
  --disable)
    shift; [[ $# -gt 0 ]] || { echo "usage: $0 --disable site.com" >&2; exit 1; }
    trap 'rollback "unexpected error"' ERR
    for s in "$@"; do
      s="${s%/}"; site_name_ok "$s" || rollback "not a site name: $s"
      del "/var/www/$s/nginx/$SWITCH"; del "/var/www/$s/nginx/$FLAG"; del "/var/www/$s/nginx/$FLAG_REDIS"; echo "OFF for $s"
    done
    reload
    echo "No purge needed: what was cached while it was on are ordinary entries of the clean URLs,"
    echo "rendered exactly as for a visitor without parameters."
    ;;
  --woo-purge|--remove-woo-purge)
    a="$1"; shift; [[ $# -gt 0 ]] || { echo "usage: $0 $a site.com [site2...]" >&2; exit 1; }
    trap 'rollback "unexpected error"' ERR
    for s in "$@"; do
      s="${s%/}"; site_name_ok "$s" || rollback "not a site name: $s"
      if [[ "$a" == --woo-purge ]]; then woo_install "$s"; else del "$(woo_dir "$s")/$WOO_NAME"; echo "WOO purge add-on OFF for $s"; fi
    done
    cleanup                        # PHP files only: no nginx reload needed
    ;;
  --uninstall)
    trap 'rollback "unexpected error"' ERR
    for f in /var/www/*/nginx/$SWITCH /var/www/*/nginx/$FLAG /var/www/*/nginx/$FLAG_REDIS /var/www/*/nginx/$OLD_SWITCH \
             "${OLD_FILES[@]}" "$STUB_HTTP" \
             /var/www/*/htdocs/wp-content/mu-plugins/$WOO_NAME; do del "$f"; done
    reload
    if [[ -e "$DIR/params.list" ]]; then
      cp -a "$DIR/params.list" /root/marketing-params.list.bak
      echo "your list was saved to /root/marketing-params.list.bak"
    fi
    rm -rf "$DIR"
    echo "uninstalled"
    ;;
  --full|[^-]*|"")
    woo=ask
    if [[ "${1:-}" == --full ]]; then
      shift; [[ $# -gt 0 ]] || { echo "usage: $0 --full site.com [site2...]" >&2; exit 1; }
      woo=yes
    fi
    BACKUP_TMP="$(mktemp -d)"; trap 'rm -rf "$BACKUP_TMP"' EXIT
    trap 'rollback "unexpected error"' ERR
    install_engine
    refresh_sites
    for s in "$@"; do
      s="${s%/}"; site_name_ok "$s" || rollback "not a site name: $s"
      enable_site "$s"
      if [[ $woo == yes ]] || woo_ask "$s"; then woo_install "$s"; fi
    done
    for f in /var/www/*/htdocs/wp-content/mu-plugins/$WOO_NAME; do   # update copies after git pull
      [[ -e "$f" ]] && ! cmp -s "$f" "$WOO_SRC" || continue
      s="${f#/var/www/}"; woo_install "${s%%/*}"
    done
    reload
    trap - ERR
    status || true
    ;;
  *) echo "unknown option $1" >&2; usage; exit 1 ;;
esac
