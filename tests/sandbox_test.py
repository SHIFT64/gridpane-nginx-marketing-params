#!/usr/bin/env python3
"""
Sandbox test - safe to run on any GridPane server (even production):

  * builds a throw-away nginx instance in /tmp on a random 127.0.0.1 port,
  * using the server's own nginx binary + GridPane's real FastCGI cache template
    (/etc/nginx/common/wpfc.conf, or a rendered /etc/nginx/common/<site>-wpfc.conf with --site),
  * plus the files from THIS repo (engine, list, hooks, switches),
  * with a fake PHP backend (tests/fake_php.py) instead of PHP-FPM/WordPress,
  * runs the whole behaviour matrix and removes everything again.

The live nginx, its cache and the sites are never touched.

  ./test.sh                     generic GridPane template
  ./test.sh --site example.com  that site's rendered template
  ./test.sh -v                  also print passing checks' details
  ./test.sh --keep              keep the sandbox dir for inspection
"""
import argparse
import glob
import http.client
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import fake_php  # noqa: E402

NGINX = shutil.which("nginx") or "/usr/sbin/nginx"
MAX_PARAMS, MAX_LEN = 24, 2048
LIVE_DIR = "/etc/nginx/marketing-params"
HOST = "test.example"
OFF_HOST = "off.example"

COLOR = sys.stdout.isatty()


def c(code, s):
    return "\033[%sm%s\033[0m" % (code, s) if COLOR else s


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def read(p):
    with open(p, encoding="utf-8", errors="replace") as f:
        return f.read()


def write(p, s, mode=0o644):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(s)
    os.chmod(p, mode)


class SetupError(Exception):
    pass


# --------------------------------------------------------------------------- sandbox

