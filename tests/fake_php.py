"""
Minimal FastCGI responder standing in for PHP-FPM/WordPress in the sandbox test.
stdlib only. It reports what "PHP" received and imitates a few WordPress behaviours:

  every response   X-Test-Uri / X-Test-Qs     REQUEST_URI / QUERY_STRING (last value wins,
                                              like PHP-FPM)
                   X-Test-N-Uri / X-Test-N-Qs how many times each param was sent
                   X-Test-Gen                 global counter -> tells a cache HIT from a MISS
  /echo/...        body echoes REQUEST_URI and QUERY_STRING (cache-poisoning check)
  /loc/<kind>/     returns a redirect with a Location chosen by <kind> (see LOCATIONS)
  /path            (no trailing slash) WordPress-like canonical 301 to /path/ keeping the query
  www.<host>       WordPress-like 301 to the apex host keeping the query
"""
import socket
import struct
import threading

LOCATIONS = {
    # kind: (status, location template; {h} = apex host)
    "same":      (302, "https://{h}/target/"),
    "sameq":     (302, "https://{h}/target/?lang=pl"),
    "frag":      (302, "https://{h}/target/#frag"),
    "www":       (302, "https://www.{h}/target/"),
    "upper":     (302, "https://{H}/target/"),
    "port":      (302, "https://{h}:8443/target/"),
    "bare":      (302, "https://{h}"),
    "rel":       (302, "/target/"),
    "relq":      (302, "/target/?lang=pl"),
    "relnoslash": (302, "target/"),
    "ext":       (302, "https://example.org/x"),
    "protorel":  (302, "//evil.example/x"),
    "backslash": (302, "/\\evil.example/x"),
    "tab":       (302, "/\t/evil.example/x"),
    "suffix":    (302, "https://{h}.evil.example/x"),
    "prefix":    (302, "https://evil{h}/x"),
    "userinfo":  (302, "https://{h}@evil.example/x"),
    "s301":      (301, "https://{h}/target/"),
    "s303":      (303, "https://{h}/target/"),
    "s307":      (307, "https://{h}/target/"),
    "s308":      (308, "https://{h}/target/"),
    "s201":      (201, "https://{h}/target/"),
}

_gen_lock = threading.Lock()
_gen = [0]


def _next_gen():
    with _gen_lock:
        _gen[0] += 1
        return _gen[0]


def _read(c, n):
    b = b""
    while len(b) < n:
        x = c.recv(n - len(b))
        if not x:
            raise EOFError
        b += x
    return b


def _pairs(data):
    i, out = 0, []
    while i < len(data):
        lens = []
        for _ in range(2):
            n = data[i]
            if n >> 7:
                n = struct.unpack(">I", data[i:i + 4])[0] & 0x7FFFFFFF
                i += 4
            else:
                i += 1
            lens.append(n)
        name = data[i:i + lens[0]]
        i += lens[0]
        val = data[i:i + lens[1]]
        i += lens[1]
        out.append((name.decode("latin1"), val.decode("latin1")))
    return out


def _respond(params):
    last = {}
    count = {}
    for n, v in params:
        last[n] = v
        count[n] = count.get(n, 0) + 1
    ru = last.get("REQUEST_URI", "")
    qs = last.get("QUERY_STRING", "")
    host = last.get("HTTP_HOST", "localhost")
    apex = host[4:] if host.startswith("www.") else host
    path = ru.split("?", 1)[0]
    gen = _next_gen()

    headers = [
        ("X-Test-Uri", ru), ("X-Test-Qs", qs),
        ("X-Test-N-Uri", str(count.get("REQUEST_URI", 0))),
        ("X-Test-N-Qs", str(count.get("QUERY_STRING", 0))),
        ("X-Test-Gen", str(gen)),
        ("Content-Type", "text/html; charset=UTF-8"),
    ]
    status, body = 200, "ok gen=%d\n" % gen
    keep_q = ("?" + qs) if qs else ""

    if path.startswith("/loc/"):
        kind = path.split("/")[2]
        st, tpl = LOCATIONS.get(kind, (302, "https://{h}/target/"))
        status = st
        headers.append(("Location", tpl.format(h=apex, H=apex.upper())))
        body = ""
    elif path.startswith("/echo/"):
        body = "<a href=\"%s\">self</a> qs=%s\n" % (ru, qs)
    elif not path.endswith("/") and not path.endswith(".php"):
        status = 301
        headers.append(("Location", "https://%s%s/%s" % (apex, path, keep_q)))
        body = ""
    elif host.startswith("www."):
        status = 301
        headers.append(("Location", "https://%s%s%s" % (apex, path, keep_q)))
        body = ""

    head = "Status: %d X\r\n" % status + "".join("%s: %s\r\n" % h for h in headers) + "\r\n"
    return (head + body).encode("latin1")


def _handle(c):
    try:
        params, rid = b"", 1
        while True:
            ver, typ, rid, clen, plen, _ = struct.unpack(">BBHHBB", _read(c, 8))
            body = _read(c, clen)
            _read(c, plen)
            if typ == 4 and clen:
                params += body
            if typ == 5 and clen == 0:
                break
        payload = _respond(_pairs(params))
        for off in range(0, len(payload), 65535):
            chunk = payload[off:off + 65535]
            c.sendall(struct.pack(">BBHHBB", 1, 6, rid, len(chunk), 0, 0) + chunk)
        c.sendall(struct.pack(">BBHHBB", 1, 6, rid, 0, 0, 0))
        c.sendall(struct.pack(">BBHHBB", 1, 3, rid, 8, 0, 0) + struct.pack(">IB3x", 0, 0))
    except Exception:
        pass
    finally:
        c.close()


def start(port):
    """Start the responder on 127.0.0.1:port in a daemon thread."""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(128)

    def loop():
        while True:
            c, _ = s.accept()
            threading.Thread(target=_handle, args=(c,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return s
