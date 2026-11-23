#!/usr/bin/env python3
"""
Extras test (extras/cache-log + extras/perf-digest) - safe to run on any server, even production:

  * copies extras/ into a throw-away root under /tmp, with the scripts' paths (/etc/nginx,
    /etc/logrotate.d, /etc/s64-*, /var/www, /var/log/nginx, /var/run/nginx.pid) moved into it,
  * runs the cache-log installer there (GP_CACHELOG_ROOT): its own logrotate file next to GridPane's
    logrotate v1 file and next to an older catch-all pattern, re-install, --status, --disable,
    --uninstall,
  * runs the perf digest (S64_DRY_RUN=1) and s64-slow-catch.sh on log lines written by this test,
    spread over the live log and rotated files under both of GridPane's naming schemes
    (<log>.N.gz and <log>-YYYYMMDD-<epoch>.gz),
  * with the `logrotate` binary and root: lets the real logrotate rotate those logs with the
    installed file next to GridPane's v1 stanzas, then runs the digest on the result,
  * checks that the digest counts a GET on an ?add-to-cart= link as a bot, not as a page.

The log lines are synthetic, not real traffic. No nginx is started, reloaded or signalled, nothing
is posted to Discord, and the server's own logs, logrotate config and state are never touched.
Parts that need GNU date or logrotate are skipped with a notice where those are missing.

  ./test.sh extras
"""
import datetime
import fnmatch
import gzip
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from sandbox_test import SetupError, c, read, write  # noqa: E402

SITE = "shop.test"
DAY = datetime.date(2026, 11, 19)                 # the day the digest reports on (S64_DATE)
OLD = DAY - datetime.timedelta(days=3)            # an earlier day still in the logs
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
REAL_PATHS = ("/var/log/nginx", "/var/run/nginx.pid", "/etc/nginx/", "/etc/s64-")
DATED = r"-\d{8}-\d+"                             # GridPane's dateformat -%Y%m%d-%s

# /etc/logrotate.d/nginx as GridPane writes it since 2026-11 (copied from a live server)
GP_V1 = """\
# GridPane nginx log rotation (#2258) v1 - managed by workers::nginx::sync_logrotate.
# Two stanzas with NON-overlapping patterns: logrotate errors on a file matched twice.
# Any new log file written to /var/log/nginx must be added to one of them.

# Access logs: kept 30 days by age; no uncompressed file beyond ~10M plus one hour of writes.
/var/log/nginx/access.log /var/log/nginx/*.access.log {
    create 0644 root root
    daily
    maxsize 10M
    maxage 30
    rotate 800
    dateext
    dateformat -%Y%m%d-%s
    missingok
    notifempty
    compress
    sharedscripts
    postrotate
        /bin/kill -USR1 $(cat /var/run/nginx.pid 2>/dev/null) 2>/dev/null || true
    endscript
}

# Error and WAF logs: unchanged from the previous build default (1M / 28 rotations, numbered names).
/var/log/nginx/error.log /var/log/nginx/*.error.log /var/log/nginx/6g.log /var/log/nginx/7g.log {
    create 0644 root root
    daily
    maxsize 1M
    rotate 28
    missingok
    notifempty
    compress
    sharedscripts
    postrotate
        /bin/kill -USR1 $(cat /var/run/nginx.pid 2>/dev/null) 2>/dev/null || true
    endscript
}
"""
# Stand-in for GridPane's file before v1. Only its pattern is known for certain (one catch-all
# for every nginx log, numbered names); the settings are v1's "previous build default".
GP_OLD = """\
/var/log/nginx/*log {
    create 0644 root root
    daily
    maxsize 1M
    rotate 28
    missingok
    notifempty
    compress
    sharedscripts
    postrotate
        /bin/kill -USR1 $(cat /var/run/nginx.pid 2>/dev/null) 2>/dev/null || true
    endscript
}
"""


def stanzas(text):
    """logrotate config -> [(patterns, settings)]"""
    text = re.sub(r"(?m)^\s*#.*\n", "", text)
    return [(m.group(1).split(), [l.strip() for l in m.group(2).strip().splitlines()])
            for m in re.finditer(r"([^{}]+)\{(.*?)\n\}", text, re.S)]