class Sandbox:
    def __init__(self, site=None, keep=False):
        self.site = site
        self.keep = keep
        self.d = tempfile.mkdtemp(prefix="gp-mkt-test.")
        os.chmod(self.d, 0o755)
        self.port = free_port()
        self.fport = free_port()
        self.started = False
        self.template = None

    def p(self, *a):
        return os.path.join(self.d, *a)

    def relocate(self, s):
        return s.replace(LIVE_DIR, self.p("marketing-params"))

    # -- GridPane template ---------------------------------------------------
    def wpfc(self, site_dir, name):
        if self.site:
            tpl = "/etc/nginx/common/%s-wpfc.conf" % self.site
            var = "/etc/nginx/common/%s-fcgi-cache-var.conf" % self.site
            if not os.path.exists(tpl):
                raise SetupError("%s not found (does the site use FastCGI caching?)" % tpl)
            s = read(tpl)
            needed = ["/var/www/%s/nginx/" % self.site]
            for n in needed:
                if n not in s:
                    raise SetupError("template %s: expected '%s' not found" % (tpl, n))
            s = s.replace("/var/www/%s/nginx/" % self.site, site_dir + "/")
        else:
            tpl = "/etc/nginx/common/wpfc.conf"
            var = "/etc/nginx/common/fcgi-cache-var.conf"
            if not os.path.exists(tpl):
                raise SetupError("%s not found - is this a GridPane server?" % tpl)
            s = read(tpl)
            # emulate GridPane's per-site rendering of the generic template
            renders = {
                "include /etc/nginx/extra.d/*-skip-cache-context.conf;":
                    "include %s/*skip-fcgi-cache-context.conf;" % site_dir,
                "include /etc/nginx/extra.d/*-root-context.conf;":
                    "include %s/*-root-context.conf;" % site_dir,
                "include /etc/nginx/extra.d/*-php-context.conf;":
                    "include %s/*-php-context.conf;" % site_dir,
            }
            for a, b in renders.items():
                if a not in s:
                    raise SetupError("GridPane template changed: '%s' not found in %s" % (a, tpl))
                s = s.replace(a, b)
        for must in ['if ($query_string != "")', "$skip_reason", "fastcgi_cache FASTCGICACHE"]:
            if must not in s:
                raise SetupError("GridPane template changed: '%s' not found in %s" % (must, tpl))
        inc = "include %s;" % var
        if inc not in s:
            raise SetupError("template %s does not include %s" % (tpl, var))
        s = s.replace(inc, "include %s;" % self.p("fcgi-cache-var.conf"))
        s = s.replace("/etc/nginx/extra.d/", self.p("extra.d") + "/")
        self.template = tpl
        v = read(var)
        if "fastcgi_cache_key" not in v:
            raise SetupError("%s has no fastcgi_cache_key" % var)
        v = re.sub(r"fastcgi_cache_valid\s+200\s+\S+;", "fastcgi_cache_valid 200 300s;", v)
        write(self.p("fcgi-cache-var.conf"), v)
        write(self.p(name), s)
        return v

    # -- build ---------------------------------------------------------------
    def build(self, variant="full"):
        d = self.d
        mp = self.p("marketing-params")
        os.makedirs(mp, exist_ok=True)
        eng = subprocess.run([sys.executable, os.path.join(REPO, "build-engine.py"), str(MAX_PARAMS), str(MAX_LEN), mp],
                             capture_output=True, text=True, check=True).stdout
        write(os.path.join(mp, "engine.conf"), eng)
        shutil.copy(os.path.join(REPO, "server/marketing-params/params.list"), os.path.join(mp, "params.list"))
        write(os.path.join(mp, "php-context.conf"),
              self.relocate(read(os.path.join(REPO, "server/marketing-params/php-context.conf"))))
        # stray copies an admin might leave behind - must be ignored (exact-name includes)
        stray = subprocess.run([sys.executable, os.path.join(REPO, "build-engine.py"), "1", "64", mp],
                               capture_output=True, text=True, check=True).stdout
        write(os.path.join(mp, "engine.old.conf"), stray)
        write(os.path.join(mp, "php-context.orig.conf"), read(os.path.join(mp, "php-context.conf")))
        write(self.p("stub-http.conf"), self.relocate(read(os.path.join(REPO, "server/stubs/conf.d-marketing-params.conf"))))
        os.makedirs(self.p("extra.d"), exist_ok=True)
        write(self.p("site", "marketing-params-php-context.conf"),
              self.relocate(read(os.path.join(REPO, "site", "marketing-params-php-context.conf"))))
        # a site's own customisations that must keep working (and win):
        write(self.p("site", "custom-root-context.conf"),
              "rewrite ^/rw/$ /index.php?lang=en last;\n"
              "rewrite ^/old-missing/$ /missing.php?x=1 last;\n")
        write(self.p("site", "custom-skip-fcgi-cache-context.conf"),
              'if ($arg_utm_source = "nocache") {\n'
              '    set $skip_cache 1;\n'
              '    set $skip_reason "${skip_reason}-custom";\n'
              '}\n')
        os.makedirs(self.p("site-off"), exist_ok=True)
        write(self.p("htdocs/index.php"), "")
        for t in ("body", "fcgi", "proxy", "uwsgi", "scgi"):
            os.makedirs(self.p("tmp", t), exist_ok=True)
        os.makedirs(self.p("cache"), exist_ok=True)
        self.wpfc(self.p("site"), "wpfc.conf")
        self.wpfc(self.p("site-off"), "wpfc-off.conf")
        write(self.p("nginx.conf"), self.nginx_conf())
        if os.geteuid() == 0:
            subprocess.run(["chown", "-R", "www-data:www-data", self.p("cache"), self.p("tmp")], check=False)

    def nginx_conf(self, engine=True, switches_dir=None, wpfc=True, off_server=True):
        mods = []
        for m in ("ndk_http_module", "ngx_http_set_misc_module", "ngx_http_headers_more_filter_module"):
            so = "/etc/nginx/modules/%s.so" % m
            if not os.path.exists(so):
                raise SetupError("nginx module missing: %s" % so)
            mods.append("load_module %s;" % so)
        fc = read("/etc/nginx/conf.d/fastcgi.conf")
        fc = re.sub(r"^\s*fastcgi_cache_path[^\n]*\n", "", fc, flags=re.M)
        user = "user www-data;" if os.geteuid() == 0 else ""
        site = switches_dir or self.p("site")
        servers = []
        if wpfc:
            servers.append("""
    server {
        listen 127.0.0.1:%(port)d;
        server_name %(host)s www.%(host)s;
        root %(d)s/htdocs;
        index index.php;
        set $sockfile php;   # GridPane sets this in <site>-sockfile.conf
        include %(site)s/*-main-context.conf;
        include %(d)s/wpfc.conf;
    }""" % dict(port=self.port, host=HOST, d=self.d, site=site))
            if off_server:
                servers.append("""
    server {
        listen 127.0.0.1:%(port)d;
        server_name %(off)s;
        root %(d)s/htdocs;
        index index.php;
        set $sockfile php;
        include %(d)s/site-off/*-main-context.conf;
        include %(d)s/wpfc-off.conf;
    }""" % dict(port=self.port, off=OFF_HOST, d=self.d))
        else:
            servers.append("""
    server {
        listen 127.0.0.1:%(port)d;
        server_name %(host)s;
        root %(d)s/htdocs;
        location ~ \\.php$ { include /etc/nginx/fastcgi_params; fastcgi_pass php; }
    }""" % dict(port=self.port, host=HOST, d=self.d))
        return """%(mods)s
%(user)s
worker_processes 1;
pid %(d)s/nginx.pid;
error_log %(d)s/error.log warn;
events { worker_connections 512; }
http {
    access_log off;
    client_body_temp_path %(d)s/tmp/body;
    fastcgi_temp_path %(d)s/tmp/fcgi;
    proxy_temp_path %(d)s/tmp/proxy;
    uwsgi_temp_path %(d)s/tmp/uwsgi;
    scgi_temp_path %(d)s/tmp/scgi;
    server_names_hash_bucket_size 256;
    variables_hash_max_size 2048;
    client_header_buffer_size 13k;
    large_client_header_buffers 4 52k;
    fastcgi_cache_path %(d)s/cache levels=1:2 keys_zone=FASTCGICACHE:10m max_size=100m inactive=1h;
%(fc)s
    %(engine)s
    upstream php { server 127.0.0.1:%(fport)d; }
%(servers)s
}
""" % dict(mods="\n".join(mods), user=user, d=self.d, fc=fc, fport=self.fport,
           engine=("include %s;" % self.p("stub-http.conf")) if engine else "",
           servers="\n".join(servers))

    def nginx_t(self, conf_text, name):
        path = self.p(name)
        write(path, conf_text)
        r = subprocess.run([NGINX, "-t", "-p", self.d, "-c", path], capture_output=True, text=True)
        return r.returncode == 0, (r.stdout + r.stderr).strip()

    def start(self):
        fake_php.start(self.fport)
        ok, out = self.nginx_t(read(self.p("nginx.conf")), "nginx.conf")
        if not ok:
            raise SetupError("nginx -t failed for the sandbox:\n" + out)
        self.t_output = out
        r = subprocess.run([NGINX, "-p", self.d, "-c", self.p("nginx.conf")], capture_output=True, text=True)
        if r.returncode != 0:
            raise SetupError("cannot start sandbox nginx:\n" + r.stdout + r.stderr)
        self.started = True
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        raise SetupError("sandbox nginx did not start listening")

    def stop(self):
        if self.started:
            subprocess.run([NGINX, "-p", self.d, "-c", self.p("nginx.conf"), "-s", "stop"], capture_output=True)
            for _ in range(30):
                if not os.path.exists(self.p("nginx.pid")):
                    break
                time.sleep(0.1)
        if not self.keep:
            shutil.rmtree(self.d, ignore_errors=True)


