#!/usr/bin/env bash
# Marketing params ignored by the GridPane FastCGI page cache.
#
#   ./install.sh                      install / update the server-wide engine (your list is kept)
#   ./install.sh site.com [site2...]  ...and switch it ON for the given site(s)
#   ./install.sh --disable site.com   switch it OFF for a site
#   ./install.sh --apply-list         after editing params.list: nginx -t + reload, or restore
#                                     the last good list if the test fails
#   ./install.sh --uninstall          remove everything (all sites + engine)
#   ./install.sh --status             show what is installed / enabled / suspicious
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
SWITCH=marketing-params-php-context.conf              # php location logic
FLAG=marketing-params-skip-fcgi-cache-context.conf    # "this vhost is FastCGI-cached" flag
# files of the round-1 layout, removed on update
OLD_FILES=(/etc/nginx/extra.d/00-marketing-params-skip-fcgi-cache-context.conf "$DIR/skip-context.conf")
OLD_SWITCH=marketing-params-main-context.conf
STOCK_KEY='fastcgi_cache_key "$scheme$request_method$host$request_uri";'

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

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
    echo "nginx -t OK, nginx reloaded"
    if [[ -e "$DIR/params.list" ]]; then cp -a "$DIR/params.list" "$DIR/params.list.last-good"; fi
    if [[ -e "$DIR/engine.conf" ]]; then write_manifest; fi
    cleanup
  else
    echo "$out" >&2
    rollback "nginx -t failed"
  fi
}

is_fcgi_site() { grep -qs -- "-wpfc.conf" "/etc/nginx/sites-available/$1"; }

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
write_manifest() { (cd "$DIR" && sha256sum engine.conf php-context.conf README.md limits.env > "$MANIFEST" && chmod 640 "$MANIFEST"); }
site_key_ok() {                  # the override assumes GridPane's stock cache key
  local var="/etc/nginx/common/$1-fcgi-cache-var.conf" wpfc="/etc/nginx/common/$1-wpfc.conf"
  [[ -e "/etc/nginx/common/_custom/$1-fcgi-cache-var.conf" ]] && var="/etc/nginx/common/_custom/$1-fcgi-cache-var.conf"
  grep -qsF "$STOCK_KEY" "$var" || return 1
  ! grep -qsE 'cache_uri|lua' "$wpfc"
}

