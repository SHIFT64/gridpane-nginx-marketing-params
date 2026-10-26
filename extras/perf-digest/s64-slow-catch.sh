#!/usr/bin/env bash
# SHIFT64 slow-request catcher — forensic companion to s64-nginx-perf.sh.
# Answers "WHY were these requests slow?" by looking at what else hit the site
# in the same second (±1 s) and at how the slow times cluster.
#
#   s64-slow-catch.sh <site> [DATE] [THRESHOLD] [--summary] [--verbose] [--lines FILE]
#
#   DATE       YYYY-MM-DD | today (default) | yesterday
#   THRESHOLD  seconds of $upstream_response_time (pure PHP time), default 3
#   --summary  print a 2–3 line Discord-ready summary (used by s64-nginx-perf.sh)
#   --verbose  also list the ±1 s neighbours of every visitor-facing slow request
#   --lines F  analyze pre-extracted log lines from F instead of /var/log/nginx
#
# Log format = GridPane access log, same field numbers as s64-nginx-perf.sh:
#   $1 [date:time  $3 IP  $4 upstream_time ("-" = nginx alone)  $7 "METHOD
#   $8 uri  $10 status  $12 request_time  last "..." = user-agent
#
# Reading the output — three signatures worth knowing:
#   * tight cluster of times (e.g. all 5.0–6.0 s) + one dominant same-second
#     companion from 127.0.0.1  -> a plugin doing a blocking loopback HTTP call
#     with a fixed timeout (MailPoet cron daemon = 5 s, 'MailPoet Cron' UA)
#   * tight cluster, NO companion -> a fixed timeout on something outside the
#     log: DNS (5 s resolver default), external API, Redis/DB socket
#   * spread-out times, many neighbours per second -> load: php-fpm pool
#     saturation, DB locks, a crawler burst

set -u
SITE="" DAY="today" THR=3 SUMMARY=0 VERBOSE=0 LINES="" pos=0
while [ $# -gt 0 ]; do
    case "$1" in
        --summary) SUMMARY=1 ;;
        --verbose) VERBOSE=1 ;;
        --lines)   LINES="${2:-}"; shift ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) pos=$((pos + 1))
           case $pos in 1) SITE="$1" ;; 2) DAY="$1" ;; 3) THR="$1" ;; esac ;;
    esac
    shift
done
usage() { echo "usage: $0 <site> [YYYY-MM-DD|today|yesterday] [threshold_s] [--summary] [--verbose] [--lines FILE]" >&2; exit 2; }
[ -z "$SITE" ] && usage
case "$THR" in ''|*[!0-9.]*) echo "threshold must be a number, got '$THR'" >&2; usage ;; esac

case "$DAY" in
    today)     TAG=$(date +%d/%b/%Y); ISO=$(date +%F) ;;
    yesterday) TAG=$(date -d yesterday +%d/%b/%Y); ISO=$(date -d yesterday +%F) ;;
    *)         TAG=$(date -d "$DAY" +%d/%b/%Y) || exit 2; ISO=$(date -d "$DAY" +%F) ;;
esac

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
if [ -n "$LINES" ]; then
    cp "$LINES" "$tmp/lines"
else
    LOG="/var/log/nginx/${SITE}.access.log"
    [ -f "$LOG" ] || { echo "no access log: $LOG" >&2; exit 1; }
    : > "$tmp/lines"
    for f in "$LOG" "$LOG".*; do
        [ -e "$f" ] || continue
        case "$f" in
            *.gz) zgrep -hF "[$TAG" "$f" >> "$tmp/lines" 2>/dev/null || true ;;
            *)    grep  -hF "[$TAG" "$f" >> "$tmp/lines" 2>/dev/null || true ;;
        esac
    done
fi
total=$(wc -l < "$tmp/lines")