# --------------------------------------------------------------------------- checks

class Runner:
    def __init__(self, sbx, verbose=False):
        self.sbx = sbx
        self.verbose = verbose
        self.passed = 0
        self.failed = []
        self.group = ""

    def section(self, name):
        self.group = name
        print("\n" + c("1", name))

    def check(self, name, ok, detail=""):
        if ok:
            self.passed += 1
            print("  " + c("32", "PASS") + " " + name + (("  [" + detail + "]") if self.verbose and detail else ""))
        else:
            self.failed.append((self.group, name, detail))
            print("  " + c("31", "FAIL") + " " + name + ("\n       " + detail if detail else ""))

    def req(self, target, host=HOST, method="GET", headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.sbx.port, timeout=15)
        conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host)
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        data = body.encode() if body is not None else None
        if data is not None:
            conn.putheader("Content-Length", str(len(data)))
            conn.putheader("Content-Type", "application/x-www-form-urlencoded")
        conn.endheaders(data)
        r = conn.getresponse()
        raw = r.read()
        h = {}
        for k, v in r.getheaders():
            h.setdefault(k.lower(), v)
        conn.close()
        return r.status, h, raw.decode("latin1")

    def purge(self, path, host=HOST):
        self.req("/purge" + path, host)
        self.req("/purge" + path, host, method="HEAD")


