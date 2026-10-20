#!/usr/bin/env bash
# Tests for the GridPane marketing-params cache config.
#
#   ./test.sh                         sandbox test + installer test (below). Touches nothing live.
#                                     Safe on production servers.
#   ./test.sh --cache fcgi|redis|all  sandbox test only: throw-away nginx on a random port with
#                                     GridPane's real templates + this repo + a fake PHP
#                                     (Redis: + a throw-away redis-server)
#   ./test.sh --site example.com      sandbox test using that site's rendered GridPane template
#   ./test.sh live example.com [/path/]   smoke test of an installed site (read-only requests
#                                         to the local nginx; creates at most one cache entry)
#   ./test.sh woo example.com         test of the installed WooCommerce purge add-on, inside the
#                                     site's WordPress (gp wp); purges nothing, saves nothing
#   ./test.sh install                 test of install.sh in every mode, in a throw-away root under
#                                     /tmp with the real nginx -t (touches nothing live)
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
if [[ "${1:-}" == "install" ]]; then
  exec python3 tests/install_test.py
fi
if [[ $# -gt 0 ]]; then
  exec python3 tests/sandbox_test.py "$@"
fi
rc=0
python3 tests/sandbox_test.py || rc=$?
echo
python3 tests/install_test.py || { r=$?; [[ $rc -ne 0 ]] || rc=$r; }
echo
if [[ $rc -eq 0 ]]; then echo "ALL TESTS PASSED (sandbox + installer)"; else echo "TESTS FAILED (exit $rc)"; fi
exit $rc