install_engine() {
  local mp=24 ml=2048
  [[ -e "$DIR/limits.env" ]] && { mp="$(sed -n 's/^MAX_PARAMS=//p' "$DIR/limits.env")"; ml="$(sed -n 's/^MAX_LEN=//p' "$DIR/limits.env")"; }
  mp="${MAX_PARAMS:-$mp}"; ml="${MAX_LEN:-$ml}"
  mkdir -p "$DIR"
  python3 "$HERE/build-engine.py" "$mp" "$ml" > "$BACKUP_TMP/engine.conf"
  printf 'MAX_PARAMS=%s\nMAX_LEN=%s\n' "$mp" "$ml" > "$BACKUP_TMP/limits.env"
  baseline_guard "$BACKUP_TMP/engine.conf" "$HERE/server/marketing-params/php-context.conf" "$HERE/README.md" \
    "$HERE/server/marketing-params/params.list" "$HERE/server/stubs/conf.d-marketing-params.conf" \
    "$HERE/site/$SWITCH" "$HERE/site/$FLAG" || rollback "refusing to install"
  put "$BACKUP_TMP/engine.conf" "$DIR/engine.conf" 640
  put "$BACKUP_TMP/limits.env" "$DIR/limits.env" 640
  put "$HERE/server/marketing-params/php-context.conf" "$DIR/php-context.conf" 640
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

enable_site() {
  local s="$1" d="/var/www/$1/nginx"
  [[ -d "$d" ]] || rollback "no such site: $d"
  if ! is_fcgi_site "$s"; then
    echo "!! $s does not use GridPane FastCGI caching (no *-wpfc.conf in its vhost) - skipped" >&2
    return 0
  fi
  if ! site_key_ok "$s"; then
    echo "!! $s has a customised FastCGI cache key or GridPane's Lua query-param cache - skipped" >&2
    echo "   (this tool overrides the key with the stock formula: $STOCK_KEY)" >&2
    return 0
  fi
  put "$HERE/site/$FLAG" "$d/$FLAG" 644
  put "$HERE/site/$SWITCH" "$d/$SWITCH" 644
  echo "ON  for $s"
}

status() {
  local ok=1 f s d r bad
  echo "engine dir:   $DIR"
  for f in "$STUB_HTTP" "$DIR/engine.conf" "$DIR/php-context.conf" "$DIR/params.list"; do
    if [[ -e "$f" ]]; then echo "  ok      $f"; else echo "  MISSING $f"; ok=0; fi
  done
  [[ -e "$DIR/limits.env" ]] && echo "  limits: $(tr '\n' ' ' < "$DIR/limits.env")"
  echo "  list:   $(grep -cE '^[^#[:space:]].*;' "$DIR/params.list" 2>/dev/null || echo 0) entries"
  if [[ -e "$MANIFEST" ]]; then
    bad="$(cd "$DIR" && sha256sum -c --quiet "$MANIFEST" 2>/dev/null | cut -d: -f1 | tr '\n' ' ')" || true
    if [[ -n "$bad" ]]; then echo "  !! changed since install: $bad(re-run ./install.sh)"; ok=0; else echo "  ok      files unchanged since install"; fi
  fi
  if ! baseline_guard "$DIR"/engine.conf "$DIR"/php-context.conf "$DIR"/params.list "$DIR"/README.md 2>/dev/null; then
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
    [[ -e "$d/$SWITCH" || -e "$d/$FLAG" ]] || continue
    s="$(basename "$(dirname "$d")")"
    r="/etc/nginx/common/$s-wpfc.conf"
    if [[ ! -e "$d/$SWITCH" || ! -e "$d/$FLAG" ]]; then echo "  $s   !! only one of $SWITCH / $FLAG present (inactive) - re-run ./install.sh $s or --disable it"; continue; fi
    if [[ -L "$d/$SWITCH" || -L "$d/$FLAG" ]]; then echo "  $s   !! switch files must be regular files (GridPane deletes symlinks there)"; ok=0; continue; fi
    if ! is_fcgi_site "$s"; then echo "  $s   (switch present, but the site has no FastCGI cache: inactive)"; continue; fi
    if ! site_key_ok "$s"; then echo "  $s   !! customised cache key / Lua query-param cache - disable it"; ok=0; continue; fi
    if [[ -e /etc/nginx/common/_custom/wpfc.conf || -e "/etc/nginx/common/_custom/$s-wpfc.conf" ]]; then
      echo "  $s   !! a common/_custom wpfc override exists - check that the hooks below are still there"; fi
    if ! grep -qF 'if ($query_string != "")' "$r" || ! grep -qF -- '-query_string"' "$r" \
       || ! grep -qF "include /var/www/$s/nginx/*-php-context.conf;" "$r" \
       || ! grep -qF "include /var/www/$s/nginx/*skip-fcgi-cache-context.conf;" "$r"; then
      echo "  $s   !! GridPane's template changed (query-string rule or include hooks missing in $r) - run ./test.sh --site $s"; ok=0; continue
    fi
    echo "  $s"
  done
  if ! nginx -t >/dev/null 2>&1; then echo "  !! nginx -t FAILS"; ok=0; fi
  [[ $ok -eq 1 ]] && echo "status: OK" || { echo "status: PROBLEMS FOUND"; return 1; }
}

case "${1:-}" in
  --status) status; exit $? ;;
  -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
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
    for s in "$@"; do del "/var/www/$s/nginx/$SWITCH"; del "/var/www/$s/nginx/$FLAG"; echo "OFF for $s"; done
    reload
    echo "Tip: purge the site's cache afterwards (GridPane UI or Nginx Helper)."
    ;;
  --uninstall)
    trap 'rollback "unexpected error"' ERR
    for f in /var/www/*/nginx/$SWITCH /var/www/*/nginx/$FLAG /var/www/*/nginx/$OLD_SWITCH "${OLD_FILES[@]}" "$STUB_HTTP"; do del "$f"; done
    reload
    if [[ -e "$DIR/params.list" ]]; then
      cp -a "$DIR/params.list" /root/marketing-params.list.bak
      echo "your list was saved to /root/marketing-params.list.bak"
    fi
    rm -rf "$DIR"
    echo "uninstalled"
    ;;
  -*) echo "unknown option $1" >&2; sed -n '2,17p' "$0"; exit 1 ;;
  *)
    BACKUP_TMP="$(mktemp -d)"; trap 'rm -rf "$BACKUP_TMP"' EXIT
    trap 'rollback "unexpected error"' ERR
    install_engine
    for s in "$@"; do enable_site "$s"; done
    reload
    trap - ERR
    status || true
    ;;
esac