def summary(h):
    return "status=%s cache=%s skip=%s mkt=%s php_uri=%s php_qs=%s loc=%s" % (
        h.get(":status"), h.get("x-grid-cache"), h.get("x-grid-cache-skip"), h.get("x-grid-cache-mkt"),
        h.get("x-test-uri"), h.get("x-test-qs"), h.get("location"))


def run_matrix(R):
    def get(target, **kw):
        st, h, body = R.req(target, **kw)
        h[":status"] = st
        return h, body

    def is_ignored(h):
        return h.get("x-grid-cache-mkt") == "ignored" and h.get("x-grid-cache") != "BYPASS"

    def is_stock_bypass(h, target):
        return (h.get("x-grid-cache") == "BYPASS" and "-query_string" in (h.get("x-grid-cache-skip") or "")
                and "x-grid-cache-mkt" not in h and h.get("x-test-uri") == target)

    # ------------------------------------------------------------------
    R.section("1. Marketing variants share the cache entry of the clean URL")
    R.purge("/a/")
    h0, _ = get("/a/")
    R.check("clean URL is cached normally (MISS)", h0.get("x-grid-cache") == "MISS", summary(h0))
    gen = h0.get("x-test-gen")
    for q in ["gclid=1", "gclid=2&utm_source=fb&utm_medium=cpc&fbclid=x", "UTM_Source=X&GCLID=Y",
              "gad_source=1&gad_campaignid=123&gbraid=a&wbraid=b", "_gl=1*abc*_ga*MTIz&msclkid=9",
              "hsa_acc=1&hsa_cam=2&hsa_grp=3&hsa_ad=4", "mtm_campaign=x&pk_source=y&ttclid=z"]:
        h, _ = get("/a/?" + q)
        R.check("/a/?%s -> HIT of /a/" % q[:50],
                h.get("x-grid-cache") == "HIT" and h.get("x-test-gen") == gen and h.get("x-grid-cache-mkt") == "ignored",
                summary(h))
    h, _ = get("/a/?gclid=1")
    R.check("no X-Grid-Cache-Skip on an ignored request", "x-grid-cache-skip" not in h, summary(h))
    R.check("200 page gets no Location / no redirect", "location" not in h and h[":status"] == 200, summary(h))

    # ------------------------------------------------------------------
    R.section("2. First visitor with params: PHP sees the clean URL (no cache poisoning)")
    R.purge("/echo/p/")
    h, body = get("/echo/p/?gclid=POISON1&utm_source=POISON2&fbclid=POISON3")
    R.check("MISS stored", h.get("x-grid-cache") == "MISS", summary(h))
    R.check("PHP REQUEST_URI is the clean path", h.get("x-test-uri") == "/echo/p/", summary(h))
    R.check("PHP QUERY_STRING is empty", h.get("x-test-qs") == "", summary(h))
    R.check("generated HTML contains no param values", "POISON" not in body, body[:120])
    h2, body2 = get("/echo/p/")
    R.check("clean URL then gets the same entry (HIT, identical body)",
            h2.get("x-grid-cache") == "HIT" and body2 == body, summary(h2))
    R.check("exactly one QUERY_STRING sent to PHP", h.get("x-test-n-qs") == "1", "n=%s" % h.get("x-test-n-qs"))

    # ------------------------------------------------------------------
    R.section("3. Any other param keeps stock GridPane behaviour (BYPASS, PHP sees the original)")
    for q in ["page=2", "gclid=1&page=2", "page=2&gclid=1", "s=foo&utm_source=x", "mc_cid=1", "mc_eid=1",
              "ref=abc", "gclidx=1", "xgclid=1", "my_utm_source=1", "utm=1", "a=1%26gclid%3D2",
              "utm%5Fsource=x", "gclid[]=1", "=x&gclid=1", "add-to-cart=5&utm_source=x", "lang=en&gclid=1",
              "cn-reloaded=1", "age-verified=1", "ao_noptimize=1"]:
        t = "/b/?" + q
        h, _ = get(t)
        R.check(t, is_stock_bypass(h, t), summary(h))
    h, _ = get("/b/?page=2")
    R.check("bypass sends exactly one QUERY_STRING and one REQUEST_URI",
            h.get("x-test-n-qs") == "1" and h.get("x-test-n-uri") == "1",
            "n_qs=%s n_uri=%s" % (h.get("x-test-n-qs"), h.get("x-test-n-uri")))

    # ------------------------------------------------------------------
    R.section("4. Parser edge cases that are still marketing-only")
    for q in ["gclid=", "gclid", "&gclid=1&", "gclid=1&&utm_source=2", "gclid=1;s=foo", "utm_source[]=x",
              "gclid=a=b", "gclid=1%26s%3Dfoo", "gclid=1&gclid=2", "UTM_CAMPAIGN=x", "fbclid=" + "x" * 300]:
        h, _ = get("/c/?" + q)
        R.check("/c/?%s -> ignored" % q[:40], is_ignored(h), summary(h))
    h, _ = get("/c/?")
    R.check("bare '?' -> not skipped by GridPane at all (stock), no mkt header",
            h.get("x-grid-cache") != "BYPASS" and "x-grid-cache-mkt" not in h, summary(h))

    # ------------------------------------------------------------------
    R.section("5. Limits fall back to stock behaviour")
    qmax = "&".join("utm_p%d=v" % i for i in range(1, MAX_PARAMS + 1))
    h, _ = get("/c/?" + qmax)
    R.check("%d marketing params -> ignored" % MAX_PARAMS, is_ignored(h), summary(h))
    h, _ = get("/c/?" + qmax + "&utm_px=v")
    R.check("%d marketing params -> stock BYPASS" % (MAX_PARAMS + 1),
            h.get("x-grid-cache") == "BYPASS" and "x-grid-cache-mkt" not in h, summary(h)[:200])
    h, _ = get("/c/?&&" + qmax.replace("&", "&&&") + "&&")
    R.check("empty '&&' segments do not count towards the limit", is_ignored(h), summary(h)[:200])
    lp = "/c/" + "p" * 9000 + "/?gclid=1"
    h, _ = get(lp)
    R.check("path longer than 8 KB + marketing param -> stock BYPASS (not doubled to PHP)",
            h.get("x-grid-cache") == "BYPASS" and "x-grid-cache-mkt" not in h, summary(h)[:200])
    q2048 = "gclid=" + "x" * (2048 - 6)
    h, _ = get("/c/?" + q2048)
    R.check("2048-byte marketing query -> ignored", is_ignored(h), summary(h)[:200])
    h, _ = get("/c/?" + q2048 + "x")
    R.check("2049-byte marketing query -> stock BYPASS",
            h.get("x-grid-cache") == "BYPASS" and "x-grid-cache-mkt" not in h, summary(h)[:200])
    big = "z=" + "y" * 20000
    st, hb, _ = R.req("/b/?" + big)
    R.check("20 KB non-marketing query still works (200, single params)",
            st == 200 and hb.get("x-test-n-qs") == "1" and hb.get("x-test-n-uri") == "1",
            "status=%s n_qs=%s n_uri=%s" % (st, hb.get("x-test-n-qs"), hb.get("x-test-n-uri")))

    # ------------------------------------------------------------------
    R.section("6. Other skip reasons always win")
    st, h, _ = R.req("/d/?gclid=1", method="POST", body="a=1")
    R.check("POST ?gclid=1 -> not cached, PHP sees gclid", h.get("x-test-qs") == "gclid=1" and "x-grid-cache-mkt" not in h,
            "qs=%s mkt=%s" % (h.get("x-test-qs"), h.get("x-grid-cache-mkt")))
    for m in ("PUT", "DELETE", "OPTIONS", "PATCH"):
        st, h, _ = R.req("/d/?gclid=1", method=m)
        R.check("%s ?gclid=1 -> PHP sees gclid" % m, h.get("x-test-qs") == "gclid=1" and "x-grid-cache-mkt" not in h,
                "qs=%s mkt=%s" % (h.get("x-test-qs"), h.get("x-grid-cache-mkt")))
    for ck in ["wordpress_logged_in_abc=1", "woocommerce_items_in_cart=1", "wp_woocommerce_session_x=1",
               "comment_author_x=1", "wp-postpass_x=1", "wordpress_no_cache=1"]:
        t = "/d/?gclid=1"
        h, _ = get(t, headers={"Cookie": ck})
        R.check("cookie %s -> BYPASS, PHP sees original" % ck.split("=")[0],
                h.get("x-grid-cache") == "BYPASS" and h.get("x-test-uri") == t and "x-grid-cache-mkt" not in h, summary(h))
    t = "/c/?gclid=1&utm_source=nocache"
    h, _ = get(t)
    R.check("own skip rule after GridPane's (appends -custom) wins: BYPASS, PHP sees original",
            h.get("x-grid-cache") == "BYPASS" and "-custom" in (h.get("x-grid-cache-skip") or "")
            and h.get("x-test-uri") == t and "x-grid-cache-mkt" not in h, summary(h))
    R.purge("/rw/")
    h, _ = get("/rw/?gclid=1")
    R.check("own location rewrite that adds args (/rw/ -> ?lang=en) is respected: BYPASS, PHP gets lang=en&gclid=1",
            h.get("x-grid-cache") == "BYPASS" and h.get("x-test-qs") == "lang=en&gclid=1"
            and "x-grid-cache-mkt" not in h, summary(h))
    h, _ = get("/rw/")
    R.check("...and the clean /rw/ entry is the stock one (PHP got lang=en)", h.get("x-test-qs") == "lang=en", summary(h))
    st, h, _ = R.req("/old-missing/?gclid=1")
    R.check("rewrite to a missing .php still answers 404 (try_files intact)", st == 404, "status=%s" % st)
    st, h, _ = R.req("/missing.php?gclid=1")
    R.check("missing .php with marketing param -> 404", st == 404, "status=%s" % st)
    for t in ["/cart/?gclid=1", "/checkout/?utm_source=x", "/my-account/?fbclid=1", "/index.php?gclid=1",
              "/feed/?utm_source=x", "/wp-login.php?gclid=1"]:
        h, _ = get(t)
        R.check("%s -> BYPASS, PHP sees original" % t,
                (h.get("x-grid-cache") == "BYPASS" or h[":status"] == 404) and "x-grid-cache-mkt" not in h, summary(h))

    # ------------------------------------------------------------------
    R.section("7. Redirects keep the marketing params (same host only)")
    Q = "gclid=R1&utm_source=x"
    A, U = True, False   # params appended / Location untouched
    expect = {
        "same": ("https://%s/target/?%s" % (HOST, Q), A),
        "sameq": ("https://%s/target/?lang=pl&%s" % (HOST, Q), A),
        "frag": ("https://%s/target/?%s#frag" % (HOST, Q), A),
        "www": ("https://www.%s/target/?%s" % (HOST, Q), A),
        "upper": ("https://%s/target/?%s" % (HOST.upper(), Q), A),
        "port": ("https://%s:8443/target/?%s" % (HOST, Q), A),
        "bare": ("https://%s?%s" % (HOST, Q), A),
        "rel": ("/target/?" + Q, A),
        "relq": ("/target/?lang=pl&" + Q, A),
        "relnoslash": ("target/", U),
        "ext": ("https://example.org/x", U),
        "protorel": ("//evil.example/x", U),
        "backslash": ("/\\evil.example/x", U),
        "tab": ("/\t/evil.example/x", U),
        "suffix": ("https://%s.evil.example/x" % HOST, U),
        "prefix": ("https://evil%s/x" % HOST, U),
        "userinfo": ("https://%s@evil.example/x" % HOST, U),
        "s301": ("https://%s/target/?%s" % (HOST, Q), A),
        "s303": ("https://%s/target/?%s" % (HOST, Q), A),
        "s307": ("https://%s/target/?%s" % (HOST, Q), A),
        "s308": ("https://%s/target/?%s" % (HOST, Q), A),
        "s201": ("https://%s/target/" % HOST, U),
    }
    for kind, (want, appended) in expect.items():
        h, _ = get("/loc/%s/?%s" % (kind, Q))
        loc = h.get("location", "")
        ok = loc == want
        if not appended:
            ok = ok and "gclid" not in loc
        R.check("Location %-10s %s %s" % (kind, "->" if appended else "untouched:", want), ok, "got %r" % loc)
    for kind, want in (("rel", "/target/"), ("protorel", "//evil.example/x"), ("relq", "/target/?lang=pl")):
        for q in ("", "?page=2"):
            h, _ = get("/loc/%s/%s" % (kind, q))
            R.check("non-marketing request: Location %s stays exactly %r (not made absolute)" % (kind + q, want),
                    h.get("location") == want, "got %r" % h.get("location"))
    h, _ = get("/noslash?" + Q)
    R.check("WordPress trailing-slash 301 keeps params",
            h.get("location") == "https://%s/noslash/?%s" % (HOST, Q), summary(h))
    h, _ = get("/f/?" + Q, host="www." + HOST)
    R.check("WordPress www->apex 301 keeps params",
            h.get("location") == "https://%s/f/?%s" % (HOST, Q), summary(h))
    h, _ = get("/noslash?gclid=1&page=2")
    R.check("bypass request: PHP's own Location untouched (no double params)",
            h.get("location") == "https://%s/noslash/?gclid=1&page=2" % HOST, summary(h))
    h, _ = get("/noslash?gclid=1", headers={"Cookie": "wordpress_logged_in_x=1"})
    R.check("logged-in request: PHP's own Location untouched (no double params)",
            h.get("location") == "https://%s/noslash/?gclid=1" % HOST, summary(h))

    # ------------------------------------------------------------------
    R.section("8. Purge (Nginx Helper style GET /purge/<path>) clears every variant")
    R.purge("/g/")
    h, _ = get("/g/?gclid=1")
    R.check("variant creates the entry (MISS)", h.get("x-grid-cache") == "MISS", summary(h))
    h, _ = get("/g/")
    R.check("clean URL hits it", h.get("x-grid-cache") == "HIT", summary(h))
    R.req("/purge/g/")
    h, _ = get("/g/?gclid=2")
    R.check("after /purge/g/ the variant is a MISS again", h.get("x-grid-cache") == "MISS", summary(h))

    # ------------------------------------------------------------------
    R.section("9. Site without the switch = stock GridPane")
    t = "/a/?gclid=1"
    h, _ = get(t, host=OFF_HOST)
    R.check("off.example %s -> BYPASS -query_string, PHP sees original" % t, is_stock_bypass(h, t), summary(h))
    st, h, _ = R.req("/noslash?gclid=1", host=OFF_HOST)
    R.check("off.example redirect untouched", h.get("location") == "https://%s/noslash/?gclid=1" % OFF_HOST,
            "loc=%r" % h.get("location"))