# One awk pass does all the work; output sections are prefixed so bash can
# split them without re-parsing the log.
awk -v thr="$THR" -v verbose="$VERBOSE" '
function secs(ts,  a) { split(ts, a, ":"); return a[2]*3600 + a[3]*60 + a[4] }
function ua(line,  m) { m = match(line, /"[^"]*"$/); return m ? substr(line, RSTART+1, RLENGTH-2) : "-" }
function internal(ip) { return ip == "127.0.0.1" || ip == "::1" }
{
    n++
    sec[n] = secs($1); ip[n] = $3; up[n] = $4; st[n] = $10
    m = $7; sub(/^"/, "", m); meth[n] = m; uri[n] = $8
    u = ua($0); uas[n] = u
    sig[n] = $3 " · " $10 " · " substr(u, 1, 30)
    bysec[sec[n]] = bysec[sec[n]] " " n
    if ($4 + 0 == $4 && $4 + 0 >= thr) {
        slow[++ns] = n
        if (internal($3)) nself++; else { vis[++nv] = n; b = int($4); band[b]++ }
        h = substr($1, 14, 2); byhour[h]++
        hb = int($4 * 2) / 2; hist[hb]++
    }
}
END {
    printf "HDR\t%d\t%d\t%d\t%d\n", n, ns, nself + 0, nv + 0
    if (ns == 0) exit
    for (k in hist)   printf "HIST\t%.1f\t%d\n", k, hist[k]
    for (k in byhour) printf "HOUR\t%s\t%d\n", k, byhour[k]
    # tight-cluster check on visitor-facing requests: biggest 1-s band share
    bmax = 0; for (k in band) if (band[k] > bmax) { bmax = band[k]; bk = k }
    printf "BAND\t%d\t%d\t%d\n", bk + 0, bmax, nv + 0
    # ±1 s companions of visitor-facing slow requests, aggregated by signature
    for (v = 1; v <= nv; v++) {
        i = vis[v]; delete seen
        for (d = -1; d <= 1; d++) {
            cnt = split(bysec[sec[i] + d], js, " ")
            for (c = 1; c <= cnt; c++) {
                j = js[c]; if (j == i || j == "") continue
                if (!(sig[j] in seen)) { seen[sig[j]] = 1; cov[sig[j]]++ }
                if (verbose) printf "NEIGH\t%d\t%d\t%s\t%s\t%s\t%s %s\t%s\n", i, sec[j] - sec[i], ip[j], up[j], st[j], meth[j], substr(uri[j], 1, 60), substr(uas[j], 1, 40)
            }
        }
    }
    for (s in cov) printf "COMP\t%d\t%s\n", cov[s], s
    for (k = 1; k <= ns; k++) {
        i = slow[k]
        printf "SLOW\t%s\t%s\t%s\t%s\t%s\t%s %s\t%s\t%d\n", sec[i], ip[i], up[i], st[i], (internal(ip[i]) ? "self" : "visitor"), meth[i], substr(uri[i], 1, 70), substr(uas[i], 1, 45), i
    }
}' "$tmp/lines" > "$tmp/out"

read -r _ n ns nself nv < <(grep -m1 '^HDR' "$tmp/out")
hms() { printf '%02d:%02d:%02d' $(( $1 / 3600 )) $(( $1 % 3600 / 60 )) $(( $1 % 60 )); }

# dominant companion + cluster verdict (shared by both output modes)
comp_line=$(grep '^COMP' "$tmp/out" | sort -t$'\t' -k2,2nr | head -1)
comp_n=$(printf '%s' "$comp_line" | cut -f2); comp_sig=$(printf '%s' "$comp_line" | cut -f3)
read -r _ bk bmax bn < <(grep -m1 '^BAND' "$tmp/out" || echo "BAND 0 0 0")
verdict=""
if [ "${nv:-0}" -ge 5 ] && [ "$bmax" -gt 0 ] && [ $(( bmax * 100 / nv )) -ge 60 ]; then
    verdict="times cluster in ${bk}–$((bk + 1)) s (${bmax}/${nv}) → fixed-timeout signature, not slow code"
elif [ "${nv:-0}" -ge 3 ]; then
    verdict="times spread out (largest 1-s band ${bmax}/${nv}) → load / genuinely slow pages"
fi
comp_txt="no dominant same-second companion"
if [ -n "$comp_n" ] && [ "$nv" -ge 3 ] && [ "$comp_n" -ge 3 ] && [ $(( comp_n * 100 / nv )) -ge 30 ]; then
    comp_txt="companion (±1 s) \`${comp_sig}\` in ${comp_n}/${nv} ($(( comp_n * 100 / nv ))%)"
fi

if [ "$SUMMARY" = "1" ]; then
    [ "${ns:-0}" -eq 0 ] && exit 0
    printf '%s req · %s self-calls (127.0.0.1) · %s visitor-facing\n' "$ns" "$nself" "$nv"
    [ "$nv" -ge 3 ] && printf '%s\n%s\n' "$comp_txt" "$verdict"
    exit 0
fi

echo "== $SITE · $ISO · PHP upstream time >= ${THR}s · $n log lines"
echo "   slow: $ns (self-calls from 127.0.0.1: $nself · visitor-facing: $nv)"
[ "${ns:-0}" -eq 0 ] && exit 0
echo
echo "-- time histogram (0.5 s buckets, all slow):"
grep '^HIST' "$tmp/out" | sort -t$'\t' -k2,2n | awk -F'\t' '{ b=""; for(i=0;i<$3 && i<40;i++) b=b"#"; printf "   %4.1f–%.1f s  %4d  %s\n", $2, $2+0.5, $3, b }'
[ -n "$verdict" ] && echo "   → $verdict"
echo
echo "-- by hour:"
grep '^HOUR' "$tmp/out" | sort -t$'\t' -k2,2 | awk -F'\t' '{ printf "   %sh %3d  ", $2, $3; for(i=0;i<$3 && i<40;i++) printf "#"; print "" }'
echo
echo "-- same-second (±1 s) companions of the $nv visitor-facing slow requests, by IP · status · UA:"
if grep -q '^COMP' "$tmp/out"; then
    grep '^COMP' "$tmp/out" | sort -t$'\t' -k2,2nr | head -6 | awk -F'\t' -v nv="$nv" '{ printf "   %3d/%d (%3d%%)  %s\n", $2, nv, $2*100/nv, $3 }'
else
    echo "   (none)"
fi
echo "   → $comp_txt"
echo
echo "-- all slow requests (time · ip · upstream · status · kind · request · ua):"
grep '^SLOW' "$tmp/out" | sort -t$'\t' -k2,2n | while IFS=$'\t' read -r _ s ip up st kind req ua idx; do
    printf '   %s  %-16s %6ss  %s  %-7s %s  | %s\n' "$(hms "$s")" "$ip" "$up" "$st" "$kind" "$req" "$ua"
    if [ "$VERBOSE" = "1" ] && [ "$kind" = "visitor" ]; then
        grep -P "^NEIGH\t${idx}\t" "$tmp/out" | awk -F'\t' '{ printf "        %+ds  %-16s %6ss  %s  %s  | %s\n", $3, $4, $5, $6, $7, $8 }'
    fi
done