def access(day, hms, uri):
    """One line of GridPane's access log: a page for a person, rendered by PHP."""
    return '[%s:%s +0100] 203.0.113.7 0.120 - %s "GET %s HTTP/2.0" 200 5120 0.125 "-" "%s"\n' % (
        day.strftime("%d/%b/%Y"), hms, SITE, uri, UA)


def cache(day, hms, uri, fetch="HIT"):
    """One line of the s64cache log (extras/cache-log/s64-cachelog.conf)."""
    rt, store, php = ("0.002", "BYPASS", "-") if fetch == "HIT" else ("0.391", "STORE", "0.390")
    return '%sT%s+01:00 203.0.113.7 GET 200 %s %s %s skip= dnc=- 5120 "%s" "%s" "%s"\n' % (
        day.isoformat(), hms, rt, fetch, store, uri, php, UA)


class Root:
    def __init__(self):
        self.d = tempfile.mkdtemp(prefix="gp-mkt-extras-test.")
        os.chmod(self.d, 0o755)
        self.extras = os.path.join(self.d, "extras")
        self.log = os.path.join(self.d, "var/log/nginx")
        self.rot = os.path.join(self.d, "etc/logrotate.d")
        self.hist = os.path.join(self.d, "hist")
        self.ours = os.path.join(self.rot, "s64-cachelog")
        self.gp = os.path.join(self.rot, "nginx")
        self.passed, self.failed, self.skipped = 0, [], 0

    def relocate(self, s):
        return re.sub("|".join(re.escape(p) for p in REAL_PATHS), lambda m: self.d + m.group(0), s)

    # -- fake server -----------------------------------------------------------
    def build(self):
        shutil.copytree(os.path.join(REPO, "extras"), self.extras)
        for f in ("s64-nginx-perf.sh", "s64-slow-catch.sh"):         # the cache-log installer needs no
            p = os.path.join(self.extras, "perf-digest", f)           # edit: it has GP_CACHELOG_ROOT
            write(p, self.relocate(read(p)), 0o755)
        for d in (self.log, self.rot, self.hist, os.path.join(self.d, "etc/nginx/conf.d")):
            os.makedirs(d, exist_ok=True)
        write(os.path.join(self.d, "etc/s64-nginx-perf.conf"), 'SITES="%s"\n' % SITE)

    def site(self, s):
        """A GridPane Redis page-cache site, reduced to the lines the extras look at."""
        os.makedirs(os.path.join(self.d, "var/www", s, "nginx"), exist_ok=True)
        write(os.path.join(self.d, "etc/nginx/sites-available", s),
              "server {\n    server_name %s;\n    include /var/www/%s/nginx/*-main-context.conf;\n"
              "    include /etc/nginx/common/%s-wp-redis.conf;\n}\n" % (s, s, s))

    def put(self, name, lines):
        """Write a log file (gzip when the name says so) into the fake /var/log/nginx."""
        p = os.path.join(self.log, name)
        with (gzip.open(p, "wt") if name.endswith(".gz") else open(p, "w")) as f:
            f.write("".join(lines))

    def logs(self):
        return sorted(os.listdir(self.log))

    # -- running things ----------------------------------------------------------
    def sh(self, args, **env):
        r = subprocess.run(args, env=dict(os.environ, **env), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=300)
        return r.returncode, r.stdout + r.stderr

    def cachelog(self, *args):
        return self.sh(["bash", os.path.join(self.extras, "cache-log/install.sh")] + list(args),
                       GP_CACHELOG_ROOT=self.d)

    def digest(self):
        """Dry run for DAY -> (requests in the history row, HIT rows, MISS rows, output)"""
        digest = os.path.join(self.extras, "perf-digest")
        rc, out = self.sh(["bash", os.path.join(digest, "s64-nginx-perf.sh")], S64_DRY_RUN="1",
                          S64_HIST_DIR=self.hist, S64_DATE=DAY.isoformat(),
                          S64_CATCH=os.path.join(digest, "s64-slow-catch.sh"))
        csv = os.path.join(self.hist, SITE + ".csv")
        row = read(csv).splitlines()[-1].split(",") if os.path.exists(csv) else []
        total = int(row[1]) if len(row) > 1 and row[0] == DAY.isoformat() else None
        n = lambda label: int((re.search(r"(?m)^%s +(\d+)" % label, out) or [0, -1])[1])   # noqa: E731
        return total, n("HIT"), n("MISS"), "exit %d\n%s" % (rc, out)

    def snapshot(self):
        out = {}
        for top in ("etc", "var/www"):
            for dp, _, fs in os.walk(os.path.join(self.d, top)):
                for f in fs:
                    with open(os.path.join(dp, f), "rb") as fh:
                        out[os.path.join(dp, f)] = hashlib.sha256(fh.read()).hexdigest()
        return out

    # -- checks -----------------------------------------------------------------
    def section(self, name):
        print("\n" + c("1", name))

    def check(self, name, ok, detail=""):
        if ok:
            self.passed += 1
            print("  " + c("32", "PASS") + " " + name)
        else:
            self.failed.append(name)
            print("  " + c("31", "FAIL") + " " + name + ("\n       " + detail.replace("\n", "\n       ") if detail else ""))

    def skip(self, why):
        self.skipped += 1
        print("  " + c("33", "SKIP") + " " + why)


