#!/usr/bin/env python3
"""
Sandbox test - safe to run on any GridPane server (even production):

  * builds a throw-away nginx instance in /tmp on a random 127.0.0.1 port,
  * using the server's own nginx binary + GridPane's real page-cache templates:
      FastCGI  /etc/nginx/common/wpfc.conf      (or a rendered <site>-wpfc.conf with --site)
      Redis    /etc/nginx/common/wp-redis.conf  (or a rendered <site>-wp-redis.conf with --site),
               with a throw-away redis-server on a unix socket inside the sandbox dir,
  * plus the files from THIS repo (engine, list, hooks, switches),
  * with a fake PHP backend (tests/fake_php.py) instead of PHP-FPM/WordPress,
  * runs the whole behaviour matrix and removes everything again.

The live nginx, its caches (FastCGI zone, redis-page) and the sites are never touched.

  ./test.sh                     generic GridPane templates, FastCGI and Redis
  ./test.sh --cache redis       only one cache type (fcgi | redis | redis-legacy | all)
  ./test.sh --site example.com  that site's rendered template (its own cache type)
  ./test.sh -v                  also print passing checks' details
  ./test.sh --keep              keep the sandbox dir for inspection
"""
import argparse
import glob
import http.client
import os
import re
import secrets
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
NOFLAG_HOST = "noflag.example"
REDIS_SERVER = shutil.which("redis-server") or "/usr/bin/redis-server"
# Redis sandbox: one database (and dbN: key prefix) per vhost, like GridPane
GEO_HOST = "geo.example"          # switch + flag, but a customised key formula
FSW_HOST = "fsw.example"          # FastCGI switch + FastCGI flag left on a Redis vhost
MIX_HOST = "mix.example"          # its own switch, but another site's flag (add-on-domain overlay)
OLD_HOST = "old.example"          # FastCGI switch files of an older version (flag "1", unnamed switch)
XSW_HOST = "xsw.example"          # flag of one cache type + switch of the other, same site (staging push)
REDIS_DB = {HOST: 2, OFF_HOST: 3, NOFLAG_HOST: 4, GEO_HOST: 5, FSW_HOST: 6, MIX_HOST: 7}
# Same relative order as /etc/nginx/modules-enabled (filter modules run in reverse load
# order, which decides e.g. whether srcache stores headers set by headers-more).
REDIS_MODULES = ("ndk_http_module", "ngx_http_echo_module", "ngx_http_headers_more_filter_module",
                 "ngx_http_redis2_module", "ngx_http_redis_module", "ngx_http_set_misc_module",
                 "ngx_http_srcache_filter_module")

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
    cache = "fcgi"

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
        for f in ("php-context.conf", "php-context-fcgi.conf", "php-context-redis.conf"):
            write(os.path.join(mp, f), self.relocate(read(os.path.join(REPO, "server/marketing-params", f))))
        # stray copies an admin might leave behind - must be ignored (exact-name includes)
        stray = subprocess.run([sys.executable, os.path.join(REPO, "build-engine.py"), "1", "64", mp],
                               capture_output=True, text=True, check=True).stdout
        write(os.path.join(mp, "engine.old.conf"), stray)
        write(os.path.join(mp, "php-context.orig.conf"), read(os.path.join(mp, "php-context.conf")))
        write(self.p("stub-http.conf"), self.relocate(read(os.path.join(REPO, "server/stubs/conf.d-marketing-params.conf"))))
        os.makedirs(self.p("extra.d"), exist_ok=True)
        sw = self.relocate(read(os.path.join(REPO, "site", "marketing-params-php-context.conf")))
        fl = read(os.path.join(REPO, "site", "marketing-params-skip-fcgi-cache-context.conf"))
        if "@SITE@" not in sw or "@SITE@" not in fl:
            raise SetupError("FastCGI switch / flag in site/ lost their @SITE@ placeholder")
        write(self.p("site", "marketing-params-php-context.conf"), sw.replace("@SITE@", HOST))   # what install.sh writes
        write(self.p("site", "marketing-params-skip-fcgi-cache-context.conf"), fl.replace("@SITE@", HOST))
        # same switch but WITHOUT the FastCGI flag (what a Redis/no-cache vhost would see)
        write(self.p("site-noflag", "marketing-params-php-context.conf"), sw.replace("@SITE@", NOFLAG_HOST))
        # a switch and a flag of two different sites in one vhost (add-on-domain overlay)
        write(self.p("site-mix", "marketing-params-php-context.conf"), sw.replace("@SITE@", MIX_HOST))
        write(self.p("site-mix", "marketing-params-skip-fcgi-cache-context.conf"), fl.replace("@SITE@", "parent.example"))
        # files of an older version: inert with this engine until ./install.sh rewrites them
        write(self.p("site-old", "marketing-params-php-context.conf"),
              "include %s/php-context[.]conf;\n" % mp)
        write(self.p("site-old", "marketing-params-skip-fcgi-cache-context.conf"), "set $gp_mkt_fcgi 1;\n")
        # the site's own FastCGI flag + its Redis switch (a staging push from a Redis site leaves both)
        write(self.p("site-xsw", "marketing-params-php-context.conf"),
              self.relocate(read(os.path.join(REPO, "site", "marketing-params-php-context.redis.conf"))).replace("@SITE@", XSW_HOST))
        write(self.p("site-xsw", "marketing-params-skip-fcgi-cache-context.conf"), fl.replace("@SITE@", XSW_HOST))
        # an nginx-generated redirect inside the php location (GridPane KB style php-context rule)
        write(self.p("site", "custom-redirect-php-context.conf"),
              'if ($request_uri ~ "^/ngx-redirect\\.php") {\n    return 301 /nginx-target/;\n}\n')
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
        self.wpfc(self.p("site-noflag"), "wpfc-noflag.conf")
        self.wpfc(self.p("site-mix"), "wpfc-mix.conf")
        self.wpfc(self.p("site-old"), "wpfc-old.conf")
        self.wpfc(self.p("site-xsw"), "wpfc-xsw.conf")
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
    }
    server {
        listen 127.0.0.1:%(port)d;
        server_name %(noflag)s;
        root %(d)s/htdocs;
        index index.php;
        set $sockfile php;
        include %(d)s/wpfc-noflag.conf;
    }
    server {
        listen 127.0.0.1:%(port)d;
        server_name %(mix)s;
        root %(d)s/htdocs;
        index index.php;
        set $sockfile php;
        include %(d)s/wpfc-mix.conf;
    }
    server {
        listen 127.0.0.1:%(port)d;
        server_name %(old)s;
        root %(d)s/htdocs;
        index index.php;
        set $sockfile php;
        include %(d)s/wpfc-old.conf;
    }
    server {
        listen 127.0.0.1:%(port)d;
        server_name %(xsw)s;
        root %(d)s/htdocs;
        index index.php;
        set $sockfile php;
        include %(d)s/wpfc-xsw.conf;
    }""" % dict(port=self.port, off=OFF_HOST, noflag=NOFLAG_HOST, mix=MIX_HOST, old=OLD_HOST, xsw=XSW_HOST, d=self.d))
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
        r = subprocess.run([NGINX, "-e", self.p("error.log"), "-t", "-p", self.d, "-c", path], capture_output=True, text=True)
        return r.returncode == 0, (r.stdout + r.stderr).strip()

    def start(self):
        fake_php.start(self.fport)
        ok, out = self.nginx_t(read(self.p("nginx.conf")), "nginx.conf")
        if not ok:
            raise SetupError("nginx -t failed for the sandbox:\n" + out)
        self.t_output = out
        r = subprocess.run([NGINX, "-e", self.p("error.log"), "-p", self.d, "-c", self.p("nginx.conf")],
                           capture_output=True, text=True)
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
            subprocess.run([NGINX, "-e", self.p("error.log"), "-p", self.d, "-c", self.p("nginx.conf"), "-s", "stop"],
                           capture_output=True)
            for _ in range(30):
                if not os.path.exists(self.p("nginx.pid")):
                    break
                time.sleep(0.1)
        if not self.keep:
            shutil.rmtree(self.d, ignore_errors=True)


# --------------------------------------------------------------------------- Redis sandbox

class Resp:
    """Minimal Redis client (RESP2, stdlib only) for the sandbox's own redis-server."""

    def __init__(self, path, password=None):
        self.s = socket.socket(socket.AF_UNIX)
        self.s.settimeout(5)
        self.s.connect(path)
        self.f = self.s.makefile("rb")
        if password:
            self.cmd("AUTH", password)

    def cmd(self, *args):
        out = b"*%d\r\n" % len(args)
        for a in args:
            a = a if isinstance(a, bytes) else str(a).encode("latin1")
            out += b"$%d\r\n%s\r\n" % (len(a), a)
        self.s.sendall(out)
        return self._read()

    def _read(self):
        line = self.f.readline()
        t, rest = line[:1], line[1:-2]
        if t == b"+":
            return rest.decode()
        if t == b"-":
            raise RuntimeError("redis: " + rest.decode())
        if t == b":":
            return int(rest)
        if t == b"$":
            n = int(rest)
            if n < 0:
                return None
            data = self.f.read(n + 2)[:-2]
            return data.decode("latin1")
        if t == b"*":
            return [self._read() for _ in range(int(rest))]
        raise RuntimeError("redis: unexpected reply %r" % line)

    def keys(self, db):
        self.cmd("SELECT", db)
        return sorted(self.cmd("KEYS", "*"))

    def close(self):
        self.s.close()


