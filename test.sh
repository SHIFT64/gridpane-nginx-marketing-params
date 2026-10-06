#!/usr/bin/env bash
# Tests for the GridPane marketing-params cache config.
#
#   ./test.sh                         sandbox test: throw-away nginx on a random port with
#                                     GridPane's real template + this repo + a fake PHP.
#                                     Touches nothing live. Safe on production servers.
#   ./test.sh --site example.com      sandbox test using that site's rendered GridPane template
#   ./test.sh live example.com [/path/]   smoke test of an installed site (read-only requests
#                                         to the local nginx; creates at most one cache entry)
set -euo pipefail
cd "$(dirname "$0")"
if [[ "${1:-}" == "live" ]]; then
  shift
  exec python3 tests/live_test.py "$@"
fi
exec python3 tests/sandbox_test.py "$@"
