#!/usr/bin/env python3
"""
Installer test - safe to run on any GridPane server (even production):

  * copies this repo into a throw-away root under /tmp, with install.sh's paths
    (/etc/nginx, /var/www, /root) moved into it, and renders GridPane's real templates
    from this server (wpfc.conf, wp-redis.conf, php.conf) for a few fake sites,
  * runs install.sh in every mode: install, re-install, --status, --disable, --uninstall,
    --apply-list, cache-type changes in both directions, a clone/staging copy, non-stock
    templates, an nginx -t failure, and an engine from before the Redis support,
  * `nginx` is the server's real binary on the fake config (so `nginx -t` really checks what
    install.sh wrote) and `systemctl` only records the call,
  * checks files, output, exit codes and nginx -t after every step, then removes everything.

The live nginx, its config and the sites are never touched.

  ./test.sh install
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from sandbox_test import REDIS_MODULES, SetupError, c, read, write  # noqa: E402

NGINX = shutil.which("nginx") or "/usr/sbin/nginx"
SWITCH = "marketing-params-php-context.conf"
FLAG = "marketing-params-skip-fcgi-cache-context.conf"
FLAG_REDIS = "marketing-params-skip-redis-cache-context.conf"
ENGINE_FILES = ("engine.conf", "php-context.conf", "php-context-fcgi.conf", "php-context-redis.conf", "README.md", "limits.env")


class Skip(Exception):
    pass


class Root:
    def __init__(self):
        self.d = tempfile.mkdtemp(prefix="gp-mkt-install-test.")
        os.chmod(self.d, 0o755)
        self.etc = os.path.join(self.d, "etc/nginx")
        self.www = os.path.join(self.d, "var/www")
        self.home = os.path.join(self.d, "root")
        self.repo = os.path.join(self.d, "repo")
        self.mp = os.path.join(self.etc, "marketing-params")
        self.syslog = os.path.join(self.d, "systemctl.log")
        self.passed, self.failed = 0, []

    def relocate(self, s):
        return s.replace("/etc/nginx", self.etc).replace("/var/www", self.www)

    # -- fake server -----------------------------------------------------------
    def build(self):
        for t in ("/etc/nginx/common/wpfc.conf", "/etc/nginx/common/fcgi-cache-var.conf", "/etc/nginx/common/php.conf",
                  "/etc/nginx/common/basics.conf", "/etc/nginx/conf.d/fastcgi.conf", "/etc/nginx/fastcgi_params"):
            if not os.path.exists(t):
                raise SetupError("%s not found - is this a GridPane server?" % t)
        miss = [f for f in ["/etc/nginx/common/wp-redis.conf"] + ["/etc/nginx/modules/%s.so" % m for m in REDIS_MODULES]
                if not os.path.exists(f)]
        if miss:
            raise Skip("missing " + ", ".join(miss))
        shutil.copytree(REPO, self.repo, ignore=shutil.ignore_patterns(".git", ".claude", "__pycache__"))
        for dp, _, fs in os.walk(self.repo):
            for f in fs:
                p = os.path.join(dp, f)
                if p.endswith((".conf", ".list", ".md", ".sh")) or f == "build-engine.py":
                    s = self.relocate(read(p))
                    if f == "install.sh":
                        s = s.replace("/root/", self.home + "/")
                    write(p, s, os.stat(p).st_mode & 0o777)
        for d in ("conf.d", "common", "extra.d", "sites-available", "sites-enabled"):
            os.makedirs(os.path.join(self.etc, d), exist_ok=True)
        for d in (self.home, os.path.join(self.d, "cache"), os.path.join(self.d, "tmp")):
            os.makedirs(d, exist_ok=True)
        shutil.copy("/etc/nginx/fastcgi_params", self.etc)
        shutil.copy("/etc/nginx/common/basics.conf", os.path.join(self.etc, "common"))
        # GridPane's HTTP auth / IP allowlist, reduced to its access rules
        write(os.path.join(self.etc, "common/acl.conf"), "satisfy any;\nallow 127.0.0.1;\ndeny all;\n")
        fc = read("/etc/nginx/conf.d/fastcgi.conf")
        fc = re.sub(r"fastcgi_cache_path\s+\S+", "fastcgi_cache_path %s/cache" % self.d, fc)
        write(os.path.join(self.etc, "conf.d/fastcgi.conf"), self.relocate(fc))
        write(os.path.join(self.etc, "conf.d/upstream.conf"),
              "upstream php { server 127.0.0.1:9; }\n"
              "upstream redis-page { server 127.0.0.1:6; }\n"
              "upstream redis-fetch-page { server 127.0.0.1:6; }\n"
              "upstream redis { server 127.0.0.1:6; }\n")                  # legacy Redis template
        write(os.path.join(self.etc, "nginx.conf"), "%s\npid %s/nginx.pid;\nerror_log %s/error.log warn;\n"
              "events { worker_connections 64; }\nhttp {\n    access_log off;\n"
              "    client_body_temp_path %s/tmp; fastcgi_temp_path %s/tmp; proxy_temp_path %s/tmp;\n"
              "    uwsgi_temp_path %s/tmp; scgi_temp_path %s/tmp;\n"
              "    server_names_hash_bucket_size 256;\n    variables_hash_max_size 2048;\n"
              "    include %s/conf.d/*.conf;\n    include %s/sites-enabled/*;\n}\n"
              % ("\n".join("load_module /etc/nginx/modules/%s.so;" % m for m in REDIS_MODULES),
                 self.d, self.d, self.d, self.d, self.d, self.d, self.d, self.etc, self.etc))

    def vhost(self, s, kind, db=2, mutate=None, tmutate=None, acl=False):
        """(Re)render site s the way GridPane does: kind = fcgi | redis | redis-legacy | php.
        mutate: changes the Redis template / the FastCGI key file; tmutate: the FastCGI template."""
        os.makedirs("%s/%s/nginx" % (self.www, s), exist_ok=True)
        os.makedirs("%s/%s/htdocs" % (self.www, s), exist_ok=True)
        site = "/var/www/%s/nginx" % s                # template text uses the real paths and is
        common = os.path.join(self.etc, "common")      # relocated once, at the end
        for f in os.listdir(common):          # GridPane removes the other cache families
            if f.startswith(s + "-"):
                os.remove(os.path.join(common, f))
        hooks = {"include /etc/nginx/extra.d/*-root-context.conf;": "include %s/*-root-context.conf;" % site,
                 "include /etc/nginx/extra.d/*-php-context.conf;": "include %s/*-php-context.conf;" % site}
        if kind == "fcgi":
            t = read("/etc/nginx/common/wpfc.conf")
            hooks["include /etc/nginx/extra.d/*-skip-cache-context.conf;"] = "include %s/*skip-fcgi-cache-context.conf;" % site
            hooks["include /etc/nginx/common/fcgi-cache-var.conf;"] = "include /etc/nginx/common/%s-fcgi-cache-var.conf;" % s
            name = "%s-wpfc.conf" % s
            v = read("/etc/nginx/common/fcgi-cache-var.conf")
            write(os.path.join(common, "%s-fcgi-cache-var.conf" % s), mutate(v) if mutate else v)
        elif kind in ("redis", "redis-legacy"):
            t = read("/etc/nginx/common/wp-redis%s.conf" % ("-legacy" if kind == "redis-legacy" else ""))
            hooks["include /etc/nginx/extra.d/*-skip-cache-context.conf;"] = "include %s/*-skip-redis-cache-context.conf;" % site
            name = "%s-wp-redis.conf" % s
        else:
            t = read("/etc/nginx/common/php.conf")
            name = "%s-php.conf" % s
        for a, b in hooks.items():
            if a not in t:
                raise SetupError("GridPane template changed: '%s' not found" % a)
            t = t.replace(a, b)
        if kind == "redis":
            t = (t.replace("nginx-cache:", "db%d:" % db).replace("set $redis_db 0;", "set $redis_db %d;" % db)
                 .replace("redis2_query select 0;", "redis2_query select %d;" % db).replace("dummy", "x"))
        if kind != "fcgi" and mutate:
            t = mutate(t)
        if kind == "fcgi" and tmutate:
            t = tmutate(t)
        t = self.relocate(t)
        write(os.path.join(common, name), t)
        v = "server {\n    listen 127.0.0.1:8;\n    server_name %s;\n    root %s/%s/htdocs;\n" \
            "    set $sockfile php;\n%s    include %s/%s;\n}\n" % (
                s, self.www, s, ("    include %s/acl.conf;\n" % common) if acl else "", common, name)
        write(os.path.join(self.etc, "sites-available", s), v)
        write(os.path.join(self.etc, "sites-enabled", s), v)

    # -- running things ----------------------------------------------------------
    def run(self, *args):
        env = dict(os.environ, GP_MKT_NO_UPDATE_CHECK="1")
        # -e: errors found before the config's own error_log is read go to the test root, not to
        # the server's /var/log/nginx/error.log
        env["BASH_FUNC_nginx%%"] = '() { %s -e %s/error.log -p %s/ -c %s/nginx.conf "$@"; }' % (NGINX, self.d, self.etc, self.etc)
        env["BASH_FUNC_systemctl%%"] = '() { echo "$*" >> %s; }' % self.syslog
        open(self.syslog, "w").close()
        r = subprocess.run(["bash", os.path.join(self.repo, "install.sh")] + list(args), env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
        return r.returncode, r.stdout + r.stderr

    def nginx_t(self):
        r = subprocess.run([NGINX, "-e", os.path.join(self.d, "error.log"), "-p", self.etc + "/",
                            "-c", os.path.join(self.etc, "nginx.conf"), "-t"],
                           capture_output=True, text=True)
        return r.returncode == 0 and "[warn]" not in r.stderr, r.stderr.strip()

    def reloaded(self):
        return "reload nginx" in read(self.syslog)

    def site(self, s, f):
        return os.path.join(self.www, s, "nginx", f)

    def files(self, s):
        return sorted(f for f in (SWITCH, FLAG, FLAG_REDIS) if os.path.exists(self.site(s, f)))

    def snapshot(self, skip=()):
        out = {}
        for top in (self.mp, os.path.join(self.etc, "conf.d"), self.www):
            for dp, _, fs in os.walk(top):
                for f in fs:
                    p = os.path.join(dp, f)
                    if any(x in p for x in skip):
                        continue
                    with open(p, "rb") as fh:
                        out[p] = hashlib.sha256(fh.read()).hexdigest()
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

    def clean_run(self, name, rc, out, want_rc=0):
        self.check("%s: exit code %d, no rollback" % (name, want_rc),
                   rc == want_rc and "unexpected error" not in out and "restoring previous files" not in out, out[-1500:])
        ok, t = self.nginx_t()
        self.check("%s: nginx -t OK, no warnings" % name, ok, t[-600:])


def scenarios(T):
    src_redis = read(os.path.join(T.repo, "site", "marketing-params-php-context.redis.conf"))
    src_fcgi = read(os.path.join(T.repo, "site", SWITCH))
    redis_sw = lambda s: src_redis.replace("@SITE@", s)   # noqa: E731 (what install.sh writes)
    fcgi_sw = lambda s: src_fcgi.replace("@SITE@", s)     # noqa: E731

    def named(s, kind):
        f = FLAG if kind == "fcgi" else FLAG_REDIS
        return (T.files(s) == sorted([SWITCH, f])
                and read(T.site(s, SWITCH)) == (fcgi_sw(s) if kind == "fcgi" else redis_sw(s))
                and 'set $gp_mkt_%s "%s";' % ("fcgi" if kind == "fcgi" else "redis", s) in read(T.site(s, f)))

    T.section("1. Fresh server: engine only")
    rc, out = T.run()
    T.clean_run("install", rc, out)
    T.check("engine files installed", all(os.path.exists(os.path.join(T.mp, f)) for f in ENGINE_FILES + ("params.list",))
            and os.path.exists(os.path.join(T.etc, "conf.d/marketing-params.conf")))
    T.check("the engine includes the test root's list (not the server's)",
            "include %s/params[.]list;" % T.mp in read(os.path.join(T.mp, "engine.conf"))
            and "/etc/nginx/marketing-params/" not in read(os.path.join(T.mp, "engine.conf")).replace(T.etc, ""))
    r = subprocess.run(["sha256sum", "-c", "--quiet", ".manifest.sha256"], cwd=T.mp, capture_output=True, text=True)
    T.check("manifest lists all %d engine files and matches" % len(ENGINE_FILES), r.returncode == 0
            and len(read(os.path.join(T.mp, ".manifest.sha256")).splitlines()) == len(ENGINE_FILES), r.stdout + r.stderr)
    T.check("nginx reloaded, status OK", T.reloaded() and "status: OK" in out, out[-800:])

    T.section("2. Switch on: FastCGI site, Redis sites, a site without page cache")
    sites = [("fcgi.test", "fcgi", 0), ("redis.test", "redis", 2), ("redis2.test", "redis", 3), ("nocache.test", "php", 0),
             ("cafedalua.test", "redis", 6), ("luafc.test", "fcgi", 0)]
    legacy = os.path.exists("/etc/nginx/common/wp-redis-legacy.conf")
    if legacy:
        sites.append(("legacy.test", "redis-legacy", 0))
    for s, kind, db in sites:
        T.vhost(s, kind, db)
    rc, out = T.run(*[s for s, _, _ in sites])
    T.clean_run("install %d sites" % len(sites), rc, out)
    T.check("FastCGI site: FastCGI switch + flag carrying the site's name",
            named("fcgi.test", "fcgi") and "ON  for fcgi.test (FastCGI)" in out, out[-800:])
    for s in ("redis.test", "redis2.test"):
        T.check("%s: Redis switch + flag carrying the site's name" % s, named(s, "redis") and "ON  for %s (Redis)" % s in out,
                out[-800:])
    T.check("names containing 'lua' are not mistaken for GridPane's Lua feature",
            named("cafedalua.test", "redis") and named("luafc.test", "fcgi"), out[-800:])
    if legacy:
        T.check("legacy Redis template (prefix nginx-cache:, one database): switched on",
                named("legacy.test", "redis") and "ON  for legacy.test (Redis)" in out, out[-800:])
    T.check("site without page cache: skipped with a reason, no files",
            T.files("nocache.test") == [] and "nocache.test: uses neither" in out, out[-800:])
    rc, out = T.run("--status")
    T.check("--status: OK, lists both types", rc == 0 and "redis.test   (Redis)" in out
            and "fcgi.test   (FastCGI)" in out and "status: OK" in out, out[-1200:])

    T.vhost("auth.test", "redis", 7, acl=True)
    T.vhost("authfc.test", "fcgi", acl=True)
    rc, out = T.run("auth.test", "authfc.test")
    T.clean_run("sites behind GridPane's HTTP auth / IP allowlist", rc, out)
    T.check("Redis site behind HTTP auth: switched on, with a note that Redis is never read while it is on",
            named("auth.test", "redis") and "ON  for auth.test (Redis)" in out
            and "note: GridPane's HTTP auth / IP allowlist is on" in out, out[-800:])
    T.check("FastCGI site behind HTTP auth: switched on, no note (FastCGI is not affected)",
            named("authfc.test", "fcgi")
            and not any("authfc.test" in l and "HTTP auth" in l for l in out.splitlines()), out[-800:])
    rc, out = T.run("--status")
    T.check("--status: notes HTTP auth on the Redis site, still OK", rc == 0 and "status: OK" in out
            and "auth.test   (Redis; GridPane's HTTP auth / IP allowlist is on" in out
            and "authfc.test   (FastCGI)" in out, out[-1200:])
    T.run("--disable", "auth.test", "authfc.test")

    T.section("3. Re-running the same install changes nothing")
    before = T.snapshot()
    rc, out = T.run("fcgi.test", "redis.test", "redis2.test")
    T.clean_run("re-install", rc, out)
    T.check("every file byte-identical", T.snapshot() == before)

    T.section("4. Templates and site files the installer must refuse: skipped, old switch removed, nothing else touched")
    key = 'set $key "db5:$scheme$request_method$host$request_uri";'
    php_inc = "include /var/www/bad.test/nginx/*-php-context.conf;"
    variants = [
        ("redis", "customised key (like GeoIP)",
         lambda t: t.replace(key, key.replace('$request_uri"', '$request_uri$http_x_country"')), None, "cache key is not"),
        ("redis", "$escaped_key line changed",
         lambda t: t.replace("set_escape_uri $escaped_key $key;", 'set_escape_uri $escaped_key "$key";'), None,
         "$escaped_key is not computed before"),
        ("redis", "site php-context include missing", lambda t: t.replace(php_inc, ""), None, "$escaped_key is not computed before"),
        ("redis", "REQUEST_URI set again after the include",
         lambda t: t.replace(php_inc, php_inc + "\n    fastcgi_param REQUEST_URI $request_uri;"), None, "sets REQUEST_URI again"),
        ("redis", "fastcgi_params included again after the include",
         lambda t: t.replace(php_inc, php_inc + "\n    include /etc/nginx/fastcgi_params;"), None, "sets REQUEST_URI again"),
        ("redis", "second set $key (add-on-domain overlay)",
         lambda t: t.replace("location ~ \\.php$ {", "location @sub {\n    %s\n}\n\nlocation ~ \\.php$ {" % key, 1),
         None, "cache key is not"),
        ("redis", "another site's php-context included (add-on-domain overlay)",
         lambda t: t.replace("location ~ \\.php$ {", "location @sub {\n    include /var/www/child.test/nginx/*-php-context.conf;\n}\n\n"
                             "location ~ \\.php$ {", 1), None, "includes another site's"),
        ("redis", "GridPane Lua", lambda t: t.replace('set $skip_reason "";', 'set $skip_reason "";\nset $cache_uri $request_uri;', 1),
         None, "Lua"),
        ("redis", "another php-context file changes $key", None,
         ("zz-custom-php-context.conf", 'set $key "db5:custom$request_uri";\n'), "changes the cache key or REQUEST_URI"),
        ("redis", "another php-context file includes fastcgi_params", None,
         ("zz-custom-php-context.conf", "include fastcgi_params;\n"), "changes the cache key or REQUEST_URI"),
        ("fcgi", "customised FastCGI key", lambda v: v.replace('"$scheme$request_method$host$request_uri"',
                                                               '"$scheme$request_method$http_x_country$host$request_uri"'),
         None, "customised FastCGI cache key"),
        ("fcgi", "FastCGI: stock key commented out, customised key active (GeoIP switched by hand)",
         lambda v: v.replace('\nfastcgi_cache_key "$scheme$request_method$host$request_uri";',
                             '\n#fastcgi_cache_key "$scheme$request_method$host$request_uri";\n'
                             'fastcgi_cache_key "$scheme$request_method$http_x_country$host$request_uri";'),
         None, "customised FastCGI cache key"),
        ("fcgi", "FastCGI: another site's php-context included, REQUEST_URI set after it (add-on-domain overlay)",
         ("tmutate", lambda t: t.replace("location ~ \\.php$ {", "location @sub {\n    include /etc/nginx/fastcgi_params;\n"
                                         "    include /var/www/child.test/nginx/*-php-context.conf;\n"
                                         "    fastcgi_param REQUEST_URI $request_uri;\n    fastcgi_pass php;\n}\n\n"
                                         "location ~ \\.php$ {", 1)),
         None, "includes another site's"),
        ("fcgi", "FastCGI: another php-context file sets REQUEST_URI", None,
         ("zz-custom-php-context.conf", "fastcgi_param REQUEST_URI $request_uri;\n"), "changes the cache key or REQUEST_URI"),
    ]
    for kind, label, mutate, extra, reason in variants:
        T.vhost("bad.test", kind, 5)
        rc, out = T.run("bad.test")
        if not named("bad.test", kind):
            T.check("%s: setup (stock template enabled first)" % label, False, out[-600:])
            continue
        if isinstance(mutate, tuple):
            T.vhost("bad.test", kind, 5, tmutate=mutate[1])
        else:
            T.vhost("bad.test", kind, 5, mutate=mutate)
        if extra:
            write(T.site("bad.test", extra[0]), extra[1])
        before = T.snapshot(skip=("/bad.test/",))
        rc, out = T.run("bad.test")
        T.clean_run(label, rc, out)
        T.check("%s: skipped with the reason, old switch files removed" % label,
                "bad.test: " in out and reason in out and "- skipped" in out and "old switch files were removed" in out
                and T.files("bad.test") == [], out[-800:])
        T.check("%s: engine and other sites untouched" % label, T.snapshot(skip=("/bad.test/",)) == before)
        if extra:
            os.remove(T.site("bad.test", extra[0]))
    T.vhost("bad.test", "redis", 5)
    T.run("bad.test")
    write(T.site("bad.test", "zz-custom-php-context.conf"), "fastcgi_param REQUEST_URI $request_uri;\n")
    rc, out = T.run("--status")
    T.check("--status: a site file added later is reported as ON and NOT safe",
            rc == 1 and "bad.test   !! zz-custom-php-context.conf changes the cache key or REQUEST_URI after the switch: "
                        "the feature is ON and NOT safe" in out, out[-1200:])
    os.remove(T.site("bad.test", "zz-custom-php-context.conf"))
    T.run("--disable", "bad.test")

    T.section("5. Site arguments")
    rc, out = T.run("redis.test/")
    T.clean_run("trailing slash (tab completion)", rc, out)
    T.check("'redis.test/' is redis.test: stays ON", named("redis.test", "redis") and "ON  for redis.test (Redis)" in out, out[-600:])
    before = T.snapshot()
    rc, out = T.run("../etc")
    T.check("'../etc' is refused, nothing changed", rc == 1 and "not a site name" in out and T.snapshot() == before, out[-600:])

    T.section("6. Cache type changed in GridPane: Redis -> FastCGI -> Redis -> no cache")
    T.vhost("redis2.test", "fcgi")
    ok, t = T.nginx_t()
    T.check("Redis switch left on the now-FastCGI site: nginx -t OK", ok, t[-600:])
    T.check("...and it never touches the FastCGI key (only the FastCGI switch sets fastcgi_cache_key)",
            "fastcgi_cache_key" not in read(os.path.join(T.mp, "php-context.conf"))
            and "fastcgi_cache_key" not in read(os.path.join(T.mp, "php-context-redis.conf")))
    rc, out = T.run("--status")
    T.check("--status: inactive, re-run the installer", rc == 1
            and "redis2.test   !! Redis switch, but the site now uses FastCGI page cache (inactive)" in out, out[-1200:])
    rc, out = T.run("redis2.test")
    T.clean_run("re-install as FastCGI", rc, out)
    T.check("now FastCGI switch + flag, Redis flag gone", named("redis2.test", "fcgi")
            and "ON  for redis2.test (FastCGI)" in out, out[-800:])
    T.vhost("redis2.test", "redis", 3)
    rc, out = T.run("--status")
    T.check("--status: inactive, re-run the installer", rc == 1
            and "redis2.test   !! FastCGI switch, but the site now uses Redis page cache (inactive)" in out, out[-1200:])
    rc, out = T.run("redis2.test")
    T.clean_run("re-install as Redis", rc, out)
    T.check("now Redis switch + flag, FastCGI flag gone", named("redis2.test", "redis"), out[-800:])
    T.vhost("redis2.test", "php")
    ok, t = T.nginx_t()
    T.check("Redis switch left on a site without page cache: nginx -t OK", ok, t[-600:])
    rc, out = T.run("--status")
    T.check("--status: switch on a site without page cache", rc == 1
            and "redis2.test   !! switch present, but the site has no FastCGI or Redis page cache" in out, out[-1200:])
    rc, out = T.run("redis2.test")
    T.clean_run("re-install on a site without page cache", rc, out)
    T.check("skipped, its switch files removed", T.files("redis2.test") == [] and "old switch files were removed" in out, out[-800:])

    T.section("7. Clone / staging copy (GridPane copies the site's nginx dir verbatim)")
    T.vhost("clone.test", "redis", 4)
    for f in (SWITCH, FLAG_REDIS):
        shutil.copy(T.site("redis.test", f), T.site("clone.test", f))
    rc, out = T.run("--status")
    T.check("--status: reports the copy and asks for a re-run", rc == 1
            and "clone.test   !! switch files copied from redis.test" in out, out[-1200:])
    rc, out = T.run("clone.test")
    T.clean_run("re-install on the clone", rc, out)
    T.check("clone now carries its own name", named("clone.test", "redis"))
    write(T.site("clone.test", FLAG_REDIS), read(T.site("redis.test", FLAG_REDIS)))
    rc, out = T.run("--status")
    T.check("flag of another site + own switch: --status says inactive",
            "clone.test   !! flag (redis.test) and switch (clone.test) belong to different sites (inactive)" in out, out[-1200:])
    rc, out = T.run()
    T.clean_run("plain update", rc, out)
    T.check("the plain update rewrites it with the site's own name", named("clone.test", "redis")
            and "updated the switch files of clone.test" in out, out[-800:])

    T.vhost("push.test", "fcgi")
    T.run("push.test")
    for f in (SWITCH, FLAG_REDIS):                   # a staging push from Redis site redis.test: GridPane
        shutil.copy(T.site("redis.test", f), T.site("push.test", f))   # copies its files, deletes none
    ok, t = T.nginx_t()
    T.check("staging push left the site's FastCGI flag next to a Redis switch: nginx -t OK", ok, t[-600:])
    rc, out = T.run()
    T.clean_run("plain update after the push", rc, out)
    T.check("the update renames the Redis files and removes the FastCGI flag (the switch's type wins)",
            named("push.test", "redis") and not os.path.exists(T.site("push.test", FLAG)), out[-800:])
    rc, out = T.run("--status")
    T.check("--status: Redis switch on a FastCGI site, re-run the installer",
            "push.test   !! Redis switch, but the site now uses FastCGI page cache (inactive)" in out, out[-1200:])
    rc, out = T.run("push.test")
    T.clean_run("re-install after the push", rc, out)
    T.check("FastCGI again", named("push.test", "fcgi"), out[-800:])
    T.run("--disable", "push.test")

    T.section("8. nginx -t fails: every file restored")
    T.run("--disable", "clone.test")
    write(os.path.join(T.etc, "conf.d/zz-broken.conf"), "this_is_not_a_directive on;\n")
    before = T.snapshot()
    rc, out = T.run("clone.test")
    T.check("install exits 1 with nginx's error and a rollback", rc == 1 and "nginx -t failed" in out
            and "restoring previous files" in out, out[-800:])
    T.check("files exactly as before", T.snapshot() == before)
    T.check("nginx not reloaded", not T.reloaded())
    os.remove(os.path.join(T.etc, "conf.d/zz-broken.conf"))
    ok, t = T.nginx_t()
    T.check("nginx -t OK again", ok, t[-600:])

    T.section("9. --apply-list")
    lst = os.path.join(T.mp, "params.list")
    good = read(lst)
    write(lst, good + "\nbroken_line 1\n")
    rc, out = T.run("--apply-list")
    T.check("broken list: exit 1, last good list restored, edit kept as .rejected",
            rc == 1 and read(lst) == good and os.path.exists(lst + ".rejected") and not T.reloaded(), out[-600:])
    write(lst, good + "\nmy_click_id 1;\n")
    rc, out = T.run("--apply-list")
    T.check("valid list: exit 0, reloaded", rc == 0 and "list applied" in out and T.reloaded(), out[-600:])
    ok, t = T.nginx_t()
    T.check("nginx -t OK", ok, t[-600:])

    T.section("10. --disable")
    rc, out = T.run("--disable", "redis.test", "fcgi.test/")
    T.clean_run("--disable", rc, out)
    T.check("both sites' files removed, nginx reloaded", T.files("redis.test") == [] and T.files("fcgi.test") == []
            and "OFF for redis.test" in out and "OFF for fcgi.test" in out and T.reloaded(), out[-600:])

    T.section("11. An engine and site files from before the Redis support (older release installed)")
    def old_layout():
        for f in ("php-context-fcgi.conf", "php-context-redis.conf"):
            if os.path.exists(os.path.join(T.mp, f)):
                os.remove(os.path.join(T.mp, f))
        subprocess.run("sha256sum engine.conf php-context.conf README.md limits.env > .manifest.sha256",
                       shell=True, cwd=T.mp, check=True)
        write(T.site("fcgi.test", SWITCH), "include %s/php-context[.]conf;\n" % T.mp)     # the old switch and flag
        write(T.site("fcgi.test", FLAG), "set $gp_mkt_fcgi 1;\n")
    old_layout()
    rc, out = T.run("--status")
    T.check("--status: old site files reported", rc == 1 and "fcgi.test   !! switch files of an older version" in out, out[-1200:])
    rc, out = T.run("--disable", "fcgi.test")
    T.clean_run("--disable with the old engine", rc, out)
    T.check("site switched off, files stay off after the reload", T.files("fcgi.test") == [] and T.reloaded(), out[-600:])
    old_layout()
    rc, out = T.run()
    T.clean_run("update to the new engine", rc, out)
    T.check("new engine files added, manifest complete", all(os.path.exists(os.path.join(T.mp, f)) for f in ENGINE_FILES)
            and len(read(os.path.join(T.mp, ".manifest.sha256")).splitlines()) == len(ENGINE_FILES), out[-600:])
    T.check("the site's old files were rewritten for this version, the site stays ON",
            named("fcgi.test", "fcgi") and "updated the switch files of fcgi.test" in out, out[-600:])
    rc, out = T.run("--status")
    T.check("--status OK afterwards", rc == 0 and "fcgi.test   (FastCGI)" in out, out[-1200:])

    T.section("12. --uninstall")
    T.vhost("redis.test", "redis", 2)
    T.run("redis.test")
    old_layout()                                     # also with an engine from before the Redis support
    rc, out = T.run("--uninstall")
    T.clean_run("--uninstall", rc, out)
    left = [p for p in T.snapshot() if p.endswith((SWITCH, FLAG, FLAG_REDIS))]
    T.check("no switch or flag left on any site", not left, " ".join(left))
    T.check("engine dir and stub removed, list saved", not os.path.exists(T.mp)
            and not os.path.exists(os.path.join(T.etc, "conf.d/marketing-params.conf"))
            and os.path.exists(os.path.join(T.home, "marketing-params.list.bak")), out[-600:])


def main():
    if os.geteuid() != 0:
        print("run as root (install.sh refuses to run otherwise)")
        return 2
    T = Root()
    try:
        T.build()
        print("installer test root: %s" % T.d)
        try:
            scenarios(T)
        except (OSError, subprocess.SubprocessError, AssertionError) as e:   # a broken install.sh can leave
            T.check("test run completed", False, "%s: %s" % (e.__class__.__name__, e))   # files missing
    except Skip as e:
        print(c("33", "installer test skipped: %s (it needs GridPane's Redis template and nginx modules)" % e))
        return 0
    except SetupError as e:
        print(c("31", "SETUP ERROR: ") + str(e))
        return 2
    finally:
        shutil.rmtree(T.d, ignore_errors=True)
    total = T.passed + len(T.failed)
    print()
    if T.failed:
        print(c("31", "%d of %d installer checks FAILED" % (len(T.failed), total)))
        for n in T.failed:
            print("  - " + n)
        return 1
    print(c("32", "ALL %d INSTALLER CHECKS PASSED" % total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