class RedisSandbox(Sandbox):
    """Same sandbox, but with GridPane's Redis page-cache template (srcache + redis2).

    The current template gives every site its own database and dbN: key prefix. The legacy one
    (servers without the redis-page instance) has one database and the prefix nginx-cache: for
    every site, no auth/select, and stores 3xx responses as well."""
    cache = "redis"

    def __init__(self, site=None, keep=False, template=None):
        super().__init__(site, keep)
        self.tpl_path = template or ("/etc/nginx/common/%s-wp-redis.conf" % site if site else "/etc/nginx/common/wp-redis.conf")
        self.legacy = os.path.exists(self.tpl_path) and "redis2_query select" not in read(self.tpl_path)
        self.password = None if self.legacy else secrets.token_hex(16)
        self.rsock = self.p("redis", "redis.sock")   # dir owned by the redis user (www-data as root)
        self.redis = None

    def prefix(self, host):
        return "nginx-cache:" if self.legacy else "db%d:" % REDIS_DB[host]

    def db(self, host):
        return 0 if self.legacy else REDIS_DB[host]

    # -- GridPane template ---------------------------------------------------
    def wpredis(self, site_dir, name, host, key_suffix=""):
        db = self.db(host)
        tpl = self.tpl_path
        if self.site:
            if not os.path.exists(tpl):
                raise SetupError("%s not found (does the site use Redis page caching?)" % tpl)
            s = read(tpl)
            if "/var/www/%s/nginx/" % self.site not in s:
                raise SetupError("template %s: expected '/var/www/%s/nginx/' not found" % (tpl, self.site))
            s = s.replace("/var/www/%s/nginx/" % self.site, site_dir + "/")
        else:
            if not os.path.exists(tpl):
                raise SetupError("%s not found - is this a GridPane server?" % tpl)
            s = read(tpl)
            # emulate GridPane's per-site rendering of the generic template
            renders = {
                "include /etc/nginx/extra.d/*-skip-cache-context.conf;":
                    "include %s/*-skip-redis-cache-context.conf;" % site_dir,
                "include /etc/nginx/extra.d/*-root-context.conf;":
                    "include %s/*-root-context.conf;" % site_dir,
                "include /etc/nginx/extra.d/*-php-context.conf;":
                    "include %s/*-php-context.conf;" % site_dir,
            }
            for a, b in renders.items():
                if a not in s:
                    raise SetupError("GridPane template changed: '%s' not found in %s" % (a, tpl))
                s = s.replace(a, b)
        for must in ['if ($query_string != "")', "$skip_reason", "set_escape_uri $escaped_key $key;",
                     "srcache_fetch GET /redis-fetch $key;", "srcache_store PUT /redis-store key=$escaped_key;",
                     "include %s/*-skip-redis-cache-context.conf;" % site_dir,
                     "include %s/*-php-context.conf;" % site_dir]:
            if must not in s:
                raise SetupError("GridPane template changed: '%s' not found in %s" % (must, tpl))
        if s.index("set_escape_uri $escaped_key $key;") > s.index("include %s/*-php-context.conf;" % site_dir):
            raise SetupError("GridPane template changed: $escaped_key is computed after the php-context include")
        # this vhost's database and key prefix (GridPane: one per site)
        key = re.compile(r'^(\s*)set \$key "[A-Za-z0-9_-]+:(\$scheme\$request_method\$host\$request_uri)";', re.M)
        if len(key.findall(s)) != 1:
            raise SetupError("%s: expected exactly one active 'set $key \"<prefix>:$scheme$request_method"
                             "$host$request_uri\";' line" % tpl)
        s = key.sub(lambda m: '%sset $key "%s%s%s";' % (m.group(1), self.prefix(host), m.group(2), key_suffix), s)
        s, n1 = re.subn(r"set \$redis_db \d+;", "set $redis_db %d;" % db, s)
        s, n2 = re.subn(r"redis2_query select \d+;", "redis2_query select %d;" % db, s)
        s, n3 = re.subn(r"set \$redis_auth [^;]*;", "set $redis_auth %s;" % self.password, s)
        s, n4 = re.subn(r"redis2_query auth [^;]*;", "redis2_query auth %s;" % self.password, s)
        if (n1, n2, n3, n4) != ((0, 0, 0, 0) if self.legacy else (1, 1, 1, 1)):
            raise SetupError("GridPane template changed: redis db/auth lines not found in %s (%s)"
                             % (tpl, (n1, n2, n3, n4)))
        s = s.replace("/etc/nginx/extra.d/", self.p("extra.d") + "/")
        self.template = tpl
        write(self.p(name), s)

    # -- build ---------------------------------------------------------------
    def build(self, variant="full"):
        mp = self.p("marketing-params")
        os.makedirs(mp, exist_ok=True)
        eng = subprocess.run([sys.executable, os.path.join(REPO, "build-engine.py"), str(MAX_PARAMS), str(MAX_LEN), mp],
                             capture_output=True, text=True, check=True).stdout
        write(os.path.join(mp, "engine.conf"), eng)
        shutil.copy(os.path.join(REPO, "server/marketing-params/params.list"), os.path.join(mp, "params.list"))
        for f in ("php-context.conf", "php-context-fcgi.conf", "php-context-redis.conf"):
            write(os.path.join(mp, f), self.relocate(read(os.path.join(REPO, "server/marketing-params", f))))
        stray = subprocess.run([sys.executable, os.path.join(REPO, "build-engine.py"), "1", "64", mp],
                               capture_output=True, text=True, check=True).stdout
        write(os.path.join(mp, "engine.old.conf"), stray)
        write(os.path.join(mp, "php-context-redis.orig.conf"), read(os.path.join(mp, "php-context-redis.conf")))
        write(self.p("stub-http.conf"), self.relocate(read(os.path.join(REPO, "server/stubs/conf.d-marketing-params.conf"))))
        os.makedirs(self.p("extra.d"), exist_ok=True)
        sw = self.relocate(read(os.path.join(REPO, "site", "marketing-params-php-context.redis.conf")))
        fl = read(os.path.join(REPO, "site", "marketing-params-skip-redis-cache-context.conf"))
        if "@SITE@" not in sw or "@SITE@" not in fl:
            raise SetupError("Redis switch / flag in site/ lost their @SITE@ placeholder")
        redis_switch = lambda site: sw.replace("@SITE@", site)   # noqa: E731 (what install.sh writes)
        redis_flag = lambda site: fl.replace("@SITE@", site)     # noqa: E731
        for d, site in (("site", HOST), ("site-geo", GEO_HOST)):
            write(self.p(d, "marketing-params-php-context.conf"), redis_switch(site))
            write(self.p(d, "marketing-params-skip-redis-cache-context.conf"), redis_flag(site))
        # Redis switch WITHOUT the Redis flag: GridPane's key must stay untouched
        write(self.p("site-noflag", "marketing-params-php-context.conf"), redis_switch(NOFLAG_HOST))
        # a switch and a flag of two different sites in one vhost (GridPane's add-on-domain
        # overlay includes the parent's flag and the child's php-context files)
        write(self.p("site-mix", "marketing-params-php-context.conf"), redis_switch(MIX_HOST))
        write(self.p("site-mix", "marketing-params-skip-redis-cache-context.conf"), redis_flag("parent.example"))
        # a FastCGI-enabled site whose cache was switched to Redis in GridPane
        for f in ("marketing-params-php-context.conf", "marketing-params-skip-fcgi-cache-context.conf"):
            write(self.p("site-fsw", f), self.relocate(read(os.path.join(REPO, "site", f))).replace("@SITE@", FSW_HOST))
        write(self.p("site-fsw", "marketing-params-skip-redis-cache-context.conf"), redis_flag(FSW_HOST))   # + own Redis flag
        write(self.p("site", "custom-redirect-php-context.conf"),
              'if ($request_uri ~ "^/ngx-redirect\\.php") {\n    return 301 /nginx-target/;\n}\n')
        write(self.p("site", "custom-root-context.conf"),
              "rewrite ^/rw/$ /index.php?lang=en last;\n"
              "rewrite ^/old-missing/$ /missing.php?x=1 last;\n")
        write(self.p("site", "custom-skip-redis-cache-context.conf"),
              'if ($arg_utm_source = "nocache") {\n'
              '    set $skip_cache 1;\n'
              '    set $skip_reason "${skip_reason}-custom";\n'
              '}\n')
        os.makedirs(self.p("site-off"), exist_ok=True)
        write(self.p("htdocs/index.php"), "")
        for t in ("body", "fcgi", "proxy", "uwsgi", "scgi"):
            os.makedirs(self.p("tmp", t), exist_ok=True)
        os.makedirs(self.p("cache"), exist_ok=True)
        os.makedirs(self.p("redis"), exist_ok=True)
        self.wpredis(self.p("site"), "wp-redis.conf", HOST)
        self.wpredis(self.p("site-off"), "wp-redis-off.conf", OFF_HOST)
        self.wpredis(self.p("site-noflag"), "wp-redis-noflag.conf", NOFLAG_HOST)
        self.wpredis(self.p("site-geo"), "wp-redis-geo.conf", GEO_HOST, key_suffix=":v2")
        self.wpredis(self.p("site-fsw"), "wp-redis-fsw.conf", FSW_HOST)
        self.wpredis(self.p("site-mix"), "wp-redis-mix.conf", MIX_HOST)
        write(self.p("nginx.conf"), self.nginx_conf())
        if os.geteuid() == 0:
            subprocess.run(["chown", "-R", "www-data:www-data", self.p("cache"), self.p("tmp"), self.p("redis")],
                           check=False)

    def nginx_conf(self, engine=True, switches_dir=None, wpfc=True, off_server=True):
        mods = []
        for m in REDIS_MODULES:
            so = "/etc/nginx/modules/%s.so" % m
            if not os.path.exists(so):
                raise SetupError("nginx module missing: %s" % so)
            mods.append("load_module %s;" % so)
        fc = read("/etc/nginx/conf.d/fastcgi.conf")
        fc = re.sub(r"^\s*fastcgi_cache_path[^\n]*\n", "", fc, flags=re.M)
        user = "user www-data;" if os.geteuid() == 0 else ""
        servers = []
        vhosts = [(HOST + " www." + HOST, switches_dir or self.p("site"), "wp-redis.conf")]
        if off_server:
            vhosts += [(OFF_HOST, self.p("site-off"), "wp-redis-off.conf"),
                       (NOFLAG_HOST, self.p("site-noflag"), "wp-redis-noflag.conf"),
                       (GEO_HOST, self.p("site-geo"), "wp-redis-geo.conf"),
                       (FSW_HOST, self.p("site-fsw"), "wp-redis-fsw.conf"),
                       (MIX_HOST, self.p("site-mix"), "wp-redis-mix.conf")]
        if wpfc:
            for names, site, conf in vhosts:
                servers.append("""
    server {
        listen 127.0.0.1:%(port)d;
        server_name %(names)s;
        root %(d)s/htdocs;
        index index.php;
        set $sockfile php;   # GridPane sets this in <site>-sockfile.conf
        include %(site)s/*-main-context.conf;
        include %(d)s/%(conf)s;
    }""" % dict(port=self.port, names=names, d=self.d, site=site, conf=conf))
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
    upstream redis-page { server unix:%(sock)s; keepalive 10; }
    upstream redis-fetch-page { server unix:%(sock)s; keepalive 10; }
    upstream redis { server unix:%(sock)s; keepalive 10; }      # legacy template
