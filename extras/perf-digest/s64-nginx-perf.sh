#!/usr/bin/env bash
# SHIFT64 nginx perf digest — daily per-site analysis of nginx access logs.
# Run from cron at 23:59 (TODAY is fixed at start, so a run past midnight still reports the right day).
#
# GridPane access log format (observed on disk):
#   [12/Jun/2026:17:05:00 +0200] IP UPSTREAM_TIME CACHE HOST "GET /uri HTTP/2.0" STATUS BYTES REQ_TIME "ref" "ua"
#   awk:  $1                 $2  $3 $4            $5    $6    $7   $8   $9       $10    $11   $12
# $4  = $upstream_response_time (pure PHP time; "-" when nginx answers alone:
#       page-cache hit, static file, basic-auth 401). Verified on live logs:
#       POST line had $4=0.117 < $12=0.122.
# $5  = $upstream_cache_status: HIT/MISS/BYPASS/STALE with FastCGI cache,
#       always "-" with Redis page cache (srcache does not set it).
# $12 = $request_time (nginx total, includes client transfer; always numeric)
#
# Config: /etc/s64-nginx-perf.conf -> SITES="example.com other.com"
# Webhook: reuses DISCORD_WEBHOOK_URL from /etc/s64-opcache-alert.conf
#          (override possible in s64-nginx-perf.conf).
#
# One Discord message per site, two embeds: the digest (red = degradation:
# GET/PHP p95 > 1.5x the 7-day median and >100 ms worse, or 5xx >= 10 and >1%;
# green = healthy day; blue = baseline still building) + a "top 5 slowest PHP
# requests" embed (deduped by URI). Sites with a page cache also get a
# HIT/MISS/BYPASS split of customer pages (exact from <site>.cache.log when
# the s64cache log is enabled, else inferred), and MISS p95 joins the
# degradation check. get_p* in the history = customer pages (since 2026-10-26;
# before that all GETs incl. wp-json / bots).
# History: /var/lib/s64-nginx-perf/<site>.csv
# (date,total,get_p50,get_p75,get_p95,get_p99,php_p95,err5xx,hit_pct,miss_p95;
# older 7/8-column files are migrated in place, backups kept as
# <site>.csv.bak-pre-p75 / <site>.csv.bak-pre-cache).
# S64_DRY_RUN=1 prints the embeds instead of posting to Discord; S64_HIST_DIR /
# S64_CATCH override the history dir and the s64-slow-catch.sh path, S64_DATE
# (YYYY-MM-DD) analyses another day still present in the logs (testing).

set -u
CONF=/etc/s64-nginx-perf.conf
OC_CONF=/etc/s64-opcache-alert.conf
HIST_DIR="${S64_HIST_DIR:-/var/lib/s64-nginx-perf}"
CATCH="${S64_CATCH:-/usr/local/bin/s64-slow-catch.sh}"   # optional slow-request companion analysis

[ -f "$OC_CONF" ] && . "$OC_CONF"
[ -f "$CONF" ] && . "$CONF"
SITES="${SITES:-}"
WEBHOOK="${DISCORD_WEBHOOK_URL:-}"
[ "${S64_DRY_RUN:-0}" = "1" ] && WEBHOOK=""   # S64_DRY_RUN=1 -> print instead of posting
HOST=$(hostname -s)
TODAY=${S64_DATE:-$(date +%F)}
TODAY_TAG=$(LC_ALL=C date -d "$TODAY" +%d/%b/%Y)

C_RED=15548997
C_GREEN=5763719
C_BLUE=3447003

FS=$'\x1f'  # field separator
RS=$'\x1e'  # name/value separator

mkdir -p "$HIST_DIR"

