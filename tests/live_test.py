#!/usr/bin/env python3
"""
Live smoke test of an installed site, run ON the GridPane server.

  ./test.sh live example.com            test the home page
  ./test.sh live example.com /about/    test a specific page (best: a normal page with a
                                        trailing slash -> the redirect check runs too)

Talks to the local nginx (127.0.0.1:443, SNI = the site), so Cloudflare is not involved.
Only GET requests; it purges nothing. The first request may create the normal cache entry
of the page (exactly what a visitor would do).
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
    sw = os.path.exists("/var/www/%s/nginx/marketing-params-php-context.conf" % a.site)
    T.check("site switch present (/var/www/%s/nginx/marketing-params-php-context.conf)" % a.site, sw)

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

    total = T.passed + len(T.failed)
    print()
    if T.failed:
        print(c("31", "%d of %d checks FAILED" % (len(T.failed), total)))
        return 1
    print(c("32", "ALL %d CHECKS PASSED" % total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