%(servers)s
}
""" % dict(mods="\n".join(mods), user=user, d=self.d, fc=fc, fport=self.fport, sock=self.rsock,
           engine=("include %s;" % self.p("stub-http.conf")) if engine else "",
           servers="\n".join(servers))

    # -- redis-server --------------------------------------------------------
    def start(self):
        if not os.path.exists(REDIS_SERVER):
            raise SetupError("redis-server not found (%s)" % REDIS_SERVER)
        args = [REDIS_SERVER, "--port", "0", "--unixsocket", self.rsock, "--unixsocketperm", "700",
                "--dir", self.p("redis"), "--save", "", "--appendonly", "no", "--databases", "16",
                "--daemonize", "no", "--logfile", self.p("redis", "redis.log")]
        if self.password:
            args += ["--requirepass", self.password]
        kw = {}
        if os.geteuid() == 0:
            kw["user"] = "www-data"     # nginx workers run as www-data and must reach the socket
        self.redis_proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        for _ in range(100):
            if os.path.exists(self.rsock):
                try:
                    self.redis = Resp(self.rsock, self.password)
                    break
                except OSError:
                    pass
            time.sleep(0.05)
        else:
            raise SetupError("sandbox redis-server did not start (see %s)" % self.p("redis", "redis.log"))
        super().start()

    def stop(self):
        if self.redis:
            self.redis.close()
        if getattr(self, "redis_proc", None):
            self.redis_proc.terminate()
            try:
                self.redis_proc.wait(5)
            except subprocess.TimeoutExpired:
                self.redis_proc.kill()
        super().stop()


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
        if self.sbx.cache == "redis":
            self.redis_headers(h, host, method)
        return r.status, h, raw.decode("latin1")

    # -- Redis: GridPane's srcache headers under the FastCGI names, so one matrix fits both
    def redis_headers(self, h, host, method):
        h["x-grid-cache"] = h.get("x-grid-srcache-fetch")
        if h.get("x-grid-srcache-skip"):
            h["x-grid-cache-skip"] = h["x-grid-srcache-skip"]
        if h.get("x-grid-srcache-store") == "STORE" and "x-test-uri" in h:
            # srcache stores in a subrequest after the response: wait until the entry
            # (keyed by what PHP saw) exists, so the next request can hit it
            k = self.redis_key(h["x-test-uri"], host.split(":")[0].lower(), method)
            h[":key"] = k
            for _ in range(100):
                if self.rcmd(host, "EXISTS", k):
                    break
                time.sleep(0.01)

    def redis_key(self, uri, host=HOST, method="GET"):
        apex = host[4:] if host.startswith("www.") else host
        return "%shttp%s%s%s" % (self.sbx.prefix(apex), method, host, uri)

    def rcmd(self, host, *args):
        apex = host[4:] if host.startswith("www.") else host
        self.sbx.redis.cmd("SELECT", self.sbx.db(apex))
        return self.sbx.redis.cmd(*args)

    def purge(self, path, host=HOST):
        if self.sbx.cache == "redis":
            for m in ("GET", "HEAD"):
                self.rcmd(host, "DEL", self.redis_key(path, host, m))
            return
        self.req("/purge" + path, host)
        self.req("/purge" + path, host, method="HEAD")

    def purge_url(self, path, host=HOST):
        """One URL, the way Nginx Helper purges it (FastCGI: GET /purge/<path>; Redis: DEL of
        <prefix><scheme>GET<host><path>)."""
        if self.sbx.cache == "redis":
            self.rcmd(host, "DEL", self.redis_key(path, host, "GET"))
        else:
            self.req("/purge" + path, host)


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
    h, _ = get("http://%s?gclid=1" % HOST)
    R.check("absolute-form request line with an empty path (GET http://host?gclid=1) -> stock BYPASS",
            h.get("x-grid-cache") == "BYPASS" and "x-grid-cache-mkt" not in h, summary(h))
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
    for q in ("", "?gclid=1", "?page=2"):
        h, _ = get("/ngx-redirect.php" + q)
        R.check("nginx's own `return 301` in the php location keeps its Location (%s)" % (q or "no query"),
                h[":status"] == 301 and (h.get("location") or "").startswith("/nginx-target/"), summary(h))
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
    R.section("8. Purge (Nginx Helper style, %s) clears every variant"
              % ("DEL of the clean URL's key" if R.sbx.cache == "redis" else "GET /purge/<path>"))
    R.purge("/g/")
    h, _ = get("/g/?gclid=1")
    R.check("variant creates the entry (MISS)", h.get("x-grid-cache") == "MISS", summary(h))
    h, _ = get("/g/")
    R.check("clean URL hits it", h.get("x-grid-cache") == "HIT", summary(h))
    R.purge_url("/g/")
    h, _ = get("/g/?gclid=2")
    R.check("after purging /g/ the variant is a MISS again", h.get("x-grid-cache") == "MISS", summary(h))

    # ------------------------------------------------------------------
    R.section("9. Site without the switch = stock GridPane")
    t = "/a/?gclid=1"
    h, _ = get(t, host=OFF_HOST)
    R.check("off.example %s -> BYPASS -query_string, PHP sees original" % t, is_stock_bypass(h, t), summary(h))
    h, _ = get(t, host=NOFLAG_HOST)
    R.check("php switch WITHOUT the %s flag -> stock, inert" % ("Redis" if R.sbx.cache == "redis" else "FastCGI"),
            is_stock_bypass(h, t), summary(h))
    st, h, _ = R.req("/noslash?gclid=1", host=OFF_HOST)
    R.check("off.example redirect untouched", h.get("location") == "https://%s/noslash/?gclid=1" % OFF_HOST,
            "loc=%r" % h.get("location"))

    # ------------------------------------------------------------------
    R.section("10. REST API is never cached (GridPane's own skip rule leaves /wp-json/ cacheable)")

    def is_rest_bypass(h, target):
        return (h.get("x-grid-cache") == "BYPASS" and "-rest_api" in (h.get("x-grid-cache-skip") or "")
                and "x-grid-cache-mkt" not in h and h.get("x-test-uri") == target)

    for t in ["/wp-json/", "/wp-json", "/wp-json/wc/store/v1/cart", "/WP-JSON/wp/v2/posts", "/wp%2Djson/wp/v2/posts",
              "/wp-json%2Fwp/v2/posts", "//wp-json/wp/v2/posts"]:
        get(t)
        h, _ = get(t)
        R.check("%s -> BYPASS -rest_api twice (never stored)" % t, is_rest_bypass(h, t), summary(h))
    for t in ["/wp-json/wc/store/v1/cart?gclid=1", "/wp-json/?utm_source=x&fbclid=1"]:
        h, _ = get(t)
        R.check("%s -> marketing params NOT ignored on the REST API, PHP sees original" % t,
                is_rest_bypass(h, t) and "-query_string" in (h.get("x-grid-cache-skip") or ""), summary(h))
    R.purge("/wp-jsonx/")
    get("/wp-jsonx/")
    h, _ = get("/wp-jsonx/")
    R.check("/wp-jsonx/ (not the REST API) is cached normally", h.get("x-grid-cache") == "HIT", summary(h))
    R.purge("/a/wp-json/")
    get("/a/wp-json/")
    h, _ = get("/a/wp-json/")
    R.check("/a/wp-json/ (not at the root) is cached normally", h.get("x-grid-cache") == "HIT", summary(h))
    h, _ = get("/wp-json/", host=OFF_HOST)
    R.check("site without the switch: /wp-json/ stock (no -rest_api)",
            "-rest_api" not in (h.get("x-grid-cache-skip") or ""), summary(h))


MKT_IN_KEY = re.compile(r"[?&](?:gclid|utm_|fbclid|msclkid|gad_|gbraid|wbraid|_gl=|hsa_|mtm_|pk_|ttclid)", re.I)


def run_redis_checks(R):
    def get(target, **kw):
        st, h, body = R.req(target, **kw)
        h[":status"] = st
        return h, body

    def stock_bypass(h, target):
        return (h.get("x-grid-cache") == "BYPASS" and "-query_string" in (h.get("x-grid-cache-skip") or "")
                and "x-grid-cache-mkt" not in h and h.get("x-test-uri") == target)

    R.section("10. Redis: keys, prefixes and isolation")
    tpl_ttl = re.search(r"set \$cache_ttl (\d+);", read(R.sbx.p("wp-redis.conf")))
    k = R.redis_key("/k/")
    R.purge("/k/")
    h, _ = get("/k/?gclid=K1&utm_source=x")
    R.check("variant is stored under the clean URL's stock key %s" % k,
            h.get("x-grid-srcache-store") == "STORE" and R.rcmd(HOST, "EXISTS", k) == 1, summary(h))
    ttl = R.rcmd(HOST, "TTL", k)
    R.check("...with GridPane's cache_ttl (%s s)" % (tpl_ttl and tpl_ttl.group(1)),
            tpl_ttl is not None and 0 < ttl <= int(tpl_ttl.group(1)), "ttl=%s" % ttl)
    h, _ = get("/k/")
    R.check("clean URL is a HIT of that entry", h.get("x-grid-cache") == "HIT", summary(h))
    for p in ("/e%C5%BC/", "/sp%20ace/", "/a+b/", "/x%3Fy/"):
        R.purge(p)
        h1, _ = get(p + "?gclid=1")
        h2, _ = get(p)
        R.check("encoded path %s: variant MISS stores, clean URL HITs ($escaped_key recomputed)" % p,
                h1.get("x-grid-cache") == "MISS" and h2.get("x-grid-cache") == "HIT"
                and R.rcmd(HOST, "EXISTS", R.redis_key(p)) == 1, summary(h1) + " | " + summary(h2))
    R.purge("/hd/")
    st, h, _ = R.req("/hd/?gclid=1", method="HEAD")
    R.check("HEAD ?gclid=1 -> ignored like GET (key uses HEAD, as stock)",
            h.get("x-grid-cache-mkt") == "ignored" and h.get("x-grid-cache") != "BYPASS", summary(h))

    t = "/x/?gclid=1"
    h, _ = get(t, host=GEO_HOST)
    R.check("customised key formula (%s...$request_uri:v2) + switch + flag -> stock BYPASS, inert"
            % R.sbx.prefix(GEO_HOST), stock_bypass(h, t), summary(h))
    get("/x/", host=GEO_HOST)
    R.check("...and its own key is left untouched (%s)" % (R.redis_key("/x/", GEO_HOST) + ":v2"),
            R.rcmd(GEO_HOST, "EXISTS", R.redis_key("/x/", GEO_HOST) + ":v2") == 1,
            "keys=%s" % R.sbx.redis.keys(R.sbx.db(GEO_HOST)))
    h, _ = get(t, host=FSW_HOST)
    R.check("FastCGI switch + flag left on a Redis vhost, next to the site's own Redis flag -> stock, inert",
            stock_bypass(h, t), summary(h))
    h, _ = get(t, host=MIX_HOST)
    R.check("switch of one site + flag of another (add-on-domain overlay) -> stock, inert",
            stock_bypass(h, t), summary(h))
    for host in (NOFLAG_HOST, OFF_HOST, FSW_HOST, MIX_HOST):
        R.purge("/n/", host)
        h1, _ = get("/n/", host=host)
        h2, _ = get("/n/", host=host)
        R.check("%s: clean URL cached under GridPane's own key %s" % (host, R.redis_key("/n/", host)),
                h1.get("x-grid-cache") == "MISS" and h2.get("x-grid-cache") == "HIT"
                and R.rcmd(host, "EXISTS", R.redis_key("/n/", host)) == 1, summary(h1) + " | " + summary(h2))

    bad, foreign, total = [], [], 0
    for db in sorted({R.sbx.db(h) for h in REDIS_DB}):
        want = R.sbx.prefix(HOST) if R.sbx.legacy else "db%d:" % db
        for key in R.sbx.redis.keys(db):
            total += 1
            if MKT_IN_KEY.search(key):
                bad.append(key)
            if not key.startswith(want):
                foreign.append(key)
    R.check("no Redis key (of %d) contains a marketing param" % total, not bad, " ".join(bad[:5]))
    R.check("every key carries %s (no cross-site writes)"
            % ("the legacy prefix nginx-cache:" if R.sbx.legacy else "its own database's dbN: prefix"),
            not foreign, " ".join(foreign[:5]))


def run_fcgi_checks(R):
    R.section("10. FastCGI: flag and switch must belong to one site")
    t = "/x/?gclid=1"
    for host, label in ((MIX_HOST, "switch of one site + flag of another (add-on-domain overlay)"),
                        (OLD_HOST, "switch files of an older version (until ./install.sh rewrites them)"),
                        (XSW_HOST, "the site's FastCGI flag + its Redis switch (staging push)")):
        st, h, _ = R.req(t, host=host)
        h[":status"] = st
        R.check("%s -> stock BYPASS, PHP sees the original" % label,
                h.get("x-grid-cache") == "BYPASS" and "-query_string" in (h.get("x-grid-cache-skip") or "")
                and "x-grid-cache-mkt" not in h and h.get("x-test-uri") == t, summary(h))


def run_config_scenarios_redis(R, sbx):
    R.section("11. Config robustness (nginx -t of alternative layouts)")
    out = sbx.t_output
    R.check("sandbox nginx -t has no warnings", "[warn]" not in out and "[emerg]" not in out, out[-300:])
    absent = sbx.p("site-clone")
    os.makedirs(absent, exist_ok=True)
    write(os.path.join(absent, "marketing-params-php-context.conf"),
          read(os.path.join(REPO, "site", "marketing-params-php-context.redis.conf")).replace(LIVE_DIR, sbx.p("not-installed")))
    write(os.path.join(absent, "marketing-params-skip-redis-cache-context.conf"),
          read(os.path.join(REPO, "site", "marketing-params-skip-redis-cache-context.conf")))
    conf = sbx.nginx_conf(engine=False, switches_dir=absent, off_server=False)
    conf = conf.replace("include %s/wp-redis.conf;" % sbx.d, "include %s/wp-redis-clone.conf;" % sbx.d)
    write(sbx.p("wp-redis-clone.conf"), read(sbx.p("wp-redis.conf")).replace(sbx.p("site") + "/", absent + "/"))
    ok, out = sbx.nginx_t(conf, "nginx-clone.conf")
    R.check("Redis switch + flag present but engine NOT installed (remote clone) -> nginx -t OK", ok, out[-400:])
    ok, out = sbx.nginx_t(sbx.nginx_conf(wpfc=False), "nginx-noredis.conf")
    R.check("engine installed but no Redis-cache vhost at all -> nginx -t OK", ok, out[-400:])
    nolist = sbx.p("marketing-params", "params.list")
    os.rename(nolist, nolist + ".away")
    ok, out = sbx.nginx_t(read(sbx.p("nginx.conf")), "nginx-nolist.conf")
    os.rename(nolist + ".away", nolist)
    R.check("params.list missing -> nginx -t OK (nothing counts as marketing)", ok, out[-400:])
    conf = sbx.nginx_conf(wpfc=False).replace(
        "location ~ \\.php$ { include /etc/nginx/fastcgi_params; fastcgi_pass php; }",
        "location ~ \\.php$ { include /etc/nginx/fastcgi_params; include %s/*-php-context.conf; fastcgi_pass php; }" % sbx.p("site"))
    ok, out = sbx.nginx_t(conf, "nginx-nocache-site.conf")
    R.check("Redis switch left on a site without page cache -> nginx -t OK", ok, out[-400:])


def run_config_scenarios(R, sbx):
    R.section("11. Config robustness (nginx -t of alternative layouts)")
    ok, out = True, sbx.t_output
    R.check("sandbox nginx -t has no warnings", "[warn]" not in out and "[emerg]" not in out, out[-300:])
    # GridPane clone to a server WITHOUT the engine: only the per-site switch exists
    absent = sbx.p("site-clone")
    os.makedirs(absent, exist_ok=True)
    for f in ("marketing-params-php-context.conf", "marketing-params-skip-fcgi-cache-context.conf"):
        write(os.path.join(absent, f), read(os.path.join(REPO, "site", f)).replace(LIVE_DIR, sbx.p("not-installed")))
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


def redis_prereqs():
    """What is missing for the Redis sandbox on this server ("" = nothing)."""
    miss = [f for f in ([REDIS_SERVER, "/etc/nginx/common/wp-redis.conf"] +
                        ["/etc/nginx/modules/%s.so" % m for m in REDIS_MODULES]) if not os.path.exists(f)]
    return ", ".join(miss)


LEGACY_TPL = "/etc/nginx/common/wp-redis-legacy.conf"   # GridPane keeps the legacy template here


def run_one(cache, site, keep, verbose):
    """Returns (passed, failed list, setup_error)."""
    if cache == "fcgi":
        sbx = Sandbox(site=site, keep=keep)
    else:
        sbx = RedisSandbox(site=site, keep=keep, template=LEGACY_TPL if cache == "redis-legacy" else None)
    R = Runner(sbx, verbose)
    print(c("1;36", "\n=== %s page cache ===" % ("FastCGI" if cache == "fcgi" else
                                                 "Redis, legacy template" if sbx.legacy else "Redis")))
    try:
        sbx.build()
        sbx.start()
        print("sandbox: %s  (nginx on 127.0.0.1:%d, template %s)" % (sbx.d, sbx.port, sbx.template))
        run_matrix(R)
        if cache != "fcgi":
            run_redis_checks(R)
            run_config_scenarios_redis(R, sbx)
        else:
            run_fcgi_checks(R)
            run_config_scenarios(R, sbx)
        err = read(sbx.p("error.log")) if os.path.exists(sbx.p("error.log")) else ""
        lines = err.splitlines()
        # documented: a Redis switch on a non-Redis vhost reads GridPane's $key, which does not exist there
        # (GridPane's default error_log level does not write this warning)
        expected = [l for l in lines if 'using uninitialized "key" variable' in l and "server: %s," % XSW_HOST in l]
        bad = [l for l in lines if l not in expected
               and re.search(r"\[(alert|emerg|crit)\]|uninitialized|cycle while|no buffer space", l)]
        R.section("12. Error log")
        R.check("no alerts / uninitialized variables / evaluation cycles%s"
                % (" (except the documented warning of a Redis switch on a FastCGI vhost)" if expected else ""),
                not bad, "\n       ".join(bad[:5]))
    except SetupError as e:
        print(c("31", "SETUP ERROR: ") + str(e))
        sbx.keep = True
        print("sandbox kept for inspection: " + sbx.d)
        return R.passed, R.failed, True
    finally:
        sbx.stop()
    if R.failed and keep:
        print("sandbox kept: " + sbx.d)
    return R.passed, [(cache,) + f for f in R.failed], False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", help="use that site's rendered template (/etc/nginx/common/<site>-wpfc.conf "
                                   "or <site>-wp-redis.conf) instead of the generic one")
    ap.add_argument("--cache", choices=("all", "fcgi", "redis", "redis-legacy"), default="all",
                    help="which page-cache type to test (default: all; with --site: the site's own type; "
                         "redis-legacy = GridPane's legacy Redis template, wp-redis-legacy.conf)")
    ap.add_argument("--keep", action="store_true", help="keep the sandbox directory")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(NGINX):
        print("nginx binary not found"); return 2
    caches = ["fcgi", "redis"] if a.cache == "all" else [a.cache]
    if a.cache == "all" and os.path.exists(LEGACY_TPL) and "redis2_query select" in read("/etc/nginx/common/wp-redis.conf"):
        caches.append("redis-legacy")                          # the legacy template is kept next to the current one
    if a.site:
        if os.path.exists("/etc/nginx/common/%s-wpfc.conf" % a.site):
            caches = ["fcgi"]
        elif os.path.exists("/etc/nginx/common/%s-wp-redis.conf" % a.site):
            caches = ["redis"]
        else:
            print(c("31", "SETUP ERROR: ") + "%s uses neither GridPane FastCGI nor Redis page caching "
                  "(no /etc/nginx/common/%s-wpfc.conf or -wp-redis.conf)" % (a.site, a.site))
            return 2
    elif a.cache == "all" and redis_prereqs():
        print(c("33", "Redis sandbox skipped: missing %s" % redis_prereqs()))
        caches = ["fcgi"]
    passed, failed, setup_error = 0, [], False
    for cache in caches:
        p, f, err = run_one(cache, a.site, a.keep, a.verbose)
        passed, failed, setup_error = passed + p, failed + f, setup_error or err
    total = passed + len(failed)
    print()
    if setup_error:
        print(c("31", "SETUP ERROR (see above); %d checks ran, %d failed" % (total, len(failed))))
        return 2
    if failed:
        print(c("31", "%d of %d checks FAILED" % (len(failed), total)))
        for cache, g, n, _ in failed:
            print("  - [%s] [%s] %s" % (cache, g, n))
        return 1
    print(c("32", "ALL %d CHECKS PASSED (%s)" % (total, " + ".join(caches))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