def cachelog_scenarios(T):
    src = read(os.path.join(T.extras, "cache-log/s64-cachelog.logrotate"))
    T.site(SITE)
    T.site("blog.test")

    T.section("1. Cache-log install on a server with GridPane's logrotate v1 (*.access.log / *.error.log only)")
    write(T.gp, GP_V1)
    rc, out = T.cachelog(SITE)
    T.check("install: exit code 0", rc == 0, out[-800:])
    T.check("own logrotate file installed, identical to the checkout's, mode 644, and reported",
            os.path.exists(T.ours) and read(T.ours) == src and os.stat(T.ours).st_mode & 0o777 == 0o644
            and "rotation: %s" % T.ours in out, out[-800:])
    ours, gp = stanzas(src), stanzas(GP_V1)
    T.check("it has one stanza with one pattern: /var/log/nginx/*.cache.log",
            len(ours) == 1 and ours[0][0] == ["/var/log/nginx/*.cache.log"], str(ours))
    gp_logs = ["/var/log/nginx/" + f for f in ("access.log", "a.com.access.log", "error.log", "a.com.error.log", "6g.log", "7g.log")]
    T.check("no overlap with GridPane's patterns, in either direction",
            not any(fnmatch.fnmatchcase(f, ours[0][0][0]) for f in gp_logs)
            and not any(fnmatch.fnmatchcase("/var/log/nginx/a.com.cache.log", p) for pats, _ in gp for p in pats))
    T.check("same settings as GridPane's access-log stanza (daily, maxsize, dated names, compress, USR1 postrotate)",
            ours[0][1] == gp[0][1], "%s\n%s" % (ours[0][1], gp[0][1]))
    T.check("GridPane's file is not edited", read(T.gp) == GP_V1)

    T.section("2. Re-running the same install changes nothing")
    before = T.snapshot()
    rc, out = T.cachelog(SITE)
    T.check("exit code 0, 'nothing changed'", rc == 0 and "nothing changed" in out, out[-800:])
    T.check("every file byte-identical", T.snapshot() == before)
    rc, out = T.cachelog("--status")
    T.check("--status: OK, rotation file reported as current", rc == 0 and "rotation: %s (current)" % T.ours in out
            and out.rstrip().endswith("OK"), out[-800:])

    T.section("3. Rotation file changed or lost: --status says so, the next install puts it back")
    write(T.ours, src.replace("maxsize 10M", "maxsize 99M"))
    rc, out = T.cachelog("--status")
    T.check("edited file: --status reports it and exits 1",
            rc == 1 and "rotation: %s (differs from this checkout - run install)" % T.ours in out, out[-800:])
    rc, out = T.cachelog(SITE)
    T.check("install restores it without an nginx reload", rc == 0 and read(T.ours) == src
            and "nginx files unchanged, nginx not reloaded" in out, out[-800:])
    os.remove(T.ours)
    rc, out = T.cachelog("--status")
    T.check("missing file while a site logs: --status says nothing rotates the logs and exits 1",
            rc == 1 and "!! rotation: nothing rotates /var/log/nginx/*.cache.log" in out, out[-800:])
    rc, out = T.cachelog("blog.test")
    T.check("enabling another site restores it", rc == 0 and read(T.ours) == src, out[-800:])

    T.section("4. Another logrotate file already matches the cache logs (GridPane before v1: /var/log/nginx/*log)")
    write(T.gp, GP_OLD)
    rc, out = T.cachelog("--status")
    T.check("--status: both files match the cache logs, exits 1",
            rc == 1 and "!! rotation: %s and %s both match" % (T.ours, T.gp) in out, out[-800:])
    rc, out = T.cachelog(SITE)
    T.check("install removes its own file and leaves rotation to the other one",
            rc == 0 and not os.path.exists(T.ours) and "rotation: left to %s" % T.gp in out, out[-800:])
    T.check("the other file is not edited", read(T.gp) == GP_OLD)
    rc, out = T.cachelog("--status")
    T.check("--status: OK, names the file that rotates the logs", rc == 0 and "rotation: by %s" % T.gp in out, out[-800:])
    write(T.gp, GP_V1)                              # GridPane then syncs its v1 file
    rc, out = T.cachelog("--status")
    T.check("after GridPane's switch to v1: --status says nothing rotates the logs, exits 1",
            rc == 1 and "!! rotation: nothing rotates" in out, out[-800:])
    write(os.path.join(T.rot, "zz-note"), "# /var/log/nginx/*log used to be rotated here\n/var/log/other/*.log {\n    weekly\n}\n")
    rc, out = T.cachelog(SITE)
    T.check("install adds its own file again (a pattern in a comment does not count)",
            rc == 0 and os.path.exists(T.ours) and read(T.ours) == src, out[-800:])
    os.remove(os.path.join(T.rot, "zz-note"))

    T.section("5. --disable keeps the rotation, --uninstall removes it")
    rc, out = T.cachelog("--disable", "blog.test")
    T.check("--disable: rotation file stays (other sites still log)", rc == 0 and read(T.ours) == src, out[-800:])
    rc, out = T.cachelog("--uninstall")
    T.check("--uninstall: rotation file removed, GridPane's file untouched",
            rc == 0 and not os.path.exists(T.ours) and read(T.gp) == GP_V1 and "no longer rotated" in out, out[-800:])
    rc, out = T.cachelog("--status")
    T.check("--status afterwards: OK, nothing installed", rc == 0 and "rotation: not installed" in out, out[-800:])
    rc, out = T.cachelog(SITE)                      # back on, for the logrotate run below
    T.check("install again: exit code 0, rotation file back", rc == 0 and read(T.ours) == src, out[-800:])


