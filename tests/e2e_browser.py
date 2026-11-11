"""Browser QA for the public site: a real Chromium drives the REAL sharedbox-gate and sharedbox-web through TLS.

    uv run --no-project --with playwright python tests/e2e_browser.py [REPO] [--shots DIR] [--quick]

Only the root boundary is faked: sharedbox-web's spool_call (what root's provisioner would answer: status, logins,
servers, pairing, requests, invites) and Cloudflare's token check (fake tokens per persona). Everything between the
browser and that boundary is the code that ships: the gate (asyncio proxy, Host/Access checks, routing, T3 front
door), sharedbox-web's page and form handlers on its unix socket, CSRF tokens, Origin checks, redirects.
A TLS front (self-signed, standing in for Cloudflare) serves https://treehouse.test:PORT, mapped to 127.0.0.1 in
the browser, so cookies, Secure flags, Origin and Sec-Fetch-* headers are the browser's own.

Personas: alice (set up), bob (invited, welcome, no AI login), the owner, a stranger (valid login, no account),
no token, and new@x.io who signs up through an invite link. For each it opens every page, clicks every link and
submits every form it finds in the DOM (a few levels deep), and fails on: unexpected 4xx/5xx, "bad origin",
tracebacks, {placeholders}, None/null/undefined, unprotected emails (Cloudflare would rewrite them), buttons in
links, tailnet-only main actions, pages with no way back to the dashboard, console errors, horizontal overflow
on a phone. Screenshots (1280 and 390 wide, dark and light) go to --shots.

Exit 0 when clean, 1 on failures, 77 when no Chromium is available.
"""
import asyncio
import glob
import hashlib
import html as htmlmod
import importlib.machinery
import importlib.util
import json
import os
import pwd
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

args = [a for a in sys.argv[1:]]
QUICK = "--quick" in args
SHOTS = None
if "--shots" in args:
    SHOTS = args[args.index("--shots") + 1]
    del args[args.index("--shots"):args.index("--shots") + 2]
args = [a for a in args if not a.startswith("--")]
WT = os.path.abspath(args[0] if args else os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def free_block(n):
    """A base port with n free ports after it (friends' T3 ports come from PORT_BASE + uid offset)."""
    for base in range(42000, 60000, 97):
        ok = True
        for p in range(base, base + n):
            s = socket.socket()
            try:
                s.bind(("127.0.0.1", p))
            except OSError:
                ok = False
            finally:
                s.close()
            if not ok:
                break
        if ok:
            return base
    raise SystemExit("no free port block")


TMP = tempfile.mkdtemp(prefix="e2e-browser-")
STATE = os.path.join(TMP, "state")
os.makedirs(os.path.join(STATE, "health"))
os.makedirs(os.path.join(STATE, "gate", "invites"))
os.makedirs(os.path.join(STATE, "spool"))
TLS_PORT, GATE_PORT = free_port(), free_port()
PORT_BASE = free_block(40)
OWNER_T3_PORT = PORT_BASE + 35
PUBLIC = f"https://treehouse.test:{TLS_PORT}"
ME = pwd.getpwuid(os.getuid()).pw_name  # the owner's own account must exist on the system
UIDS = {"alice": 2000, "bob": 2001, "carol": 2002}
PORT_OF = {u: PORT_BASE + (uid - 2000) * 10 for u, uid in UIDS.items()}

with open(os.path.join(TMP, "sharedbox.conf"), "w") as f:
    f.write(f"PUBLIC_URL={PUBLIC}\nPUBLIC_MODE=cloudflare\nCF_TEAM_DOMAIN=x.cloudflareaccess.com\n"
            f"CF_ACCESS_AUD=aaaaaaaaaaaaaaaaaaaa\nGATE_BACKEND_HOST=127.0.0.1\nCF_OWNER_EMAILS=owner@x.io\n"
            f"PORT_BASE={PORT_BASE}\nPORT_BLOCK=10\nFRIEND_UID_MIN=2000\nOWNER_T3_PORT={OWNER_T3_PORT}\n"
            f"OWNER_T3_USER={ME}\nADMIN_USERS={ME}\nSHARED_PUBLIC_LINKS=yes\nBRAND=treehouse\n")
sock_dir = tempfile.mkdtemp(prefix="sbw-", dir="/tmp" if len(TMP) > 60 else None)  # AF_UNIX path limit
os.environ.update(SHAREDBOX_CONF=os.path.join(TMP, "sharedbox.conf"), SHAREDBOX_LIB=os.path.join(WT, "lib"),
                  SHAREDBOX_STATE=STATE, SHAREDBOX_WEB_SOCK=os.path.join(sock_dir, "gate.sock"),
                  SHAREDBOX_SHARE=os.path.join(WT, "share"))
sys.path.insert(0, os.path.join(WT, "lib"))


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, os.path.join(WT, path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


g = load("gate", "lib/sharedbox-gate")
w = load("web", "lib/sharedbox-web")
import sbaccess  # noqa: E402
import sbcommon as sb  # noqa: E402

# ---------------- the box's state files (what root and the janitor/health jobs write) ----------------

NOW = int(time.time())


def write(rel, obj):
    with open(os.path.join(STATE, rel), "w") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj))


