#!/usr/bin/env bash
# Nightly nginx perf digest to Discord (optional add-on of gridpane-nginx-marketing-params).
#
#   ./extras/perf-digest/install.sh site1.com site2.com   install / update, report on these sites
#   ./extras/perf-digest/install.sh                       update the scripts, keep the site list
#   ./extras/perf-digest/install.sh --test [YYYY-MM-DD]   dry run: print today's (or that day's) report, post nothing
#   ./extras/perf-digest/install.sh --status              scripts, config, webhook, cron, history
#   ./extras/perf-digest/install.sh --uninstall           remove scripts + cron (config and history kept)
#
# Webhook: taken from $DISCORD_WEBHOOK_URL when set, else asked for (hidden input) when none is
# configured yet. Stored in /etc/s64-nginx-perf.conf (mode 600). A DISCORD_WEBHOOK_URL in
# /etc/s64-opcache-alert.conf is used as well when this config has none.
#
# Installs /usr/local/bin/s64-nginx-perf.sh + s64-slow-catch.sh (a changed old copy is kept as
# <name>.bak-<date>) and /etc/cron.d/s64-nginx-perf (23:59 daily). History:
# /var/lib/s64-nginx-perf/<site>.csv. For the exact HIT / MISS / BYPASS split on Redis sites,
# also enable ../cache-log/install.sh for the same sites.
set -Eeuo pipefail
cd "$(dirname "$0")"

ROOT="${GP_DIGEST_ROOT:-}"          # test root; empty = the real server
BIN="$ROOT/usr/local/bin"
CONF="$ROOT/etc/s64-nginx-perf.conf"
OC_CONF="$ROOT/etc/s64-opcache-alert.conf"
CRON="$ROOT/etc/cron.d/s64-nginx-perf"
HIST="$ROOT/var/lib/s64-nginx-perf"
SCRIPTS=(s64-nginx-perf.sh s64-slow-catch.sh)

die() { echo "!! $*" >&2; exit 1; }
[[ $EUID -eq 0 || -n "$ROOT" ]] || die "run as root"

