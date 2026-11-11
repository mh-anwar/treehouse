"""End-to-end: run the real sharedbox-gate with fake Cloudflare logins against throwaway backends. Usage: python3 tests/e2e_gate.py <repo>"""
import asyncio
import base64
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WT = sys.argv[1]
import tempfile
TMP = tempfile.mkdtemp(prefix="e2e-gate-")
conf = os.path.join(TMP, "gate.conf")
shared_json = os.path.join(TMP, "shared.json")
with open(conf, "w") as f:
    f.write("PUBLIC_URL=https://treehouse.test\nPUBLIC_MODE=cloudflare\nCF_TEAM_DOMAIN=x.cloudflareaccess.com\n"
            "CF_ACCESS_AUD=aaaaaaaaaaaaaaaaaaaa\nGATE_BACKEND_HOST=127.0.0.1\nCF_OWNER_EMAILS=owner@x.io\n"
            "PORT_BASE=31000\nPORT_BLOCK=10\nFRIEND_UID_MIN=2000\nOWNER_T3_PORT=31018\nSHARED_PUBLIC_LINKS=yes\n")
with open(shared_json, "w") as f:
    json.dump({"web": {"port": 31100}, "down": {"port": 31101}, "BAD": {"port": 1}}, f)
os.environ.update(SHAREDBOX_CONF=conf, SHAREDBOX_LIB=os.path.join(WT, "lib"), SHAREDBOX_STATE=TMP)
sys.path.insert(0, os.path.join(WT, "lib"))
loader = importlib.machinery.SourceFileLoader("gate", os.path.join(WT, "lib/sharedbox-gate"))
g = importlib.util.module_from_spec(importlib.util.spec_from_loader("gate", loader))
loader.exec_module(g)
import sbaccess  # noqa: E402

g.sb.SHARED_JSON = shared_json
g.sb.friend_users = lambda reg=None: {"alice": {"uid": 2000, "email": "alice@x.io"},
                                      "bob": {"uid": 2001, "email": "bob@x.io"}}
TOKENS = {"A": "alice@x.io", "B": "bob@x.io", "O": "owner@x.io", "N": "nobody@x.io"}


class FakeVerifier:
    def verify(self, tok):
        if tok not in TOKENS:
            raise sbaccess.InvalidToken("nope")
        return {"email": TOKENS[tok], "exp": 4e9}


g.VERIFIER = FakeVerifier()


class Echo(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/go"):
            self.send_response(302)
            self.send_header("Location", "/login?next=1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/abs"):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/x")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps({"port": self.server.server_port, "path": self.path,
                           "headers": {k.lower(): v for k, v in self.headers.items()}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "app=1; Path=/; Domain=treehouse.test; HttpOnly")
        self.send_header("Set-Cookie", "t3_session_x=evil; Path=/")
        self.send_header("Set-Cookie", "deep=2; Path=/api")
        self.send_header("Service-Worker-Allowed", "/")
        self.send_header("Clear-Site-Data", '"cookies"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET


def serve_http(port, host="127.0.0.1"):
    s = ThreadingHTTPServer((host, port), Echo)
    threading.Thread(target=s.serve_forever, daemon=True).start()


async def ws_echo(reader, writer):
    head = await reader.readuntil(b"\r\n\r\n")
    key = [ln.split(b":", 1)[1].strip() for ln in head.split(b"\r\n") if ln.lower().startswith(b"sec-websocket-key")][0]
    acc = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
    path = head.split(b" ")[1]
    writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                 b"Sec-WebSocket-Accept: " + acc + b"\r\nX-Path: " + path + b"\r\n\r\n")
    await writer.drain()
    while data := await reader.read(65536):
        writer.write(b"echo:" + data)
        await writer.drain()
    writer.close()


GATE = 38091


def req(path, tok=None, method="GET", extra=(), body=b""):
    s = socket.create_connection(("127.0.0.1", GATE))
    h = [f"{method} {path} HTTP/1.1", "Host: treehouse.test", "Connection: close"]
    if tok:
        h.append(f"Cf-Access-Jwt-Assertion: {tok}")
    h += list(extra)
    if body:
        h.append(f"Content-Length: {len(body)}")
    s.sendall(("\r\n".join(h) + "\r\n\r\n").encode() + body)
    data = b""
    while chunk := s.recv(65536):
        data += chunk
    s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    hdrs = [tuple(x.split(": ", 1)) for x in lines[1:]]
    return int(lines[0].split()[1]), hdrs, body


def hv(hdrs, name):
    return [v for n, v in hdrs if n.lower() == name]


results = []


def check(name, cond, info=""):
    results.append(cond)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {info}"))


