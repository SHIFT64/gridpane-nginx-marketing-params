#!/usr/bin/env bash
# Exact Redis page-cache log for GridPane sites (optional add-on of gridpane-nginx-marketing-params).
#
#   ./extras/cache-log/install.sh site1.com site2.com   enable for these sites (Redis page cache only)
#   ./extras/cache-log/install.sh --status              what is installed, file checksums, nginx -t
#   ./extras/cache-log/install.sh --disable site.com    stop writing the site's cache log
#   ./extras/cache-log/install.sh --uninstall           disable every site + remove the log_format and the rotation
#
# Writes a second access log per site, /var/log/nginx/<site>.cache.log, with srcache's own verdict
# (HIT / MISS / BYPASS, store STORE / BYPASS), GridPane's $skip_reason and the Do-Not-Cache header.
# The logs are rotated by /etc/logrotate.d/s64-cachelog: GridPane's own logrotate file (v1) only
# matches *.access.log and *.error.log. Where another logrotate file still matches the cache logs
# (GridPane's older /var/log/nginx/*log), rotation is left to that file, because logrotate errors
# on a file matched twice. Old log files are kept on --disable / --uninstall.
#
# Never touches GridPane-managed files: one file in /etc/nginx/conf.d/, one in /etc/logrotate.d/,
# one per site in /var/www/<site>/nginx/. Every change runs `nginx -t`; if it fails, the previous
# files are put back and nginx is not reloaded.
set -Eeuo pipefail
cd "$(dirname "$0")"

CONF_SRC="s64-cachelog.conf"
CONF_DST="${GP_CACHELOG_ROOT:-}/etc/nginx/conf.d/s64-cachelog.conf"
ROTATE_SRC="s64-cachelog.logrotate"
ROTATE_DIR="${GP_CACHELOG_ROOT:-}/etc/logrotate.d"
ROTATE_DST="$ROTATE_DIR/s64-cachelog"
SITES_DIR="${GP_CACHELOG_ROOT:-}/etc/nginx/sites-available"
WWW_DIR="${GP_CACHELOG_ROOT:-}/var/www"
LOG_DIR="/var/log/nginx"
SITE_FILE="s64-cachelog-main-context.conf"

die() { echo "!! $*" >&2; exit 1; }
[[ $EUID -eq 0 || -n "${GP_CACHELOG_ROOT:-}" ]] || die "run as root"

site_conf() { echo "$WWW_DIR/$1/nginx/$SITE_FILE"; }
site_body() { # $1 site -> content of the per-site file
  printf '%s\n' \
    "# Exact Redis page-cache log (gridpane-nginx-marketing-params, extras/cache-log)." \
    "# Format: /etc/nginx/conf.d/s64-cachelog.conf. Remove with: extras/cache-log/install.sh --disable $1" \
    "access_log $LOG_DIR/$1.cache.log s64cache;"
}
# why a site cannot get the log (nothing = it can)
site_problem() {
  local s="$1" vhost="$SITES_DIR/$1"
  [[ -f "$vhost" ]] || { echo "no vhost $vhost"; return; }
  grep -Eq '^[[:space:]]*include .*-wp-redis\.conf' "$vhost" || { echo "not on GridPane's Redis page cache"; return; }
  grep -Eq "^[[:space:]]*include $WWW_DIR/$s/nginx/\*-main-context\.conf;" "$vhost" \
    || grep -Eq "^[[:space:]]*include /var/www/$s/nginx/\*-main-context\.conf;" "$vhost" \
    || { echo "vhost has no include of /var/www/$s/nginx/*-main-context.conf"; return; }
  [[ -d "$WWW_DIR/$s/nginx" ]] || echo "no $WWW_DIR/$s/nginx/"
}
# another logrotate file whose pattern already matches the cache logs (nothing = none)
rotated_by() {
  local f pat
  for f in "$ROTATE_DIR"/*; do
    [[ -f "$f" && "$f" != "$ROTATE_DST" ]] || continue
    while read -r pat; do
      if [[ "$LOG_DIR/site.cache.log" == $pat ]]; then echo "$f"; return 0; fi   # unquoted: a glob match
    done < <(sed 's/#.*//' "$f" | grep -oE "$LOG_DIR/[^[:space:]{\"']+" || true)
  done
}

# ── backup / apply / rollback ─────────────────────────────────────
BAK="$(mktemp -d)"; TOUCHED=()
trap 'rm -rf "$BAK"' EXIT
remember() { # keep the current state of $1 (file or "absent") before changing it
  local f="$1" k; k="$BAK/$(printf '%s' "$f" | tr / _)"
  [[ -e "$k" || -e "$k.absent" ]] && return 0
  if [[ -e "$f" ]]; then cp -p "$f" "$k"; else : > "$k.absent"; fi
  TOUCHED+=("$f")
}
rollback() {
  local f k
  for f in "${TOUCHED[@]}"; do
    k="$BAK/$(printf '%s' "$f" | tr / _)"
    if [[ -e "$k.absent" ]]; then rm -f "$f"; else cp -p "$k" "$f"; fi
  done
  echo "!! $1: previous files restored, nginx NOT reloaded" >&2
  exit 1
}
write_file() { # $1 path, stdin = content; no-op when identical
  local tmp; tmp="$(mktemp)"; cat > "$tmp"
  if [[ -f "$1" ]] && cmp -s "$tmp" "$1"; then rm -f "$tmp"; return 0; fi
  remember "$1"; install -m 644 "$tmp" "$1"; rm -f "$tmp"; mark_changed "$1"
}
changing() { trap 'rollback "unexpected error"' ERR; }   # mutating commands only
remove_file() { [[ -e "$1" ]] || return 0; remember "$1"; rm -f "$1"; mark_changed "$1"; }
mark_changed() { if [[ "$1" = "$ROTATE_DST" ]]; then ROTATION_CHANGED=1; else CHANGED=1; fi; }   # nginx never reads the logrotate file
apply() {
  if [[ "${CHANGED:-0}" != 1 ]]; then
    [[ "${ROTATION_CHANGED:-0}" = 1 ]] && echo "nginx files unchanged, nginx not reloaded" || echo "nothing changed"
    return 0
  fi
  local out
  if [[ -n "${GP_CACHELOG_ROOT:-}" ]]; then echo "(test root: nginx -t / reload skipped)"; return 0; fi
  if out="$(nginx -t 2>&1)"; then
    systemctl reload nginx
    echo "nginx -t OK, nginx reloaded"
  else
    echo "$out" >&2
    rollback "nginx -t failed"
  fi
}