def digest_scenarios(T):
    T.section("6. Digest: one day spread over the live log and rotated files of both naming schemes")
    rc, _ = T.sh(["date", "-d", DAY.isoformat(), "+%d/%b/%Y"])
    if rc != 0:
        T.skip("the digest needs GNU date (date -d); not available here")
        return False
    nxt = (DAY + datetime.timedelta(days=1)).strftime("%Y%m%d")
    alog, clog = SITE + ".access.log", SITE + ".cache.log"
    hm = lambda h, i: "%02d:%02d:00" % (h, i)       # noqa: E731
    # access log: 4 + 3 + 5 + 2 = 14 requests on DAY; 3 of another day must not be counted
    T.put(alog, [access(DAY, hm(14, i), "/live-%d/" % i) for i in range(4)])
    T.put(alog + ".1.gz", [access(OLD, hm(23, i), "/old-%d/" % i) for i in range(2)]
          + [access(DAY, hm(9, i), "/numbered-%d/" % i) for i in range(3)])
    T.put(alog + "-%s-1795129201.gz" % nxt, [access(OLD, hm(22, 0), "/old/")]
          + [access(DAY, hm(11, i), "/dated-%d/" % i) for i in range(5)])
    T.put(alog + "-%s-1795136400" % nxt, [access(DAY, hm(12, i), "/dated-plain-%d/" % i) for i in range(2)])
    # cache log: HIT 3 + 5 + 7 = 15, MISS 5 on DAY; the dated file starts the day at 00:00:30
    T.put(clog, [cache(DAY, hm(14, i), "/live-%d/" % i) for i in range(3)])
    T.put(clog + ".1.gz", [cache(OLD, hm(23, i), "/old-%d/" % i) for i in range(2)]
          + [cache(DAY, hm(9, i), "/numbered-%d/" % i) for i in range(5)])
    T.put(clog + "-%s-1795129201.gz" % nxt, [cache(DAY, "00:00:30", "/first/")]
          + [cache(DAY, hm(11, i), "/dated-%d/" % i) for i in range(6)]
          + [cache(DAY, hm(11, 30 + i), "/dated-miss-%d/" % i, "MISS") for i in range(5)])
    total, hit, miss, out = T.digest()
    T.check("requests: all 14 of the day (live 4 + numbered .gz 3 + dated .gz 5 + dated, not yet compressed 2)",
            total == 14, "requests in the history row: %s\n%s" % (total, out[-1500:]))
    T.check("exact cache split read from the cache logs (Redis exact)", "page cache (Redis exact)" in out, out[-1500:])
    T.check("cache HIT 15 (live 3 + numbered 5 + dated 7), MISS 5 (dated)", (hit, miss) == (15, 5),
            "HIT %s MISS %s\n%s" % (hit, miss, out[-1500:]))
    T.check("the split covers the whole day (no 'cache log started at' note)", "cache split covers" not in out, out[-1500:])
    rc, out = T.sh(["bash", os.path.join(T.extras, "perf-digest/s64-slow-catch.sh"), SITE, DAY.isoformat()])
    T.check("s64-slow-catch.sh reads the same 14 lines", rc == 0 and "· 14 log lines" in out, out[-800:])
    return True


