#!/usr/bin/env bash
# Tests for the GridPane marketing-params cache config.
#
#   ./test.sh                         sandbox test: throw-away nginx on a random port with
#                                     GridPane's real template + this repo + a fake PHP.
#                                     Touches nothing live. Safe on production servers.
#   ./test.sh --site example.com      sandbox test using that site's rendered GridPane template
#   ./test.sh live example.com [/path/]   smoke test of an installed site (read-only requests
#                                         to the local nginx; creates at most one cache entry)
#   ./test.sh woo example.com         test of the installed WooCommerce purge add-on, inside the
#                                     site's WordPress (gp wp); purges nothing, saves nothing
set -euo pipefail
cd "$(dirname "$0")"
if [[ "${1:-}" == "live" ]]; then
  shift
  exec python3 tests/live_test.py "$@"
fi
if [[ "${1:-}" == "woo" ]]; then
  site="${2:?usage: ./test.sh woo example.com}"
  tmp="$(mktemp --suffix=.php /tmp/gp-woo-purge-test.XXXXXX)"
  trap 'rm -f "$tmp"' EXIT
  cp tests/woo_purge_test.php "$tmp"; chmod 644 "$tmp"     # gp wp runs as the site user
  out="$(gp wp "$site" eval-file "$tmp" 2>&1 || true)"
  sed -n 's/^@@//p' <<<"$out"
  if grep -qE '^@@RESULT pass=[0-9]+ fail=0 ' <<<"$out"; then echo "ALL CHECKS PASSED"; exit 0; fi
  if grep -q '^@@RESULT' <<<"$out"; then exit 1; fi
  echo "could not run the test inside WordPress (gp wp $site eval-file); its output:" >&2
  tail -20 <<<"$out" >&2
  exit 2
fi
exec python3 tests/sandbox_test.py "$@"