send_embeds() { # $1 title, $2 color, $3 description, $4 fields, $5 title2, $6 fields2
    if [ -z "$WEBHOOK" ]; then
        printf '[dry-run] %s\n%s\n' "$1" "$(printf '%s' "$4" | tr "$FS" '\n' | tr "$RS" ': ')"
        [ -n "${5:-}" ] && printf '[dry-run] %s\n%s\n' "$5" "$(printf '%s' "${6:-}" | tr "$FS" '\n' | tr "$RS" ': ')"
        return
    fi
    S64_TITLE="$1" S64_COLOR="$2" S64_DESC="$3" S64_FIELDS="$4" \
    S64_TITLE2="${5:-}" S64_FIELDS2="${6:-}" S64_FOOTER="$HOST · s64-nginx-perf" \
    python3 - <<'PY' | curl -fsS -m 10 -H 'Content-Type: application/json' -d @- "$WEBHOOK" >/dev/null 2>&1 \
        || echo "s64-nginx-perf: discord send failed" >&2
import json, os

def fields_of(env):
    out = []
    for chunk in os.environ.get(env, "").split("\x1f"):
        if not chunk.strip():
            continue
        name, _, value = chunk.partition("\x1e")
        out.append({"name": name[:256], "value": value[:1024], "inline": False})
    return out

color = int(os.environ["S64_COLOR"])
main = {
    "title": os.environ["S64_TITLE"][:256],
    "color": color,
    "fields": fields_of("S64_FIELDS"),
}
if os.environ.get("S64_DESC"):
    main["description"] = os.environ["S64_DESC"][:4096]
embeds = [main]
if os.environ.get("S64_TITLE2"):
    embeds.append({
        "title": os.environ["S64_TITLE2"][:256],
        "color": color,
        "fields": fields_of("S64_FIELDS2"),
        "footer": {"text": os.environ.get("S64_FOOTER", "")},
    })
else:
    main["footer"] = {"text": os.environ.get("S64_FOOTER", "")}
print(json.dumps({"embeds": embeds}))
PY
}

pctl() { # $1 = file with sorted numbers, $2 = percentile -> value or "n/a"
    local n idx
    n=$(wc -l < "$1")
    [ "$n" -eq 0 ] && { echo "n/a"; return; }
    idx=$(( (n * $2 + 99) / 100 )); [ "$idx" -lt 1 ] && idx=1
    sed -n "${idx}p" "$1"
}

ms() { # seconds -> "123 ms" / "1.23 s" (passes "n/a" through)
    [ "$1" = "n/a" ] && { echo "n/a"; return; }
    awk -v s="$1" 'BEGIN{ if (s >= 1) printf "%.2f s", s; else printf "%.0f ms", s*1000 }'
}

[ -z "$SITES" ] && { echo "s64-nginx-perf: no SITES configured in $CONF"; exit 0; }