conf_get() { # $1 key, $2 file -> value (quotes stripped) or nothing
  [[ -f "$2" ]] || return 0
  sed -n "s/^[[:space:]]*$1=//p" "$2" | tail -1 | sed 's/^"\(.*\)"$/\1/'
}
conf_set() { # $1 key, $2 value: replace or append KEY="value" in $CONF
  local tmp; tmp="$(mktemp)"
  { grep -v "^[[:space:]]*$1=" "$CONF" 2>/dev/null || true; printf '%s="%s"\n' "$1" "$2"; } > "$tmp"
  install -m 600 "$tmp" "$CONF"; rm -f "$tmp"
}
webhook_ok() { [[ "$1" =~ ^https://(discord\.com|discordapp\.com)/api/webhooks/[0-9]+/[A-Za-z0-9_-]+$ ]]; }

cmd_install() {
  local s f bak today; today="$(date +%F)"
  [[ $# -gt 0 || -n "$(conf_get SITES "$CONF")" ]] || die "no sites configured: $0 site1.com [site2.com ...]"
  for s in "$@"; do
    [[ -f "$ROOT/var/log/nginx/$s.access.log" ]] || echo "warning: no $ROOT/var/log/nginx/$s.access.log yet ($s)"
  done
  mkdir -p "$BIN" "$(dirname "$CRON")" "$HIST"
  for f in "${SCRIPTS[@]}"; do
    if [[ -f "$BIN/$f" ]] && cmp -s "$f" "$BIN/$f"; then echo "$f: current"; continue; fi
    if [[ -f "$BIN/$f" ]]; then
      bak="$BIN/$f.bak-$today"; [[ -e "$bak" ]] && bak="$bak-$(date +%H%M%S)"   # never overwrite a backup
      cp -p "$BIN/$f" "$bak"; echo "$f: old copy kept as ${bak##*/}"
    fi
    install -m 755 "$f" "$BIN/$f"; echo "$f: installed"
  done

  [[ -f "$CONF" ]] || { printf '# SHIFT64 nginx perf digest config\n' > "$CONF"; chmod 600 "$CONF"; }
  chmod 600 "$CONF"
  if [[ $# -gt 0 ]]; then conf_set SITES "$*"; fi
  echo "sites: $(conf_get SITES "$CONF")"

  local hook="${DISCORD_WEBHOOK_URL:-}"
  if [[ -n "$hook" ]]; then
    webhook_ok "$hook" || die "DISCORD_WEBHOOK_URL does not look like a Discord webhook URL"
    conf_set DISCORD_WEBHOOK_URL "$hook"; echo "webhook: saved in $CONF"
  elif [[ -n "$(conf_get DISCORD_WEBHOOK_URL "$CONF")$(conf_get DISCORD_WEBHOOK_URL "$OC_CONF")" ]]; then
    echo "webhook: already configured"
  elif [[ -t 0 ]]; then
    read -rsp "Discord webhook URL (input hidden): " hook; echo
    webhook_ok "$hook" || die "that does not look like a Discord webhook URL"
    conf_set DISCORD_WEBHOOK_URL "$hook"; echo "webhook: saved in $CONF"
  else
    echo "warning: no webhook configured - reports are only printed (set DISCORD_WEBHOOK_URL and re-run)"
  fi

  local line="59 23 * * * root /usr/local/bin/s64-nginx-perf.sh >/dev/null 2>&1"
  if [[ "$(cat "$CRON" 2>/dev/null)" != "$line" ]]; then
    printf '%s\n' "$line" > "$CRON"; chmod 644 "$CRON"; echo "cron: $CRON (23:59 daily)"
  else
    echo "cron: current"
  fi
  echo "done - check with: $0 --test"
}

cmd_test() { # dry run on a copy of the history, nothing posted, history untouched
  local day="${1:-}" tmp; tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  [[ -d "$HIST" ]] && cp -a "$HIST/." "$tmp/"
  [[ -x "$BIN/s64-nginx-perf.sh" ]] || die "not installed: run $0 site.com first"
  S64_DRY_RUN=1 S64_HIST_DIR="$tmp" S64_CATCH="$BIN/s64-slow-catch.sh" ${day:+S64_DATE=$day} "$BIN/s64-nginx-perf.sh"
}

cmd_status() {
  local f s ok=1
  for f in "${SCRIPTS[@]}"; do
    if [[ ! -f "$BIN/$f" ]]; then echo "$f: not installed"; ok=0
    elif cmp -s "$f" "$BIN/$f"; then echo "$f: current"
    else echo "$f: differs from this checkout (run install)"; ok=0; fi
  done
  echo "sites: $(conf_get SITES "$CONF")"
  if [[ -n "$(conf_get DISCORD_WEBHOOK_URL "$CONF")" ]]; then echo "webhook: in $CONF"
  elif [[ -n "$(conf_get DISCORD_WEBHOOK_URL "$OC_CONF")" ]]; then echo "webhook: from $OC_CONF"
  else echo "webhook: none (reports only printed)"; ok=0; fi
  [[ -f "$CRON" ]] && echo "cron: $(cat "$CRON")" || { echo "cron: missing"; ok=0; }
  for s in $(conf_get SITES "$CONF"); do
    if [[ -f "$HIST/$s.csv" ]]; then echo "  $s: last report $(tail -1 "$HIST/$s.csv" | cut -d, -f1), $(grep -vc '^date' "$HIST/$s.csv") days of history"
    else echo "  $s: no history yet"; fi
  done
  [[ $ok = 1 ]] && echo "OK" || { echo "PROBLEMS found"; return 1; }
}

cmd_uninstall() {
  local f
  for f in "${SCRIPTS[@]}"; do rm -f "$BIN/$f"; done
  rm -f "$CRON"
  echo "scripts and cron removed; kept $CONF and $HIST"
}

case "${1:-}" in
  --test)      shift; cmd_test "$@" ;;
  --status)    cmd_status ;;
  --uninstall) cmd_uninstall ;;
  -h|--help)   sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//' ;;
  -*)          die "unknown option $1" ;;
  *)           cmd_install "$@" ;;
esac
