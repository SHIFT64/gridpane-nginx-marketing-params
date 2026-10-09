#!/usr/bin/env python3
"""
Live smoke test of an installed site, run ON the GridPane server.

  ./test.sh live example.com            test the home page
  ./test.sh live example.com /about/    test a specific page (best: a normal page with a
                                        trailing slash -> the redirect check runs too)

Talks to the local nginx (127.0.0.1:443, SNI = the site), so no proxy/CDN in front is involved.
Only GET requests. The first request may create the normal cache entry of the page (exactly
what a visitor would do). The last check sends one purge request the way WordPress does
(the site's hostname via normal DNS) for a URL that is never cached, so it purges nothing.
"""
import argparse
import http.client
import os
import random
import socket
import ssl
import string
import sys

COLOR = sys.stdout.isatty()
# served from the cache (UPDATING/STALE = expired entry served while nginx refreshes it in the background)
FROM_CACHE = ("HIT", "STALE", "UPDATING", "REVALIDATED")


def c(code, s):
    return "\033[%sm%s\033[0m" % (code, s) if COLOR else s


class Live:
    def __init__(self, site, ip):
        self.site, self.ip = site, ip
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE
        self.passed, self.failed = 0, []

    def get(self, target, host=None):
        host = host or self.site
        conn = http.client.HTTPConnection(self.ip, 443, timeout=20)
        raw = socket.create_connection((self.ip, 443), timeout=20)
        conn.sock = self.ctx.wrap_socket(raw, server_hostname=host)   # SNI = site
        conn.putrequest("GET", target, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host)
        conn.putheader("User-Agent", "gp-marketing-params-live-test")
        conn.endheaders()
        r = conn.getresponse()
        body = r.read().decode("utf-8", "replace")
        h = {}
        for k, v in r.getheaders():
            h.setdefault(k.lower(), v)
        h[":status"] = r.status
        conn.close()
        return h, body

    def check(self, name, ok, h=None, extra=""):
        if ok:
            self.passed += 1
            print("  " + c("32", "PASS") + " " + name)
        else:
            self.failed.append(name)
            info = ""
            if h is not None:
                info = "status=%s x-grid-cache=%s skip=%s mkt=%s location=%s" % (
                    h.get(":status"), h.get("x-grid-cache"), h.get("x-grid-cache-skip"),
                    h.get("x-grid-cache-mkt"), h.get("location"))
            print("  " + c("31", "FAIL") + " " + name + ("\n       " + info if info else "") +
                  ("\n       " + extra if extra else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("site")
    ap.add_argument("path", nargs="?", default="/")
    ap.add_argument("--ip", default="127.0.0.1", help="origin IP (default 127.0.0.1)")
    a = ap.parse_args()
    path = a.path if a.path.startswith("/") else "/" + a.path
    T = Live(a.site, a.ip)
    tok = "gpmkt" + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))

    print(c("1", "Installation"))
    eng = os.path.exists("/etc/nginx/marketing-params/engine.conf")
    T.check("engine installed (/etc/nginx/marketing-params/)", eng)
    enabled = True
    for f in ("marketing-params-php-context.conf", "marketing-params-skip-fcgi-cache-context.conf"):
        present = os.path.exists("/var/www/%s/nginx/%s" % (a.site, f))
        enabled = enabled and present
        T.check("site file present (/var/www/%s/nginx/%s)" % (a.site, f), present)
    if not (eng and enabled):
        print("\n" + c("33", "The site is not enabled, so the cache tests would only show stock GridPane behaviour."))
        print("Run:  ./install.sh %s   (it prints the reason if it has to skip the site)" % a.site)
        print("then: ./test.sh live %s %s" % (a.site, path))
        return 1

    print(c("1", "\nCache behaviour on https://%s%s" % (a.site, path)))
    sep = "&" if "?" in path else "?"
    h, body = T.get("%s%sgclid=%s&utm_source=%s" % (path, sep, tok, tok))
    if h[":status"] in (301, 302) and h.get("location"):
        T.check("page answered with a redirect - pick a final URL (e.g. with trailing slash)", False, h)
    T.check("marketing-only URL is not skipped (X-Grid-Cache-Mkt: ignored)",
            h.get("x-grid-cache-mkt") == "ignored" and h.get("x-grid-cache") != "BYPASS", h)
    T.check("param values are not baked into the HTML", tok not in body, h)
    h2, _ = T.get("%s%sgclid=%sB&fbclid=%s" % (path, sep, tok, tok))
    T.check("another marketing variant -> served from cache (HIT)", h2.get("x-grid-cache") in FROM_CACHE, h2)
    h3, _ = T.get(path)
    T.check("clean URL -> served from the same cache entry (HIT)", h3.get("x-grid-cache") in FROM_CACHE, h3)
    h4, _ = T.get("%s%sgp_mkt_probe=%s" % (path, sep, tok))
    T.check("non-marketing param -> stock BYPASS -query_string",
            h4.get("x-grid-cache") == "BYPASS" and "-query_string" in (h4.get("x-grid-cache-skip") or "")
            and "x-grid-cache-mkt" not in h4, h4)
    h5, _ = T.get("%s%sgclid=%s&gp_mkt_probe=1" % (path, sep, tok))
    T.check("marketing + non-marketing param -> stock BYPASS", h5.get("x-grid-cache") == "BYPASS", h5)

    print(c("1", "\nRedirects keep the params"))
    if path != "/" and path.endswith("/") and "?" not in path:
        h6, _ = T.get("%s?gclid=%s&utm_source=x" % (path.rstrip("/"), tok))
        loc = h6.get("location", "")
        T.check("no trailing slash -> 301 Location keeps gclid/utm_source",
                h6[":status"] in (301, 302, 307, 308) and ("gclid=" + tok) in loc and "utm_source=x" in loc, h6)
    else:
        print("  skip trailing-slash check (give a page path like /about/ to enable it)")
    try:
        h7, _ = T.get("%s%sgclid=%s" % (path, sep, tok), host="www." + a.site)
        if h7[":status"] in (301, 302, 307, 308):
            T.check("www -> apex redirect keeps gclid", ("gclid=" + tok) in h7.get("location", ""), h7)
        else:
            print("  skip www check (www.%s does not redirect: %s)" % (a.site, h7[":status"]))
    except Exception as e:
        print("  skip www check (%s)" % e.__class__.__name__)

    # Nginx Helper sends GET https://<site>/purge/<path> through normal DNS (often the public IP),
    # not to 127.0.0.1. If Basic Auth or an IP allowlist answers it, no purge ever works.
    print(c("1", "\nPurge requests reach this nginx (the way WordPress sends them)"))
    probe = "/purge/gp-mkt-purge-probe-%s/" % tok          # a URL that is never cached: purges nothing
    try:
        ip = socket.getaddrinfo(a.site, 443, proto=socket.IPPROTO_TCP)[0][4][0]
    except Exception as e:
        ip = "unresolved (%s)" % e.__class__.__name__
    try:
        conn = http.client.HTTPSConnection(a.site, 443, timeout=10, context=ssl.create_default_context())
        conn.request("GET", probe, headers={"User-Agent": "gp-marketing-params-live-test"})
        st = conn.getresponse().status
        conn.close()
        hint = {401: "Basic Auth answers the server's own requests: allow the server IP (acl.conf) or Nginx Helper cannot purge",
                403: "an allowlist blocks the server's own requests to /purge/ (acl.conf / 7G / firewall)",
                404: "no purge location answers: is FastCGI caching (ngx_cache_purge) on for this site?"}.get(st, "")
        T.check("GET %s via %s (%s) -> 412/200 (not blocked)" % (probe, a.site, ip), st in (200, 412),
                extra="status=%s %s" % (st, hint))
    except Exception as e:
        T.check("GET %s via %s (%s) -> 412/200 (not blocked)" % (probe, a.site, ip), False,
                extra="%s: %s" % (e.__class__.__name__, e))

    total = T.passed + len(T.failed)
    print()
    if T.failed:
        print(c("31", "%d of %d checks FAILED" % (len(T.failed), total)))
        return 1
    print(c("32", "ALL %d CHECKS PASSED" % total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