# our logrotate file, unless another one already rotates the cache logs
rotation() {
  local by
  [[ -d "$ROTATE_DIR" ]] || { echo "!! no $ROTATE_DIR: $LOG_DIR/*.cache.log will not be rotated" >&2; return 0; }
  by="$(rotated_by)"
  if [[ -n "$by" ]]; then
    remove_file "$ROTATE_DST"; echo "rotation: left to $by (it matches $LOG_DIR/*.cache.log)"
  else
    write_file "$ROTATE_DST" < "$ROTATE_SRC"; echo "rotation: $ROTATE_DST (used from logrotate's next run)"
  fi
}

enabled_sites() {
  local f
  for f in "$WWW_DIR"/*/nginx/"$SITE_FILE"; do
    [[ -e "$f" ]] || continue
    f="${f%/nginx/$SITE_FILE}"; echo "${f##*/}"
  done
}

cmd_status() {
  local s by ok=1
  if [[ -f "$CONF_DST" ]]; then
    if cmp -s "$CONF_SRC" "$CONF_DST"; then echo "log_format: $CONF_DST (current)"
    else echo "log_format: $CONF_DST (differs from this checkout - run install)"; ok=0; fi
  else
    echo "log_format: not installed"
  fi
  by="$(rotated_by)"
  if [[ -f "$ROTATE_DST" && -n "$by" ]]; then
    echo "  !! rotation: $ROTATE_DST and $by both match $LOG_DIR/*.cache.log (logrotate errors on a file matched twice) - run install"; ok=0
  elif [[ -f "$ROTATE_DST" ]]; then
    if cmp -s "$ROTATE_SRC" "$ROTATE_DST"; then echo "rotation: $ROTATE_DST (current)"
    else echo "rotation: $ROTATE_DST (differs from this checkout - run install)"; ok=0; fi
  elif [[ -n "$by" ]]; then
    echo "rotation: by $by"
  elif [[ -n "$(enabled_sites)" ]]; then
    echo "  !! rotation: nothing rotates $LOG_DIR/*.cache.log - run install"; ok=0
  else
    echo "rotation: not installed"
  fi
  for s in $(enabled_sites); do
    local p; p="$(site_problem "$s")"
    if [[ -n "$p" ]]; then echo "  !! $s: $p"; ok=0
    elif ! cmp -s <(site_body "$s") "$(site_conf "$s")"; then echo "  $s: on (file differs - run install)"; ok=0
    else echo "  $s: on → $LOG_DIR/$s.cache.log ($( [[ -f "$LOG_DIR/$s.cache.log" ]] && du -h "$LOG_DIR/$s.cache.log" | cut -f1 || echo "no file yet"))"
    fi
  done
  [[ -f "$CONF_DST" || -z "$(enabled_sites)" ]] || { echo "  !! sites enabled but no log_format"; ok=0; }
  if [[ -z "${GP_CACHELOG_ROOT:-}" ]] && ! nginx -t >/dev/null 2>&1; then echo "  !! nginx -t FAILS"; ok=0; fi
  [[ $ok = 1 ]] && echo "OK" || { echo "PROBLEMS found"; return 1; }
}

cmd_enable() {
  local s p n=0
  changing
  write_file "$CONF_DST" < "$CONF_SRC"
  for s in "$@"; do
    p="$(site_problem "$s")"
    if [[ -n "$p" ]]; then echo "skipped $s: $p"; continue; fi
    write_file "$(site_conf "$s")" < <(site_body "$s")
    echo "$s: on → $LOG_DIR/$s.cache.log"; n=$((n + 1))
  done
  # sites that already have the log get the current file too
  for s in $(enabled_sites); do
    if [[ -z "$(site_problem "$s")" ]]; then write_file "$(site_conf "$s")" < <(site_body "$s"); fi
  done
  [[ $n -gt 0 || -n "$(enabled_sites)" ]] || echo "no site enabled"
  rotation
  apply
}

cmd_disable() {
  local s
  changing
  [[ $# -gt 0 ]] || die "usage: $0 --disable site.com [...]"
  for s in "$@"; do remove_file "$(site_conf "$s")"; echo "$s: off (old $LOG_DIR/$s.cache.log* kept)"; done
  apply
}

cmd_uninstall() {
  local s
  changing
  for s in $(enabled_sites); do remove_file "$(site_conf "$s")"; echo "$s: off"; done
  remove_file "$CONF_DST"
  remove_file "$ROTATE_DST"
  apply
  echo "old $LOG_DIR/*.cache.log* files kept (and no longer rotated)"
}

case "${1:-}" in
  --status)    cmd_status ;;
  --disable)   shift; cmd_disable "$@" ;;
  --uninstall) cmd_uninstall ;;
  ""|-h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//' ;;
  -*)          die "unknown option $1" ;;
  *)           cmd_enable "$@" ;;
esac