def run_config_scenarios(R, sbx):
    R.section("10. Config robustness (nginx -t of alternative layouts)")
    ok, out = True, sbx.t_output
    R.check("sandbox nginx -t has no warnings", "[warn]" not in out and "[emerg]" not in out, out[-300:])
    # GridPane clone to a server WITHOUT the engine: only the per-site switch exists
    absent = sbx.p("site-clone")
    os.makedirs(absent, exist_ok=True)
    write(os.path.join(absent, "marketing-params-php-context.conf"),
          read(os.path.join(REPO, "site", "marketing-params-php-context.conf")).replace(LIVE_DIR, sbx.p("not-installed")))
    conf = sbx.nginx_conf(engine=False, switches_dir=absent, off_server=False)
    conf = conf.replace("include %s/wpfc.conf;" % sbx.d, "include %s/wpfc-clone.conf;" % sbx.d)
    write(sbx.p("wpfc-clone.conf"), read(sbx.p("wpfc.conf")).replace(sbx.p("site") + "/", absent + "/"))
    ok, out = sbx.nginx_t(conf, "nginx-clone.conf")
    R.check("site switch present but engine NOT installed (remote clone) -> nginx -t OK", ok, out[-400:])
    ok, out = sbx.nginx_t(sbx.nginx_conf(wpfc=False), "nginx-nowpfc.conf")
    R.check("engine installed but no FastCGI-cache vhost at all -> nginx -t OK", ok, out[-400:])
    nolist = sbx.p("marketing-params", "params.list")
    os.rename(nolist, nolist + ".away")
    ok, out = sbx.nginx_t(read(sbx.p("nginx.conf")), "nginx-nolist.conf")
    os.rename(nolist + ".away", nolist)
    R.check("params.list missing -> nginx -t OK (nothing counts as marketing)", ok, out[-400:])
    # php location of a NON-cache vhost (site switched to "no cache" in GridPane) with the switch still there
    conf = sbx.nginx_conf(wpfc=False).replace(
        "location ~ \\.php$ { include /etc/nginx/fastcgi_params; fastcgi_pass php; }",
        "location ~ \\.php$ { include /etc/nginx/fastcgi_params; include %s/*-php-context.conf; fastcgi_pass php; }" % sbx.p("site"))
    ok, out = sbx.nginx_t(conf, "nginx-nocache-site.conf")
    R.check("switch left on a site without page cache -> nginx -t OK", ok, out[-400:])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", help="use /etc/nginx/common/<site>-wpfc.conf instead of the generic template")
    ap.add_argument("--keep", action="store_true", help="keep the sandbox directory")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(NGINX):
        print("nginx binary not found"); return 2
    sbx = Sandbox(site=a.site, keep=a.keep)
    try:
        sbx.build()
        sbx.start()
        print("sandbox: %s  (nginx on 127.0.0.1:%d, template %s)" % (sbx.d, sbx.port, sbx.template))
        R = Runner(sbx, a.verbose)
        run_matrix(R)
        run_config_scenarios(R, sbx)
        err = read(sbx.p("error.log")) if os.path.exists(sbx.p("error.log")) else ""
        bad = [l for l in err.splitlines() if re.search(r"\[(alert|emerg|crit)\]|uninitialized|cycle while|no buffer space", l)]
        R.section("11. Error log")
        R.check("no alerts / uninitialized variables / evaluation cycles", not bad, "\n       ".join(bad[:5]))
    except SetupError as e:
        print(c("31", "SETUP ERROR: ") + str(e))
        sbx.keep = True
        print("sandbox kept for inspection: " + sbx.d)
        return 2
    finally:
        sbx.stop()
    total = R.passed + len(R.failed)
    print()
    if R.failed:
        print(c("31", "%d of %d checks FAILED" % (len(R.failed), total)))
        for g, n, _ in R.failed:
            print("  - [%s] %s" % (g, n))
        if a.keep:
            print("sandbox kept: " + sbx.d)
        return 1
    print(c("32", "ALL %d CHECKS PASSED" % total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