def logrotate_scenarios(T, digest_ran):
    T.section("7. The real logrotate: installed file next to GridPane's v1 stanzas, then the digest again")
    if not digest_ran or not shutil.which("logrotate") or os.geteuid() != 0:
        T.skip("needs the digest part above, the logrotate binary and root (the stanzas create files as root:root)")
        return
    run = os.path.join(T.d, "rotate-run")
    for name, text in (("nginx", GP_V1), ("s64-cachelog", read(T.ours))):
        text = T.relocate(text)
        if re.search(r"(?<!%s)(/var/log/|/var/run/)" % re.escape(T.d), text):
            raise SetupError("a real path is left in the relocated logrotate file %s" % name)
        write(os.path.join(run, "d", name), text)
    write(os.path.join(run, "main.conf"), "include %s\n" % os.path.join(run, "d"))
    state = os.path.join(T.d, "logrotate.state")
    rotate = lambda: T.sh(["logrotate", "--state", state, os.path.join(run, "main.conf")])   # noqa: E731
    alog, clog = SITE + ".access.log", SITE + ".cache.log"
    fresh = lambda before, log: [f for f in T.logs() if f not in before and re.fullmatch(re.escape(log) + DATED + r"\.gz", f)]   # noqa: E731

    before = T.logs()
    rc, out = rotate()
    T.check("logrotate accepts both files: exit code 0, no error (no log matched twice)",
            rc == 0 and "error" not in out.lower(), "exit %d\n%s" % (rc, out[-800:]))
    T.check("small logs seen for the first time are left alone", T.logs() == before, " ".join(T.logs()))

    # the backlog of a server where nothing rotated the cache log: > maxsize, mostly older days
    live = read(os.path.join(T.log, clog))
    T.put(clog, [cache(OLD, "%02d:%02d:%02d" % (i // 3600 % 24, i // 60 % 60, i % 60), "/backlog-%d/" % i) for i in range(75000)] + [live])
    size = os.path.getsize(os.path.join(T.log, clog))
    before = T.logs()
    rc, out = rotate()
    new = fresh(before, clog)
    T.check("a %d MB cache-log backlog (over maxsize 10M) is rotated on the next run: exit code 0" % (size >> 20),
            rc == 0 and size > 10 << 20 and "error" not in out.lower(), "exit %d\n%s" % (rc, out[-800:]))
    T.check("...into one compressed file with GridPane's dated name (<site>.cache.log-YYYYMMDD-<epoch>.gz)",
            len(new) == 1, " ".join(T.logs()))
    st = os.stat(os.path.join(T.log, clog))
    T.check("...and a new empty live log, mode 644", st.st_size == 0 and st.st_mode & 0o777 == 0o644)
    T.check("the access log (under maxsize, already seen today) is not rotated", not fresh(before, alog), " ".join(T.logs()))
    total, hit, miss, out = T.digest()
    T.check("digest after that rotation: same day, same numbers (14 requests, HIT 15, MISS 5), whole day covered",
            (total, hit, miss) == (14, 15, 5) and "cache split covers" not in out,
            "requests %s HIT %s MISS %s\n%s" % (total, hit, miss, out[-1500:]))

    # midnight: the daily rotation is due for every log
    with open(os.path.join(T.log, clog), "a") as f:
        f.write(cache(DAY, "23:58:00", "/late/"))
    write(state, re.sub(r"(?m) \d+-\d+-\d+-\d+:\d+:\d+$", " 2020-1-1-0:0:0", read(state)))
    time.sleep(1.1)                                 # the dated name carries the second of the rotation
    before = T.logs()
    rc, out = rotate()
    T.check("daily rotation: exit code 0, access log and cache log both get a dated .gz",
            rc == 0 and "error" not in out.lower() and len(fresh(before, alog)) == 1 and len(fresh(before, clog)) == 1,
            "exit %d\n%s\n%s" % (rc, out[-800:], " ".join(T.logs())))
    total, hit, miss, out = T.digest()
    T.check("digest with every file rotated: 14 requests, HIT 16 (one more page was served), MISS 5",
            (total, hit, miss) == (14, 16, 5), "requests %s HIT %s MISS %s\n%s" % (total, hit, miss, out[-1500:]))


def classify_scenarios(T, digest_ran):
    T.section("8. Digest: a GET on an ?add-to-cart= link is a crawler, not a page for a person")
    if not digest_ran:
        T.skip("needs the digest part above (GNU date)")
        return
    count = lambda out, label: int((re.search(r"%s (\d+)" % label, out) or [0, -1])[1])   # noqa: E731
    _, _, miss0, out = T.digest()
    pages0, bots0 = count(out, "pages"), count(out, "bots")
    # browser user agent on all three; only the first two carry the add-to-cart parameter
    uris = ["/produkt/soap/?add-to-cart=12", "/kategoria/soap/?per_page=6&add-to-cart=12", "/?s=add-to-cart"]
    for log, line in ((SITE + ".access.log", access), (SITE + ".cache.log", lambda d, t, u: cache(d, t, u, "MISS"))):
        with open(os.path.join(T.log, log), "a") as f:
            f.write("".join(line(DAY, "15:0%d:00" % i, u) for i, u in enumerate(uris)))
    _, _, miss, out = T.digest()
    pages, bots = count(out, "pages"), count(out, "bots")
    T.check("the two add-to-cart GETs count as bots", bots == bots0 + 2, "bots %s -> %s\n%s" % (bots0, bots, out[-1500:]))
    T.check("a search for the words 'add-to-cart' stays a page", pages == pages0 + 1,
            "pages %s -> %s\n%s" % (pages0, pages, out[-1500:]))
    T.check("the cache split leaves them out too (MISS grows by the one page only)", miss == miss0 + 1,
            "MISS %s -> %s\n%s" % (miss0, miss, out[-1500:]))


def main():
    T = Root()
    try:
        T.build()
        print("extras test root: %s" % T.d)
        try:
            cachelog_scenarios(T)
            digest_ran = digest_scenarios(T)
            logrotate_scenarios(T, digest_ran)
            classify_scenarios(T, digest_ran)
        except (OSError, subprocess.SubprocessError, AssertionError) as e:   # a broken script can leave
            T.check("test run completed", False, "%s: %s" % (e.__class__.__name__, e))   # files missing
    except SetupError as e:
        print(c("31", "SETUP ERROR: ") + str(e))
        return 2
    finally:
        shutil.rmtree(T.d, ignore_errors=True)
    total = T.passed + len(T.failed)
    skipped = " (%d part%s skipped, see above)" % (T.skipped, "" if T.skipped == 1 else "s") if T.skipped else ""
    print()
    if T.failed:
        print(c("31", "%d of %d extras checks FAILED%s" % (len(T.failed), total, skipped)))
        for n in T.failed:
            print("  - " + n)
        return 1
    print(c("32", "ALL %d EXTRAS CHECKS PASSED%s" % (total, skipped)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