def run_tests():
    # group server: friend, other friend, owner, no account, no token
    for tok in "ABO":
        c, h, b = req("/s/web/hello/x?q=1", tok)
        j = json.loads(b) if c == 200 else {}
        check(f"/s/web as {tok}", c == 200 and j["port"] == 31100 and j["path"] == "/hello/x?q=1", (c, b[:200]))
    c, h, b = req("/s/web/", "N")
    check("/s/web no account -> 403", c == 403, c)
    c, h, b = req("/s/web/")
    check("/s/web no token -> 403", c == 403, c)
    c, h, b = req("/s/nope/", "A")
    check("/s/unknown -> 404", c == 404, c)
    c, h, b = req("/s/BAD/", "A")
    check("/s/BAD (bad name) -> 404", c == 404, c)
    c, h, b = req("/s/web?x=1", "A")
    check("/s/web -> 308 /s/web/?x=1", c == 308 and hv(h, "location") == ["/s/web/?x=1"], (c, h))
    c, h, b = req("/s/down/", "A")
    check("/s/down not running -> 502 with hint", c == 502 and b"isn't running" in b and b"box shared start" in b,
          (c, b[:300]))

    # headers / cookies
    c, h, b = req("/s/web/h", "A", extra=["Cookie: CF_Authorization=A; __Host-sbgate=zz; t3_session_1=secret; app=1",
                                         "X-Forwarded-Prefix: /evil", "X-Sharedbox-Cf-Email: owner@x.io"])
    j = json.loads(b)
    hd = j["headers"]
    check("cookie filtered to app's only", hd.get("cookie") == "app=1", hd.get("cookie"))
    check("X-Forwarded-Prefix set by gate", hd.get("x-forwarded-prefix") == "/s/web", hd.get("x-forwarded-prefix"))
    check("no sharedbox/CF headers forwarded", not any(k.startswith(("x-sharedbox", "cf-")) for k in hd), list(hd))
    check("host + xfh", hd.get("host") == "treehouse.test" and hd.get("x-forwarded-host") == "treehouse.test", hd)
    sc = hv(h, "set-cookie")
    check("Set-Cookie confined to prefix, t3 cookie dropped, Domain removed",
          sc == ["app=1; Path=/s/web/; HttpOnly", "deep=2; Path=/s/web/api"], sc)
    check("Service-Worker-Allowed / Clear-Site-Data dropped",
          not hv(h, "service-worker-allowed") and not hv(h, "clear-site-data"), h)
    c, h, b = req("/s/web/go", "A")
    check("Location /login -> /s/web/login", c == 302 and hv(h, "location") == ["/s/web/login?next=1"], h)
    c, h, b = req("/p/31001/abs", "A")
    check("absolute backend Location -> public prefixed",
          hv(h, "location") == ["https://treehouse.test/p/31001/x"], h)
    c, h, b = req("/s/web/p", "A", method="POST", body=b"hello")
    check("POST with body", c == 200 and json.loads(b)["path"] == "/p", (c, b[:200]))

    # own ports
    c, h, b = req("/p/31001/a", "A")
    check("/p/own port alice", c == 200 and json.loads(b)["port"] == 31001 and json.loads(b)["path"] == "/a", (c, b))
    c, h, b = req("/p/31001/a", "B")
    check("/p/alice port as bob -> 403", c == 403, c)
    c, h, b = req("/p/31001/a", "O")
    check("/p/alice port as owner -> 403", c == 403, c)
    c, h, b = req("/p/31000/", "A")
    check("/p/T3 port -> 403", c == 403 and b"T3" in b, (c, b[:200]))
    c, h, b = req("/p/31100/", "B")
    check("/p/group port as bob ok", c == 200, c)
    c, h, b = req("/p/31100/", "O")
    check("/p/group port as owner ok", c == 200, c)
    c, h, b = req("/p/31005/", "A")
    check("/p/own port not listening -> 502 hint", c == 502 and b"box serve start" in b, (c, b[:200]))
    c, h, b = req("/p/31011", "B")
    check("/p/31011 -> 308", c == 308 and hv(h, "location") == ["/p/31011/"], (c, h))
    c, h, b = req("/p/abc/", "A")
    check("/p/abc -> 404", c == 404, c)
    c, h, b = req("/p/31001/../31011/", "A")
    check("/p traversal -> 400", c == 400, c)
    c, h, b = req("/p/31001/%2e%2e/31011/", "A")
    check("/p encoded traversal -> 400", c == 400, c)

    # websocket through /p/
    s = socket.create_connection(("127.0.0.1", GATE))
    s.sendall(b"GET /p/31002/ws?x=1 HTTP/1.1\r\nHost: treehouse.test\r\nCf-Access-Jwt-Assertion: A\r\n"
              b"Connection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\n"
              b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n")
    s.settimeout(5)
    data = b""
    while b"\r\n\r\n" not in data:
        data += s.recv(4096)
    head, _, rest = data.partition(b"\r\n\r\n")
    ok101 = head.startswith(b"HTTP/1.1 101") and b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" in head and b"X-Path: /ws?x=1" in head
    s.sendall(b"ping")
    while len(rest) < 9:
        rest += s.recv(4096)
    s.close()
    check("WebSocket via /p/ (prefix stripped, echo)", ok101 and rest == b"echo:ping", (head, rest))
    # websocket to someone else's port refused
    s = socket.create_connection(("127.0.0.1", GATE))
    s.sendall(b"GET /p/31002/ws HTTP/1.1\r\nHost: treehouse.test\r\nCf-Access-Jwt-Assertion: B\r\n"
              b"Connection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\n"
              b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n")
    check("WebSocket to alice's port as bob -> 403", s.recv(4096).startswith(b"HTTP/1.1 403"))
    s.close()

    # T3 path unaffected: alice's T3 at 31000 (backend_for) with a t3 cookie -> proxied to / with cookie kept
    c, h, b = req("/x", "A", extra=["Cookie: t3_session_1=secret"])
    j = json.loads(b) if c == 200 else {}
    check("T3 fallback unchanged", c == 200 and j.get("port") == 31000 and j["path"] == "/x"
          and j["headers"].get("cookie") == "t3_session_1=secret" and "x-forwarded-prefix" not in j["headers"], (c, b[:300]))

    # ---- front door: dashboard first, "Open my T3 Code" button reaches T3 ----
    T3C = "Cookie: t3_session_1=secret"
    c, h, b = req("/", "A", extra=[T3C])
    check("/ with no Sec-Fetch (as if Cloudflare dropped it) -> dashboard", c == 303 and hv(h, "location") == ["/_box/me"], (c, h))
    c, h, b = req("/", "A", extra=[T3C, "Sec-Fetch-Site: none", "Sec-Fetch-Dest: document"])
    check("/ typed/bookmark -> dashboard", c == 303 and hv(h, "location") == ["/_box/me"], (c, h))
    c, h, b = req("/", "A", extra=[T3C, "Sec-Fetch-Site: same-origin", "Sec-Fetch-Dest: document"])
    check("/ same-origin click -> T3", c == 200 and json.loads(b).get("port") == 31000, (c, b[:200]))
    c, h, b = req("/", "A", extra=["Cookie: t3_session_1=secret; tb_open=1"])
    check("/ with button marker, no Sec-Fetch -> T3", c == 200 and json.loads(b).get("port") == 31000, (c, b[:200]))
    check("marker cleared on the way through", any(v.startswith("tb_open=;") and "Max-Age=0" in v for v in hv(h, "set-cookie")), h)
    c, h, b = req("/", "A", extra=["Cookie: tb_open=1"])
    check("/ with marker but browser not paired -> /_box/open", c == 303 and hv(h, "location") == ["/_box/open"], (c, h))
    c, h, b = req("/", "O", extra=["Cookie: t3_session_9=s; tb_open=1"])
    check("owner + marker -> owner's own T3", c == 200 and json.loads(b).get("port") == 31018, (c, b[:200]))
    c, h, b = req("/", "O", extra=["Cookie: t3_session_9=s"])
    check("owner bookmark -> dashboard", c == 303 and hv(h, "location") == ["/_box/me"], (c, h))
    c, h, b = req("/", "N", extra=["Cookie: tb_open=1"])
    check("no account + marker -> not T3", c != 200 or b"31000" not in b, (c, b[:200]))
    c, h, b = req("/", "A", extra=["Cookie: t3_session_1=secret; tb_direct=1"])
    check("/ with 'open T3 directly' chosen -> T3", c == 200 and json.loads(b).get("port") == 31000, (c, b[:200]))
    c, h, b = req("/draft/abc", "A", extra=[T3C])
    check("deep T3 link (reload inside T3) -> T3, not dashboard", c == 200 and json.loads(b).get("path") == "/draft/abc", (c, b[:200]))

    # ---- T3's content-hashed build files may be cached by the browser; nothing else is ----
    c, h, b = req("/assets/utils-D8OUUT-k.js", "A", extra=[T3C])
    check("T3 hashed asset -> immutable", c == 200 and hv(h, "cache-control") == ["private, max-age=31536000, immutable"], h)
    c, h, b = req("/assets/main-Ben-HoR5.css?v=1", "A", extra=[T3C])
    check("T3 hashed css with query -> immutable", hv(h, "cache-control") == ["private, max-age=31536000, immutable"], h)
    for path in ("/assets/main.js", "/index.html", "/assets/sub/x-D8OUUT-k.js", "/assets/x-D8OUUT-k.html"):
        c, h, b = req(path, "A", extra=[T3C])
        check(f"not cached forever: {path}", "immutable" not in " ".join(hv(h, "cache-control")), h)
    c, h, b = req("/s/web/assets/utils-D8OUUT-k.js", "A")
    check("friend's app asset not touched", c == 200 and not hv(h, "cache-control"), h)



async def main():
    for p in (31000, 31001, 31011, 31018, 31100):
        serve_http(p)
    ws = await asyncio.start_server(ws_echo, "127.0.0.1", 31002)
    srv = await asyncio.start_server(g.client, "127.0.0.1", GATE, limit=g.MAX_HEAD)
    async with srv, ws:
        await asyncio.to_thread(run_tests)
    print(f"{sum(results)}/{len(results)} passed")


asyncio.run(main())