def save_registry(reg):
    write("users.json", reg)


save_registry({"users": {
    "alice": {"email": "alice@x.io", "status": "active", "created": NOW - 40 * 86400},
    "bob": {"email": "bob@x.io", "status": "active", "created": NOW - 3600, "welcome": True},
}})


def fake_friend_users(reg=None):
    out = {}
    for n, r in (reg or sb.registry()).get("users", {}).items():
        if n in UIDS:
            out[n] = dict(r, uid=UIDS[n], gid=UIDS[n], home=f"/home/{n}")
    return out


sb.friend_users = fake_friend_users  # the only system lookup we replace: these accounts aren't real Linux users

lines = []
cum = {"alice": 0, "bob": 0, ME: 0}
for i in range(7 * 288, 0, -1):
    ts = NOW - i * 300
    users = {}
    for n, load_ in (("alice", 0.4), ("bob", 0.05), (ME, 0.8)):
        cum[n] += int(load_ * (1 + 0.5 * ((i // 12) % 3)) * 300e6)
        users[n] = [cum[n], int((600 + 200 * ((i // 30) % 4)) * 2**20)]
    lines.append(json.dumps({"ts": ts, "cpu": 20 + (i % 40), "load1": 1.5, "mem": 45.0, "disk_free": 61.0,
                             "disk_free_b": 300 * 2**30, "temp": 55, "users": users}))
write("health/metrics.jsonl", "\n".join(lines) + "\n")
write("balance.json", {"cpu_busy": False, "mem_tight": False})
write("usage.json", {"generated": NOW - 600, "users": {
    "alice": {"disk": 3 * 2**30, "budget": 40 * 2**30, "projects": 4, "services": [{"name": "blog", "state": "running"}],
              "pending_deletions": ["old-site"], "archives": [{"name": "toy", "until": NOW + 5 * 86400}]},
    "bob": {"disk": 2**20, "budget": 40 * 2**30, "projects": 1}}})
write("events.jsonl", "\n".join(json.dumps(e) for e in [
    {"ts": NOW - 9000, "kind": "user_created", "user": "alice"},
    {"ts": NOW - 8000, "kind": "cf_access", "user": "alice", "email": "alice@x.io"},
    {"ts": NOW - 7000, "kind": "invite_created", "user": "box", "email": "bob@x.io"},
    {"ts": NOW - 6000, "kind": "shared_restart", "user": "alice", "server": "web"},
    {"ts": NOW - 5000, "kind": "tools_updated"},
    {"ts": NOW - 4000, "kind": "skill_shared", "user": "alice", "skill": "deploy"},
    {"ts": NOW - 3000, "kind": "balance", "cpu_busy": True},
    {"ts": NOW - 2000, "kind": "gate_login", "user": "bob"},
    {"ts": NOW - 1000, "kind": "something_new", "user": "alice", "detail": "x"},
]) + "\n")
write("requests.json", {"requests": [
    {"id": "r-1", "user": "alice", "kind": "install", "packages": ["ffmpeg"], "why": "video bot", "ts": NOW - 3600,
     "state": "waiting", "reason": "not on the auto-install list"},
    {"id": "r-2", "user": "bob", "kind": "other", "text": "Please open port 25\nfor my mail bot.\n\nThanks!", "ts": NOW - 600,
     "state": "new"},
]})
write("shared.json", {s_["name"]: {"port": PORT_BASE + 30 + i, "cmd": ["node", "server.js"]}
                     for i, s_ in enumerate([{"name": "web"}, {"name": "down"}])})
INVITE_TOK = "I" * 43
write(f"gate/invites/{sb.token_hash(INVITE_TOK)}.json",
      {"hash": sb.token_hash(INVITE_TOK), "email": "new@x.io", "expires": NOW + 86400})

# ---------------- fake Cloudflare and the fake root boundary ----------------

TOKENS = {"tok-alice": "alice@x.io", "tok-bob": "bob@x.io", "tok-owner": "owner@x.io",
          "tok-stranger": "nobody@x.io", "tok-new": "new@x.io"}


class FakeVerifier:
    def verify(self, tok):
        if tok not in TOKENS:
            raise sbaccess.InvalidToken("nope")
        return {"email": TOKENS[tok], "exp": 4e9}


g.VERIFIER = FakeVerifier()

ACCOUNTS = {  # what `box _portal status` would say for each account
    "alice": {"logins": {"claude": True, "codex": False, "gh": False, "t3connect": False},
              "servers": [{"name": "blog", "state": "running", "port": str(PORT_OF["alice"] + 3)},
                          {"name": "bot", "state": "stopped", "port": str(PORT_OF["alice"] + 4)}], "ssh": True},
    "bob": {"logins": {"claude": False, "codex": False, "gh": False, "t3connect": False}, "servers": [], "ssh": False},
    "carol": {"logins": {"claude": False, "codex": False, "gh": False, "t3connect": False}, "servers": [], "ssh": False},
    ME: {"logins": {"claude": True, "codex": True, "gh": True, "t3connect": False},
         "servers": [{"name": "site", "state": "running", "port": "8080"}], "ssh": True},
}
LABELS = {"claude": "Claude Code", "codex": "Codex", "gh": "GitHub", "t3connect": "T3 Connect"}
SHARED = [{"name": "web", "state": "running", "port": PORT_BASE + 30, "created_by": "alice", "created": NOW - 86400,
           "updated_by": "bob", "updated": NOW - 600},
          {"name": "down", "state": "stopped", "port": PORT_BASE + 31, "created_by": "bob", "created": NOW - 3600}]
PAIR_TOKENS = set()
ROOT_LOG = []
lock = threading.Lock()


def identity(req):
    email = TOKENS.get(req.get("cf_jwt") or "")
    if not email:
        return None, False
    name, _ = sb.user_by_email(email)
    if name:
        return name, False
    if email in sb.owner_emails(w.CFG) and not sb.email_taken(email):
        return ME, True
    return None, False


def fake_root(req, wait=30):
    """What sharedbox-admin's provisioner answers (re-verifying the Access token, like the real one)."""
    with lock:
        ROOT_LOG.append(dict(req))
        return _fake_root(req)


def _fake_root(req):
    action = req.get("action")
    if action == "join":
        email = TOKENS.get(req.get("cf_jwt") or "")
        inv = sb.gate_lookup("invites", req.get("invite"))[0]
        if not inv or inv.get("email") != email:
            return {"ok": False, "error": "this invite doesn't work any more"}
        if req["username"] in sb.registry()["users"] or req["username"] not in UIDS:
            return {"ok": False, "error": "that username is taken; pick another"}
        reg = sb.registry()
        reg["users"][req["username"]] = {"email": email, "status": "active", "created": NOW, "welcome": True}
        save_registry(reg)
        return {"ok": True}
    name, owner = identity(req)
    if not name:
        return {"ok": False, "relogin": True, "error": "your Cloudflare login couldn't be verified; reload"}
    if action == "owner-pair":
        if not owner:
            return {"ok": False, "error": "not the owner"}
        tok = hashlib.sha256(os.urandom(8)).hexdigest()[:24]
        PAIR_TOKENS.add(tok)
        return {"ok": True, "pair_url": f"http://100.64.0.5:{OWNER_T3_PORT}/pair#token={tok}"}
    if owner and action not in ("status", "shared-ls", "serve-ctl", "serve-logs", "shared-ctl", "shared-logs",
                                "request-approve", "request-done", "request-decline"):
        return {"ok": False, "error": "not available for the owner's account"}
    reg = sb.registry()
    if not owner and (reg["users"].get(name) or {}).pop("welcome", None):
        save_registry(reg)
    acct = ACCOUNTS[name]
    port = OWNER_T3_PORT if owner else PORT_OF[name]
    key = req.get("key")
    if action == "status":
        return {"ok": True, "logins": {k: {"label": LABELS[k], "ok": v} for k, v in acct["logins"].items()},
                "t3": {"url": f"http://100.64.0.5:{port}/", "running": True, "sleeping": False},
                "git": {"name": name, "email": ""}, "ssh": acct["ssh"], "updates": "auto",
                "servers": [dict(s) for s in acct["servers"]]}
    if action == "shared-ls":
        return {"ok": True, "servers": [dict(s) for s in SHARED]}
    if action in ("serve-ctl", "shared-ctl"):
        pool = acct["servers"] if action == "serve-ctl" else SHARED
        s = next((s for s in pool if s["name"] == key), None)
        if not s:
            return {"ok": False, "error": "no such server"}
        op = req.get("op")
        s["state"] = "stopped" if op == "stop" else "running"
        if action == "shared-ctl":
            return {"ok": True, "message": {"start": "started", "stop": "stopped", "restart": "restarted"}[op]
                    + f" {key}", "name": key, "op": op}
        return {"ok": True, "name": key, "op": op, "state": s["state"],
                "message": f"{key}: {'stopped' if op == 'stop' else op + 'ed'}"}
    if action in ("serve-logs", "shared-logs"):
        return {"ok": True, "name": key, "lines": [f"2026-11-11T10:00:0{i}+00:00 box node[12{i}]: line {i} <b>x</b>"
                                                   for i in range(8)]}
    if action.startswith("request-"):
        db = json.load(open(os.path.join(STATE, "requests.json")))
        r = next((r for r in db["requests"] if r["id"] == key), None)
        if not r:
            return {"ok": False, "error": f"no request {key} (see: box friends requests)"}
        op = action[8:]
        db["requests"] = [x for x in db["requests"] if x["id"] != key] if op != "approve" else db["requests"]
        if op == "approve":
            r["state"] = "approved"
        write("requests.json", db)
        if op == "approve":
            return {"ok": True, "id": key, "user": r["user"], "state": "approved",
                    "message": f"approved: installing {' '.join(r.get('packages') or [])} for {r['user']} now; "
                               "their agent is told when it's done"}
        return {"ok": True, "id": key, "user": r["user"], "state": op,
                "message": f"{'declined' if op == 'decline' else 'closed'} {r['user']}'s request; their agent will see it"}
    if action == "t3":
        tok = hashlib.sha256(os.urandom(8)).hexdigest()[:24]
        PAIR_TOKENS.add(tok)
        return {"ok": True, "pair_url": f"http://100.64.0.5:{port}/pair#token={tok}", "url": f"http://100.64.0.5:{port}/"}
    if action == "add-key":
        k, err = sb.validate_pubkey(req.get("sshkey", ""))
        return {"ok": False, "error": err} if err else {"ok": True, "key_added": True}
    if action == "updates":
        return {"ok": True, "updates": key}
    if key not in LABELS:
        return {"ok": False, "error": "unknown login"}
    if action == "login":
        if acct["logins"][key]:
            return {"ok": True, "done": True}
        if key == "claude":
            return {"ok": True, "url": "https://claude.ai/oauth/authorize?code=true", "code": None, "paste": True}
        return {"ok": True, "url": "https://github.com/login/device", "code": "ABCD-1234", "paste": False}
    if action == "code":
        acct["logins"][key] = True
        return {"ok": True, "done": True}
    if action == "check":
        return {"ok": False, "error": f"{LABELS[key]} login hasn't finished: Waiting for you to sign in"}
    return {"ok": False, "error": "unknown action"}


w.spool_call = fake_root

# ---------------- fake T3 Code (one per account port) ----------------


class FakeT3(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body, ctype="text/html", headers=()):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        paired = "t3_session_" in (self.headers.get("Cookie") or "")
        if path == "/pair":
            return self.reply(200, "<!doctype html><title>T3 pairing</title><p id=s>pairing</p><script>"
                              "const t=new URLSearchParams(location.hash.slice(1)).get('token');"
                              "fetch('/api/pair',{method:'POST',body:t}).then(r=>{if(r.ok)location.replace('/');"
                              "else document.getElementById('s').textContent='PAIRING FAILED'})</script>")
        if path == "/":
            if paired:
                return self.reply(200, f"<!doctype html><title>Fake T3 Code</title><h1>T3 on {self.server.server_port}</h1>")
            return self.reply(200, "<!doctype html><title>T3 Code: pair</title><p>Paste a pairing token</p>")
        if path == "/favicon.ico":
            return self.reply(204, "")
        return self.reply(404, '{"error":"not found"}', "application/json")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        tok = self.rfile.read(n).decode()
        if self.path == "/api/pair" and tok in PAIR_TOKENS:
            PAIR_TOKENS.discard(tok)
            return self.reply(200, "{}", "application/json",
                              [("Set-Cookie", f"t3_session_{self.server.server_port}=s; Path=/; HttpOnly; SameSite=Lax")])
        return self.reply(401, '{"error":"bad token"}', "application/json")


class AppServer(BaseHTTPRequestHandler):  # a friend's `box serve` app and the group's server, under /p/ and /s/
    def log_message(self, *a):
        pass

    def do_GET(self):
        data = (f"<!doctype html><title>App {self.server.server_port}</title><a href='/_box/me'>dashboard</a>"
                f"<p>path {htmlmod.escape(self.path)}</p>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(port, handler):
    s = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()


for p in list(PORT_OF.values()) + [OWNER_T3_PORT]:
    serve(p, FakeT3)
for p in (PORT_OF["alice"] + 3, PORT_BASE + 30):  # "blog" (running) and group "web"; "bot"/"down" stay closed
    serve(p, AppServer)

threading.Thread(target=w.serve_gate, daemon=True).start()

# ---------------- TLS front (Cloudflare's edge + cloudflared, minus the rewriting) -> the real gate ----------------

subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=treehouse.test",
                "-keyout", os.path.join(TMP, "k.pem"), "-out", os.path.join(TMP, "c.pem")], check=True,
               capture_output=True)
SSL = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
SSL.load_cert_chain(os.path.join(TMP, "c.pem"), os.path.join(TMP, "k.pem"))


async def tls_front(reader, writer):
    try:
        br, bw = await asyncio.open_connection("127.0.0.1", GATE_PORT)
    except OSError:
        writer.close()
        return

    async def pipe(r, wr):
        try:
            while data := await r.read(65536):
                wr.write(data)
                await wr.drain()
        except (ConnectionError, ssl.SSLError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                wr.close()
            except (RuntimeError, ssl.SSLError):
                pass
    await asyncio.gather(pipe(reader, bw), pipe(br, writer))


LOOP = asyncio.new_event_loop()


def run_loop():
    asyncio.set_event_loop(LOOP)

    async def start():
        await asyncio.start_server(g.client, "127.0.0.1", GATE_PORT, limit=g.MAX_HEAD)
        await asyncio.start_server(tls_front, "127.0.0.1", TLS_PORT, ssl=SSL)
    LOOP.run_until_complete(start())
    LOOP.run_forever()


threading.Thread(target=run_loop, daemon=True).start()
for _ in range(100):
    if os.path.exists(os.environ["SHAREDBOX_WEB_SOCK"]):
        break
    time.sleep(0.05)

# ---------------- the browser ----------------

FAILS, NOTES = [], []
VISITED = set()


def fail(persona, where, what):
    FAILS.append(f"[{persona}] {where}: {what}")
    print(f"FAIL [{persona}] {where}: {what}", flush=True)


def chromium():
    if os.environ.get("CHROMIUM"):
        return os.environ["CHROMIUM"]
    cands = sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome")),
                   key=lambda p: int(re.search(r"chromium-(\d+)", p).group(1)))
    return cands[-1] if cands else None


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
BAD_TEXT = [(re.compile(r"bad origin", re.I), "bad origin"), (re.compile(r"Traceback|Exception|Error:"), "python error"),
            (re.compile(r"\{[a-z_]+\}"), "{placeholder}"),
            (re.compile(r"\b(None|null|undefined|NaN)\b(?! yet)"), "None/null/undefined"),
            (re.compile(r"\[email\s*protected\]"), "Cloudflare email rewrite"),
            (re.compile(r"REQUEST_DENY|OWNER_T3_USER|ADMIN_USERS|sharedbox\.conf|\bsudo\b"), "config jargon")]


def email_outside_email_off(body):
    """Cloudflare's Email Obfuscation rewrites any email address in HTML outside <!--email_off--> blocks."""
    stripped = re.sub(r"<!--email_off-->.*?<!--/email_off-->", "", body, flags=re.S)
    stripped = re.sub(r"<script.*?</script>", "", stripped, flags=re.S)
    return EMAIL.findall(stripped) and [m.group(0) for m in EMAIL.finditer(stripped)]


class Session:
    """One persona in one browser context."""

    def __init__(self, browser, persona, token, cookie=False, width=1280, scheme="dark"):
        self.persona, self.token = persona, token
        extra = {"Cf-Access-Jwt-Assertion": token} if token and not cookie else {}
        self.ctx = browser.new_context(ignore_https_errors=True, extra_http_headers=extra,
                                       viewport={"width": width, "height": 900}, color_scheme=scheme)
        if token and cookie:
            self.ctx.add_cookies([{"name": "CF_Authorization", "value": token, "domain": "treehouse.test",
                                   "path": "/", "secure": True, "httpOnly": True}])
        self.page = self.ctx.new_page()
        self.console, self.bad_responses, self.expect_codes = [], [], set()
        self.ctx.on("page", self._watch)
        self._watch(self.page)

    def _watch(self, page):
        # network failures are judged by status in _resp (some 403/404s are intended), not by the console line
        page.on("console", lambda m: m.type == "error" and not m.text.startswith("Failed to load resource")
                and self.console.append(m.text))
        page.on("pageerror", lambda e: self.console.append(f"pageerror: {e}"))
        page.on("response", self._resp)
        page.on("dialog", lambda d: d.accept())

    def _resp(self, r):
        u = urllib.parse.urlsplit(r.url)
        if u.hostname != "treehouse.test":
            return
        if r.status >= 400 and r.status not in self.expect_codes:
            self.bad_responses.append(f"{r.status} {r.request.method} {u.path}")
        ct = (r.headers.get("content-type") or "")
        if "text/html" in ct and r.status != 304:
            try:
                body = r.text()
            except Exception:  # noqa: BLE001 - body gone after navigation
                return
            leaks = email_outside_email_off(body)
            if leaks:
                self.bad_responses.append(f"email outside email_off on {u.path}: {sorted(set(leaks))}")

    def drain(self, where):
        for c in self.console:
            fail(self.persona, where, f"console error: {c}")
        for b in self.bad_responses:
            fail(self.persona, where, b)
        self.console.clear()
        self.bad_responses.clear()

    def close(self):
        self.ctx.close()


def url(path):
    return PUBLIC + path


def inspect(s, where, page=None, dashboard_link=True):
    """Content checks on whatever page is showing."""
    page = page or s.page
    try:
        page.wait_for_load_state("load", timeout=15000)
        text = page.inner_text("body", timeout=5000)
    except Exception as ex:  # noqa: BLE001
        fail(s.persona, where, f"page didn't load: {ex}")
        return
    for rx, what in BAD_TEXT:
        m = rx.search(text)
        if m:
            ctx = text[max(0, m.start() - 60):m.end() + 60].replace("\n", " ")
            fail(s.persona, where, f"{what}: ...{ctx}...")
    host = urllib.parse.urlsplit(page.url).hostname
    if host != "treehouse.test":
        return
    if page.locator("a button, button a, a a, button button").count():
        fail(s.persona, where, "a button inside a link (dead in some browsers)")
    for el in page.locator("a.btn, button").all():
        href = el.get_attribute("href") or ""
        if href.startswith("http") and urllib.parse.urlsplit(href).hostname not in ("treehouse.test", "t3.codes") \
                and not href.startswith("https://"):
            fail(s.persona, where, f"main action points at a tailnet address: {href}")
    if dashboard_link and not page.locator("a[href='/_box/me'], a[href$='/_box/me']").count():
        fail(s.persona, where, "no way back to the dashboard")
    if page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"):
        fail(s.persona, where, f"horizontal overflow at {page.viewport_size['width']}px")
    VISITED.add((s.persona, urllib.parse.urlsplit(page.url).path))


def links_on(page):
    out = []
    for i, a in enumerate(page.locator("a[href]").all()):
        href = a.get_attribute("href")
        out.append((i, href, (a.inner_text() or "").strip()[:40], a.get_attribute("target")))
    return out


def form_sigs(page):
    """A stable description of each form on the page: (index, action, fields, button label)."""
    out = []
    for i, f in enumerate(page.locator("form").all()):
        vals = {}
        for inp in f.locator("input[type=hidden]").all():
            n = inp.get_attribute("name")
            if n != "token":
                vals[n] = inp.get_attribute("value")
        btn = f.locator("button").first
        label = (btn.inner_text() if btn.count() else "").strip()
        out.append((i, f.get_attribute("action"), tuple(sorted(vals.items())), label))
    return out


SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBfW8kyhn1bfC1CK0D4Z0N8F0rG5uRoHxdQhqC1Vd8Df qa@laptop"


def submit(page, sig):
    """Fill and submit the form matching sig on the current page. Returns False if it isn't there."""
    for i, action, vals, label in form_sigs(page):
        if (action, vals, label) == sig[1:]:
            f = page.locator("form").nth(i)
            page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")  # a person opens it
            for inp in f.locator("input:not([type=hidden]), textarea").all():
                n = inp.get_attribute("name") or ""
                if inp.get_attribute("readonly") is not None:
                    continue
                inp.fill({"sshkey": SSH_KEY, "code": "sk-paste-code-123", "username": "carol"}.get(n, "x"))
            with page.expect_navigation(timeout=30000):
                f.locator("button").first.click()
            return True
    return False


def same_site(href, base):
    full = urllib.parse.urljoin(base, href)
    u = urllib.parse.urlsplit(full)
    return full if u.hostname == "treehouse.test" else None


def crawl(browser, persona, token, start="/_box/me", depth=3, cookie=False, expect=(), dashboard_link=True):
    """Every link and form reachable from `start`, by clicking them (real same-origin navigations)."""
    s = Session(browser, persona, token, cookie=cookie)
    s.expect_codes = set(expect)
    page = s.page
    queue, seen_pages, seen_forms, seen_links = [(start, ())], set(), set(), set()
    while queue:
        path, steps = queue.pop(0)
        key = (path, steps)
        if key in seen_pages or len(steps) > depth:
            continue
        seen_pages.add(key)

        def arrive():
            page.goto(url(path))
            for st in steps:
                if not submit(page, st):
                    return False
            return True
        if not arrive():
            continue
        where = path + "".join(f" > [{st[3]}]" for st in steps)
        print(f"  .. {persona}: {where}", flush=True)
        inspect(s, where, dashboard_link=dashboard_link)
        s.drain(where)
        base = page.url
        for i, href, text, target in links_on(page):
            full = same_site(href, base)
            if not full:
                continue
            p = urllib.parse.urlsplit(full)
            if p.path.startswith("/cdn-cgi/"):
                continue  # Cloudflare's own logout; checked separately
            if (full, target) in seen_links:
                continue  # same link elsewhere: same destination
            seen_links.add((full, target))
            if not arrive():
                break
            lw = f"{where} -> link '{text}' ({href})"
            a = page.locator("a[href]").nth(i)
            try:
                if target == "_blank":
                    with s.ctx.expect_page(timeout=15000) as pi:
                        a.click()
                    np = pi.value
                    np.wait_for_load_state("load")
                    inspect(s, lw, np, dashboard_link=False)
                    np.close()
                else:
                    a.click()
                    page.wait_for_load_state("load")
                    inspect(s, lw, dashboard_link=False)
            except Exception as ex:  # noqa: BLE001
                fail(persona, lw, f"click failed: {str(ex)[:200]}")
            s.drain(lw)
            dest = urllib.parse.urlsplit(page.url)
            if dest.hostname == "treehouse.test" and dest.path.startswith("/_box") and not steps:
                queue.append((dest.path + ("?" + dest.query if dest.query else ""), ()))
        if not arrive():
            continue
        for sig in form_sigs(page):
            fsig = sig[1:]  # the same form on another result page does the same thing: submit each once
            if fsig in seen_forms or (sig[1] or "").endswith("/logout"):
                continue
            seen_forms.add(fsig)
            queue.append((path, steps + (sig,)))
        w.portal_rate.clear()  # the per-person form rate limit (30 / 10 min): the crawl is far busier than a person
        w.rate.clear()
    s.close()
    return seen_pages


def flow_open_t3(browser, persona, token, t3_port, cookie=False):
    """Dashboard -> "Open my T3 Code" -> paired into the right T3; second time straight in; bookmark -> dashboard."""
    s = Session(browser, persona, token, cookie=cookie)
    page = s.page
    page.goto(url("/"))
    if urllib.parse.urlsplit(page.url).path != "/_box/me":
        fail(persona, "open /", f"typed URL should land on the dashboard, got {page.url}")
    btn = page.get_by_role("link", name=re.compile("Open my T3 Code"))
    if not btn.count():
        fail(persona, "dashboard", "no 'Open my T3 Code' button")
        s.close()
        return
    btn.first.click()
    try:
        page.wait_for_function("document.title==='Fake T3 Code'", timeout=15000)
    except Exception:  # noqa: BLE001
        fail(persona, "Open my T3 Code", f"didn't reach T3: {page.url} '{page.title()}' "
             f"{page.inner_text('body')[:200]!r}")
    else:
        if f"T3 on {t3_port}" not in page.inner_text("body"):
            fail(persona, "Open my T3 Code", f"reached the wrong T3: {page.inner_text('body')[:80]}")
    page.goto(url("/_box/me"))
    page.get_by_role("link", name=re.compile("Open my T3 Code")).first.click()
    page.wait_for_load_state("load")
    if page.title() != "Fake T3 Code":
        fail(persona, "Open my T3 Code (already paired)", f"got {page.url} '{page.title()}'")
    s.expect_codes.add(404)
    page.goto(url("/draft/abc"))  # a reload inside T3 stays in T3
    if "not found" not in page.content():
        fail(persona, "reload inside T3", f"should be T3's own answer, got {page.url}")
    page.goto(url("/"))  # bookmark: the dashboard first
    if urllib.parse.urlsplit(page.url).path != "/_box/me":
        fail(persona, "bookmark after pairing", f"expected dashboard, got {page.url}")
    s.drain("open T3 flow")
    s.close()


def flow_login(browser):
    """bob: welcome page, then log in to Claude (paste flow) and GitHub (device code + polling)."""
    s = Session(browser, "bob", "tok-bob")
    page = s.page
    page.goto(url("/"))
    if "welcome=1" not in page.url or "Welcome" not in page.inner_text("body"):
        fail("bob", "first visit", f"expected the welcome page, got {page.url}")
    inspect(s, "welcome")
    page.get_by_role("button", name="Log in with Claude").click()
    page.wait_for_load_state("load")
    inspect(s, "claude login card")
    if not page.locator("a.btn[href^='https://claude.ai']").count():
        fail("bob", "claude login", "no 'Open the login page' link")
    page.fill("input[name=code]", "sk-paste-code-123")
    page.get_by_role("button", name="Finish").click()
    page.wait_for_load_state("load")
    inspect(s, "claude code pasted")
    if "Logged in" not in page.inner_text("body"):
        fail("bob", "claude login", "no 'Logged in' confirmation")
    page.goto(url("/_box/me"))
    gh = page.get_by_role("button", name="Connect GitHub")
    if gh.count():
        gh.click()
        page.wait_for_load_state("load")
        inspect(s, "github device code")
        page.wait_for_timeout(4500)  # the page polls once by itself
        page.get_by_role("button", name="Check now").click()
        page.wait_for_load_state("load")
        inspect(s, "github check now")
    s.drain("login flow")
    s.close()


def flow_invite(browser):
    s = Session(browser, "new@x.io", "tok-new")
    page = s.page
    page.goto(url("/"))
    inspect(s, "stranger landing before signup")
    page.goto(url(f"/i/{INVITE_TOK}"))
    inspect(s, "invite page")
    page.fill("input[name=username]", "carol")
    page.get_by_role("button", name="Create my account").click()
    page.wait_for_load_state("load")
    if "welcome=1" not in page.url:
        fail("new@x.io", "signup", f"expected the welcome page, got {page.url}: {page.inner_text('body')[:200]!r}")
    inspect(s, "after signup")
    s.drain("invite flow")
    s.close()
    flow_open_t3(browser, "carol", "tok-new", PORT_OF["carol"])


def flow_apps(browser):
    s = Session(browser, "alice", "tok-alice")
    page = s.page
    for path, want in ((f"/p/{PORT_OF['alice'] + 3}/", f"App {PORT_OF['alice'] + 3}"), ("/s/web/", f"App {PORT_BASE + 30}")):
        page.goto(url(path))
        if page.title() != want:
            fail("alice", path, f"expected {want}, got '{page.title()}'")
    s.expect_codes |= {502, 403}
    for path in (f"/p/{PORT_OF['alice'] + 4}/", "/s/down/", f"/p/{PORT_OF['bob'] + 3}/"):
        page.goto(url(path))
        inspect(s, path)
    s.drain("apps")
    s.close()


def flow_no_token(browser):
    s = Session(browser, "no token", None)
    s.expect_codes = {403}
    for path in ("/", "/_box/me", "/_box/features", f"/i/{INVITE_TOK}"):
        r = s.page.goto(url(path))
        if r.status != 403:
            fail("no token", path, f"expected 403, got {r.status}")
        inspect(s, path, dashboard_link=False)
    s.drain("no token")
    s.close()


def logout_hrefs(browser):
    for persona, tok in (("alice", "tok-alice"), ("owner", "tok-owner"), ("stranger", "tok-stranger")):
        s = Session(browser, persona, tok)
        s.page.goto(url("/_box/me"))
        hrefs = [a.get_attribute("href") for a in s.page.locator("a").all()
                 if re.search(r"log ?out|sign out", (a.inner_text() or ""), re.I)]
        if not hrefs or any(h != "/cdn-cgi/access/logout" for h in hrefs):
            fail(persona, "log out", f"log out links: {hrefs}")
        s.drain("logout")
        s.close()


def owner_requests(browser):
    s = Session(browser, "owner", "tok-owner")
    page = s.page
    page.goto(url("/_box/me"))
    for label in ("Approve", "Done", "Decline"):
        b = page.locator("#requests ~ div button", has_text=label)
        if not b.count():
            continue
        b.first.click()
        page.wait_for_load_state("load")
        inspect(s, f"request {label}")
        if "✓" not in page.inner_text("body"):
            fail("owner", f"request {label}", page.inner_text("body")[:200])
    s.drain("owner requests")
    s.close()


SHOT_PAGES = {
    "alice": ["/_box/me", "/_box/me?range=7d", "/_box/features"],
    "bob": ["/_box/me?welcome=1", "/_box/me"],
    "owner": ["/_box/me"],
    "stranger": ["/", f"/i/{INVITE_TOK}", "/_box/nope"],
}
SHOT_TOK = {"alice": "tok-alice", "bob": "tok-bob", "owner": "tok-owner", "stranger": "tok-stranger"}


def screenshots(browser):
    os.makedirs(SHOTS, exist_ok=True)
    for persona, paths in SHOT_PAGES.items():
        for width in (1280, 390):
            for scheme in ("dark", "light"):
                s = Session(browser, persona, SHOT_TOK[persona], width=width, scheme=scheme)
                s.expect_codes = {404}
                for p in paths:
                    s.page.goto(url(p))
                    s.page.wait_for_load_state("load")
                    inspect(s, f"{p} @{width} {scheme}")
                    name = re.sub(r"[^a-z0-9]+", "-", p.lower()).strip("-") or "root"
                    s.page.screenshot(path=os.path.join(SHOTS, f"{persona}-{name}-{width}-{scheme}.png"), full_page=True)
                # result cards
                if persona in ("alice", "owner"):
                    s.page.goto(url("/_box/me"))
                    s.page.locator("button", has_text="Logs").first.click()
                    s.page.wait_for_load_state("load")
                    s.page.screenshot(path=os.path.join(SHOTS, f"{persona}-logs-{width}-{scheme}.png"), full_page=True)
                if persona == "alice":
                    for label, nm in (("Get a pairing link for the app", "app-pair"), ("Link my T3 account", "t3connect")):
                        s.page.goto(url("/_box/me"))
                        b = s.page.locator("button", has_text=label)
                        if b.count():
                            b.first.click()
                            s.page.wait_for_load_state("load")
                            s.page.screenshot(path=os.path.join(SHOTS, f"alice-{nm}-{width}-{scheme}.png"), full_page=True)
                s.drain(f"screenshots {width} {scheme}")
                s.close()
                w.portal_rate.clear()


def main():
    exe = chromium()
    if not exe:
        print("SKIP: no Chromium (set CHROMIUM=/path/to/chrome)")
        sys.exit(77)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe, args=["--no-sandbox",
                                    "--host-resolver-rules=MAP treehouse.test 127.0.0.1"])
        flow_no_token(browser)
        if SHOTS:  # first, while the box looks like it does for a new visitor (bob's welcome flag back after)
            screenshots(browser)
            reg = sb.registry()
            reg["users"]["bob"]["welcome"] = True
            save_registry(reg)
            g._users = (0.0, {})
        flow_login(browser)  # bob's first visit must still see the welcome page
        flow_open_t3(browser, "alice", "tok-alice", PORT_OF["alice"])
        flow_open_t3(browser, "alice (cookie)", "tok-alice", PORT_OF["alice"], cookie=True)
        flow_open_t3(browser, "owner", "tok-owner", OWNER_T3_PORT)
        flow_apps(browser)
        flow_invite(browser)
        logout_hrefs(browser)
        owner_requests(browser)
        depth = 1 if QUICK else 3
        crawl(browser, "alice", "tok-alice", depth=depth)
        crawl(browser, "bob", "tok-bob", depth=depth)
        crawl(browser, "owner", "tok-owner", depth=depth)
        crawl(browser, "stranger", "tok-stranger", start="/", depth=depth, expect=(404,))
        crawl(browser, "stranger (cookie)", "tok-stranger", start=f"/i/{INVITE_TOK}", depth=1, cookie=True)
        browser.close()
    print(f"\nvisited {len(VISITED)} (persona, page) pairs; {len(ROOT_LOG)} calls to the fake root")
    if FAILS:
        print(f"{len(FAILS)} problem(s)")
        sys.exit(1)
    print("browser QA: clean")


main()