for site in $SITES; do
    LOG="/var/log/nginx/${site}.access.log"
    if [ ! -f "$LOG" ]; then
        send_embeds "⚠️ $site — no access log" "$C_RED" "Expected $LOG — nothing to analyze." "" "" ""
        continue
    fi

    tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
    # GridPane logrotate runs hourly with maxsize 1M, so today's traffic is
    # spread over the live log AND rotated .gz files — read them all.
    : > "$tmp/lines"
    for f in "$LOG" "$LOG".*; do
        [ -e "$f" ] || continue
        case "$f" in
            *.gz) zgrep -hF "[$TODAY_TAG" "$f" >> "$tmp/lines" 2>/dev/null || true ;;
            *)    grep  -hF "[$TODAY_TAG" "$f" >> "$tmp/lines" 2>/dev/null || true ;;
        esac
    done
    total=$(wc -l < "$tmp/lines")
    if [ "$total" -eq 0 ]; then
        send_embeds "⚠️ $site — empty log" "$C_RED" "0 requests in today's log — site is dead or the log is wrong." "" "" ""
        rm -rf "$tmp"; continue
    fi

    # Every GET is sorted into one kind; only PAGE (what customers see) feeds
    # the "pages" percentiles and the cache split:
    #   ""    = left out: not GET, nginx-answered denials (401 basic-auth /
    #           Prometheus, 403, 412 = GridPane 7G WAF, 444) and static assets
    #           (~0 ms, would drag percentiles to zero). Source maps (.map)
    #           count as static: a missing one renders a full WP 404 (~1 s).
    #   MON   = uptime monitors (healthcheck / cache-bust URL every minute)
    #   API   = /wp-json/ and ?rest_route= (integrations, not people)
    #   ADMIN = wp-admin, wp-login, xmlrpc, wp-cron, ?wc-ajax= fragments
    #   BOT   = crawlers, link previews (facebookexternalhit fetches ~450
    #           pages a day and downloads slowly), scripts, empty UA
    # Page-cache hits stay in PAGE — a 0 ms cached page is genuine user
    # experience. All of them still count under PHP when PHP ran.
    MON='uptime-kuma|uptimerobot|betteruptime|better uptime|pingdom|statuscake|site24x7'
    BOT='bot|crawl|spider|slurp|preview|externalhit|facebookcatalog|meta-external|bytespider|gptbot|claude|perplexity|python|curl|wget|go-http|okhttp|java/|headless|lighthouse|scrapy|ahrefs|semrush'
    KIND='function kind(m, st, u, ua) {
        if (m != "GET" || st ~ /^(401|403|412|444)$/) return ""
        if (u ~ /\.(css|js|mjs|map|png|jpe?g|webp|avif|svg|woff2?|ttf|otf|eot|ico|gif|mp4|webm|pdf|txt|xml|json)(\?|$)/) return ""
        ua = tolower(ua)
        if (ua ~ mon) return "MON"
        if (u ~ /^\/wp-json(\/|$)/ || u ~ /[?&]rest_route=/) return "API"
        if (u ~ /^\/(wp-admin\/|wp-login\.php|xmlrpc\.php|wp-cron\.php)/ || u ~ /[?&]wc-ajax=/) return "ADMIN"
        if (ua == "" || ua == "-" || ua ~ bot) return "BOT"
        return "PAGE"
    }'
    # pages -> raw PAGE lines; kinds -> "KIND status php_time request_time" for
    # every line ("other" = POST & co.); 404/redirect pages become "non200"
    awk -v mon="$MON" -v bot="$BOT" -v pages="$tmp/pages" "$KIND"'
        $12+0 == $12 {
            n = split($0, q, "\""); k = kind(substr($7, 2), $10, $8, q[n-1])
            if (k == "PAGE") print > pages
            if (k == "") k = "other"
            else if (k == "PAGE" && $10 != 200) k = "non200"
            print k, $10, $4, $12
        }' "$tmp/lines" > "$tmp/kinds"
    [ -f "$tmp/pages" ] || : > "$tmp/pages"
    monitors=$(awk '$1=="MON"' "$tmp/kinds" | wc -l)
    for k in BOT API ADMIN; do
        awk -v k="$k" '$1==k {print $4}' "$tmp/kinds" | sort -n > "$tmp/k_$k"
    done
    awk '{print $12}' "$tmp/pages" | sort -n > "$tmp/get"
    awk '$4+0==$4 {print $4}' "$tmp/lines" | sort -n > "$tmp/php"
    # where PHP time went (seconds per kind)
    php_spent=$(awk '$3+0==$3 {s[$1]+=$3}
        END{ split("PAGE non200 API ADMIN BOT MON other", o, " ")
             split("pages 404/redirects API admin bots monitors POST/other", l, " ")
             for (i=1; i<=7; i++) if (s[o[i]] >= 1) out = out sprintf("%s%s %.0f s", (out ? " · " : ""), l[i], s[o[i]])
             print out }' "$tmp/kinds")

    # ── page cache split of PAGE requests ────────────────────────
    # Exact (Redis): /var/log/nginx/<site>.cache.log, written by the s64cache
    # log_format (/etc/nginx/conf.d/s64-cachelog.conf, enabled per site in
    # /var/www/<site>/nginx/s64-cachelog-main-context.conf), carries
    # srcache's own verdict and GridPane's $skip_reason:
    #   HIT    = served from Redis
    #   MISS   = cacheable, but not in Redis yet: PHP generated + stored it
    #   BYPASS = never cached by design, split by reason: cookie (logged-in /
    #            cart session), query (unknown query string), cart (page sent
    #            Do-Not-Cache: cart/checkout/account), uri (GridPane URI rule)
    # Without that log the verdict is inferred from we_log: FastCGI logs it
    # in $5; with Redis $5 stays "-", so no PHP time + 200 = HIT, a visible
    # skip rule (query string, cart / sitemap... URI) = BYPASS, PHP otherwise
    # = MISS (cookie bypasses hide in MISS there).
    # Anything but 200 (404 pages, redirects) is never stored, so it gets its
    # own "non200" row instead of inflating MISS.
    vhost="/etc/nginx/sites-available/$site"
    cache_mode=""
    grep -Eq '^[[:space:]]*include .*-wp-redis\.conf' "$vhost" 2>/dev/null && cache_mode="Redis"
    grep -Eq '^[[:space:]]*include .*-wpfc\.conf' "$vhost" 2>/dev/null && cache_mode="FastCGI"
    : > "$tmp/clines"
    if [ "$cache_mode" = "Redis" ]; then
        for f in "/var/log/nginx/${site}.cache.log" "/var/log/nginx/${site}.cache.log".*; do
            [ -e "$f" ] || continue
            zgrep -h "^${TODAY}T" "$f" >> "$tmp/clines" 2>/dev/null || true
        done
        [ -s "$tmp/clines" ] && cache_mode="Redis exact"
    fi
    hit_pct="n/a"; miss_p95="n/a"; cache_since=""
    if [ "$cache_mode" = "Redis exact" ]; then
        # s64cache line, split on '"':  $1 = time ip method status request_time
        # fetch store skip=.. dnc=.. bytes · $2 = uri · $4 = php time · $6 = ua
        cache_since=$(sort "$tmp/clines" | head -1 | cut -c12-16)
        awk -F'"' -v mon="$MON" -v bot="$BOT" "$KIND"'{
            split($1, a, " ")
            if (kind(a[3], a[4], $2, $6) != "PAGE") next
            sk = substr(a[8], 6); dn = substr(a[9], 5); s = "-"
            if (a[4] != 200) { if ($4 == "-") next; c = "NON200" }
            else if (a[6] == "HIT") c = "HIT"
            else if (sk != "") {
                c = "BYPASS"
                s = (sk ~ /cookie/) ? "cookie" : (sk ~ /request_uri/) ? "uri" : (sk ~ /query_string/) ? "query" : "other"
            }
            else if (dn != "" && dn != "-") { c = "BYPASS"; s = "cart" }
            else if (a[6] == "MISS") c = "MISS"
            else { c = "BYPASS"; s = "other" }
            print c, s, a[5]
        }' "$tmp/clines" > "$tmp/cache3"
        awk '{print $1, $3}' "$tmp/cache3" > "$tmp/cache"
        for s in cookie query cart uri other; do
            awk -v s="$s" '$2==s {print $3}' "$tmp/cache3" | sort -n > "$tmp/s_$s"
        done
    elif [ -n "$cache_mode" ]; then
        awk '{
            c = $5
            if ($10 != 200) c = ($4+0 == $4) ? "NON200" : ""
            else if (c ~ /^(HIT|STALE|UPDATING|REVALIDATED)$/) c = "HIT"
            else if (c ~ /^(MISS|EXPIRED)$/) c = "MISS"
            else if (c != "BYPASS") {
                u = tolower($8)
                if ($4+0 != $4)  c = "HIT"
                else if (u ~ /\?/) c = "BYPASS"
                else if (u ~ /(\/wp-admin\/|\/xmlrpc\.php|wp-.*\.php|index\.php|\/feed\/|sitemap.*\.xml|\/store|\/cart|\/my-account|\/checkout|\/addons|\/wp-json\/)/) c = "BYPASS"
                else c = "MISS"
            }
            if (c != "") print c, $12
        }' "$tmp/pages" > "$tmp/cache"
    fi
    if [ -n "$cache_mode" ]; then
        for c in HIT MISS BYPASS NON200; do
            awk -v c="$c" '$1==c {print $2}' "$tmp/cache" | sort -n > "$tmp/c_$c"
        done
        c_all=$(wc -l < "$tmp/cache")
        c_hit=$(wc -l < "$tmp/c_HIT"); c_miss=$(wc -l < "$tmp/c_MISS"); c_byp=$(wc -l < "$tmp/c_BYPASS")
        c_non=$(wc -l < "$tmp/c_NON200")
        share() { awk -v n="$1" -v t="$c_all" 'BEGIN{ if (t==0) print "n/a"; else printf "%.0f%%", n/t*100 }'; }
        [ "$c_all" -gt 0 ] && hit_pct=$(awk -v n="$c_hit" -v t="$c_all" 'BEGIN{printf "%.1f", n/t*100}')
        miss_p95=$(pctl "$tmp/c_MISS" 95)
    fi

    gets=$(wc -l < "$tmp/get")
    php_n=$(wc -l < "$tmp/php")
    get_p50=$(pctl "$tmp/get" 50); get_p75=$(pctl "$tmp/get" 75)
    get_p95=$(pctl "$tmp/get" 95); get_p99=$(pctl "$tmp/get" 99)
    php_p95=$(pctl "$tmp/php" 95)
    err5=$(awk '$10 ~ /^5[0-9][0-9]$/' "$tmp/lines" | wc -l)
    e499=$(awk '$10 == 499' "$tmp/lines" | wc -l)
    blocked=$(awk '$10 ~ /^(401|403|412|444)$/' "$tmp/lines" | wc -l)
    bots=$(wc -l < "$tmp/k_BOT"); api_n=$(wc -l < "$tmp/k_API")
    err5_pct=$(awk -v e="$err5" -v t="$total" 'BEGIN{printf "%.2f", e/t*100}')
    e499_pct=$(awk -v e="$e499" -v t="$total" 'BEGIN{printf "%.2f", e/t*100}')
    blocked_pct=$(awk -v b="$blocked" -v t="$total" 'BEGIN{printf "%.0f", b/t*100}')

    # top 5 slowest PHP requests, deduped by URI (slowest occurrence wins)
    awk '$4+0==$4 {m=$7; sub(/^"/,"",m); print $4, m, $8}' "$tmp/lines" \
        | sort -rn | awk '!seen[$3]++' | head -5 > "$tmp/top5"

    # archive raw 5xx lines for weekly offline analysis (90-day retention)
    if [ "$err5" -gt 0 ]; then
        mkdir -p "$HIST_DIR/5xx"
        awk '$10 ~ /^5[0-9][0-9]$/' "$tmp/lines" > "$HIST_DIR/5xx/${site}-${TODAY}.log"
    fi
    find "$HIST_DIR/5xx" -name "*.log" -mtime +90 -delete 2>/dev/null

    # ── history: replace today's row, append fresh ───────────────
    hist="$HIST_DIR/$site.csv"
    HDR8="date,total,get_p50,get_p75,get_p95,get_p99,php_p95,err5xx"
    HDR="$HDR8,hit_pct,miss_p95"
    [ -f "$hist" ] || echo "$HDR" > "$hist"
    if [ "$(head -1 "$hist")" = "date,total,get_p50,get_p95,get_p99,php_p95,err5xx" ]; then
        # pre-p75 layout: insert an n/a get_p75 column after get_p50
        cp "$hist" "$hist.bak-pre-p75"
        awk -F, -v OFS=, -v hdr="$HDR8" 'NR==1{print hdr; next} NF==7{$3=$3 OFS "n/a"} {print}' "$hist" > "$hist.new" \
            && mv "$hist.new" "$hist"
    fi
    if [ "$(head -1 "$hist")" = "$HDR8" ]; then
        # pre-cache layout: append n/a hit_pct / miss_p95 columns
        cp "$hist" "$hist.bak-pre-cache"
        awk -F, -v OFS=, -v hdr="$HDR" 'NR==1{print hdr; next} NF==8{$0=$0 OFS "n/a" OFS "n/a"} {print}' "$hist" > "$hist.new" \
            && mv "$hist.new" "$hist"
    fi
    grep -v "^$TODAY," "$hist" > "$hist.new" && mv "$hist.new" "$hist"
    echo "$TODAY,$total,$get_p50,$get_p75,$get_p95,$get_p99,$php_p95,$err5,$hit_pct,$miss_p95" >> "$hist"

    # baseline: median of get_p95 / php_p95 / miss_p95 over the last 7 recorded days (excl. today)
    base_get=$(grep -v "^date\|^$TODAY," "$hist" | tail -7 | cut -d, -f5 | grep -v n/a | sort -n)
    base_php=$(grep -v "^date\|^$TODAY," "$hist" | tail -7 | cut -d, -f7 | grep -v n/a | sort -n)
    base_miss=$(grep -v "^date\|^$TODAY," "$hist" | tail -7 | cut -d, -f10 | grep -v n/a | sort -n)
    med() { local n; n=$(printf '%s\n' "$1" | grep -c . || true); [ "$n" -lt 3 ] && { echo "n/a"; return; }
            printf '%s\n' "$1" | sed -n "$(( (n+1)/2 ))p"; }
    med_get=$(med "$base_get"); med_php=$(med "$base_php"); med_miss=$(med "$base_miss")

    cmp_pct() { # $1 today, $2 median -> "+82%" or "n/a"
        { [ "$1" = "n/a" ] || [ "$2" = "n/a" ]; } && { echo "n/a"; return; }
        awk -v t="$1" -v m="$2" 'BEGIN{ if (m<=0) {print "n/a"; exit}; d=(t-m)/m*100; printf "%+.0f%%", d }'
    }
    worse() { # $1 today, $2 median -> 1 if >1.5x and >0.1s worse
        { [ "$1" = "n/a" ] || [ "$2" = "n/a" ]; } && { echo 0; return; }
        awk -v t="$1" -v m="$2" 'BEGIN{ print (t > m*1.5 && t-m > 0.1) ? 1 : 0 }'
    }

    d_get=$(cmp_pct "$get_p95" "$med_get"); d_php=$(cmp_pct "$php_p95" "$med_php")
    d_miss=$(cmp_pct "$miss_p95" "$med_miss")
    bad=0; desc=""
    [ "$(worse "$get_p95" "$med_get")" = "1" ] \
        && { bad=1; desc+="• Pages p95 **$(ms "$get_p95")** vs median $(ms "$med_get") (**$d_get**)"$'\n'; }
    [ "$(worse "$php_p95" "$med_php")" = "1" ] \
        && { bad=1; desc+="• PHP p95 **$(ms "$php_p95")** vs median $(ms "$med_php") (**$d_php**)"$'\n'; }
    [ "$(worse "$miss_p95" "$med_miss")" = "1" ] \
        && { bad=1; desc+="• Cache MISS p95 **$(ms "$miss_p95")** vs median $(ms "$med_miss") (**$d_miss**)"$'\n'; }
    awk -v e="$err5" -v t="$total" 'BEGIN{ exit !(e >= 10 && e/t > 0.01) }' \
        && { bad=1; desc+="• 5xx errors: **$err5** (${err5_pct}%)"$'\n'; }

    # ── embed 1: digest ──────────────────────────────────────────
    fields="Requests${RS}${total} · pages ${gets} · bots ${bots} · API ${api_n} · monitors ${monitors}${FS}"
    # timings as one monospace table (Discord embeds have no tables; a code
    # block keeps the columns aligned)
    msc() { # compact ms(): "184ms" / "1.66s" / "-"
        [ "$1" = "n/a" ] && { echo "-"; return; }
        awk -v s="$1" 'BEGIN{ if (s >= 1) printf "%.2fs", s; else printf "%.0fms", s*1000 }'
    }
    trow() { # $1 label, $2 count, $3 share, $4 sorted file
        printf '%-7s %6s %4s %6s %6s %6s %6s\n' "$1" "$2" "$3" \
            "$(msc "$(pctl "$4" 50)")" "$(msc "$(pctl "$4" 75)")" "$(msc "$(pctl "$4" 95)")" "$(msc "$(pctl "$4" 99)")"
    }
    tbl=$(printf '%-7s %6s %4s %6s %6s %6s %6s\n' "" "req" "" "p50" "p75" "p95" "p99")
    tbl+=$'\n'$(trow "PAGES" "$gets" "" "$tmp/get")
    tname="Response times"
    tnote="_PAGES = GET pages for people (no static, API, wp-admin/login, bots, monitors); times run until the last byte is sent, so slow clients and big HTML add to them_"
    if [ -n "$cache_mode" ]; then
        tbl+=$'\n'"── page cache ($cache_mode)"
        tbl+=$'\n'$(trow "HIT" "$c_hit" "$(share "$c_hit")" "$tmp/c_HIT")
        tbl+=$'\n'$(trow "MISS" "$c_miss" "$(share "$c_miss")" "$tmp/c_MISS")
        tbl+=$'\n'$(trow "BYPASS" "$c_byp" "$(share "$c_byp")" "$tmp/c_BYPASS")
        if [ "$cache_mode" = "Redis exact" ]; then
            for s in cookie query cart uri other; do
                n=$(wc -l < "$tmp/s_$s")
                [ "$n" -gt 0 ] && tbl+=$'\n'$(trow " $s" "$n" "$(share "$n")" "$tmp/s_$s")
            done
        fi
        tbl+=$'\n'$(trow "non200" "$c_non" "$(share "$c_non")" "$tmp/c_NON200")
        if [ "$cache_mode" = "Redis exact" ]; then
            tnote+=$'\n'"_MISS = cacheable, generated by PHP and stored · BYPASS = never cached: cookie = logged-in / cart session, query = unknown query string, cart = page sent Do-Not-Cache (cart/checkout/account), uri = GridPane URI rule · non200 = 404 / redirects rendered by PHP_"
            [ "$cache_since" \> "00:10" ] && tnote+=$'\n'"_cache split covers ${cache_since}–end of day only (cache log started then)_"
        else
            tnote+=$'\n'"_non200 = 404 / redirects rendered by PHP, never cached_"
            [ "$cache_mode" = "Redis" ] && tnote+=$'\n'"_Redis: cache status inferred from the log; cookie bypass (logged-in, cart) counts as MISS_"
        fi
    fi
    tbl+=$'\n'"── not pages"
    tbl+=$'\n'$(trow "bots" "$bots" "" "$tmp/k_BOT")
    tbl+=$'\n'$(trow "API" "$api_n" "" "$tmp/k_API")
    tbl+=$'\n'$(trow "admin" "$(wc -l < "$tmp/k_ADMIN")" "" "$tmp/k_ADMIN")
    tbl+=$'\n'"── backend (every request that ran PHP)"
    tbl+=$'\n'$(trow "PHP" "$php_n" "" "$tmp/php")
    tnote+=$'\n'"_PHP = pure PHP time (no client download) of everything that ran PHP: pages, 404s, API, admin, bots, POST — overlaps the rows above_"
    # table and legend as separate fields: Discord caps a field value at 1024 chars
    fields+="${tname}${RS}"'```'$'\n'"${tbl}"$'\n''```'"${FS}"
    fields+="How to read it${RS}${tnote}${FS}"
    [ -n "$php_spent" ] && fields+="PHP time spent${RS}${php_spent}${FS}"
    stat=$(printf '%-28s %6s  %s\n' "5xx errors" "$err5" "${err5_pct}%")
    stat+=$'\n'$(printf '%-28s %6s  %s\n' "499 client gave up" "$e499" "${e499_pct}%")
    stat+=$'\n'$(printf '%-28s %6s  %s\n' "blocked 401/403/412/444" "$blocked" "~${blocked_pct}%")
    fields+="Status codes${RS}"'```'$'\n'"${stat}"$'\n''```'"${FS}"

    if [ "$bad" = "1" ]; then
        title="🔴 $site — performance degraded today ($TODAY)"
        color=$C_RED
    elif [ "$med_get" = "n/a" ]; then
        days=$(grep -vc "^date" "$hist")
        title="📊 $site — $TODAY"
        color=$C_BLUE
        fields+="Baseline${RS}building (day $days — threshold alerts start on day 4)${FS}"
    else
        title="📊 $site — $TODAY"
        color=$C_GREEN
        fields+="vs 7-day median${RS}Pages p95 $d_get · PHP p95 $d_php ✅${FS}"
    fi

    # ── embed 2: top 5 slowest PHP requests ──────────────────────
    fields2=""
    rank=0
    while read -r t m uri; do
        rank=$((rank + 1))
        path=${uri%%\?*}; q=""
        [ "$uri" != "$path" ] && q=${uri#*\?}
        [ "${#path}" -gt 80 ] && path="${path:0:77}..."
        [ "${#q}" -gt 150 ] && q="${q:0:147}..."
        val="\`${path}\`"
        [ -n "$q" ] && val+=$'\n'"query: \`${q}\`"
        fields2+="#${rank} · $(ms "$t") · ${m}${RS}${val}${FS}"
    done < "$tmp/top5"
    # why were they slow? s64-slow-catch.sh looks at ±1 s neighbours of every
    # PHP request >= 3 s and at how the times cluster (fixed timeout vs load)
    if [ -x "$CATCH" ]; then
        slow_sum=$("$CATCH" "$site" "$TODAY" 3 --summary --lines "$tmp/lines" 2>/dev/null || true)
        [ -n "$slow_sum" ] && fields2+="⏱ PHP ≥ 3 s — what else was in flight${RS}${slow_sum}${FS}"
    fi
    title2=""
    [ -n "$fields2" ] && title2="🐌 $site — top 5 slowest PHP requests"

    send_embeds "$title" "$color" "$desc" "$fields" "$title2" "$fields2"
    rm -rf "$tmp"
done
