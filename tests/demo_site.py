"""A clickable demo of the treehouse dashboards with fake people and data, served over plain HTTP.

    python3 tests/demo_site.py [--host 100.105.248.23] [--port 8796]

It runs the REAL sharedbox-gate and sharedbox-web on a throwaway fake box (tests/fakebox.py: temp state and conf,
fake root answers, fake T3s and machines; the same fake the browser tests use) and puts a small front in front:

- /_demo: pick who to be (Alice set up, Bob just invited, Dave, Erin, the owner, a stranger), make outside
  things happen (a message arrives, an agent describes a server, a machine goes offline), reset everything.
- Every other path goes to the real gate, with that person's fake Cloudflare login added (what Cloudflare Access
  does on the real site). A banner on every page says it's a demo and switches person in one click.
- Plain HTTP on the given address, so a browser needs no certificate or hosts-file change: the front rewrites the
  fake public URL (https://treehouse.demo) to the address the browser used, drops the Secure flag from cookies,
  and points links to the outside world (Claude/GitHub/Google/ChatGPT sign-in, Telegram, the T3 app, other
  machines) at /_demo/outside, which says what would happen and can pretend it did.

State lives in memory (and a temp folder) for as long as the process runs: buttons change what's shown and a
reload keeps it. /_demo/reset puts everything back. Nothing reads or writes the real box's state, conf, spool or
sockets. It refuses to listen on 0.0.0.0: use a Tailscale or loopback address.
"""
import argparse
import html
import http.client
import os
import re
import signal
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
ap.add_argument("--host", default="100.105.248.23", help="address to listen on (never 0.0.0.0)")
ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8796))
ap.add_argument("--repo", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ARGS = ap.parse_args()
if ARGS.host in ("", "0.0.0.0", "::"):
    sys.exit("demo_site: refusing to listen on every address; give --host a Tailscale or loopback address")

FAKE_PUBLIC = "https://treehouse.demo"
os.environ.update(FAKEBOX_PUBLIC=FAKE_PUBLIC, FAKEBOX_REPO=os.path.abspath(ARGS.repo), FAKEBOX_TMP_PREFIX="treehouse-demo-")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fakebox as fb  # noqa: E402  (starts the fake box: real gate + web on loopback/unix sockets)

e = html.escape
COOKIE = "demo_as"
# (key, name, who they are); the fake Cloudflare token is "tok-<key>"
PERSONAS = [
    ("alice", "Alice", "Set-up friend: servers, messages, two machines, a teammate. Telegram not connected yet."),
    ("bob", "Bob", "Just invited, nothing set up: the welcome page the first time, then a sign-in to an AI first."),
    ("dave", "Dave", "Another friend: signed in with ChatGPT, joined without Tailscale."),
    ("erin", "Erin", "Another friend: T3 Code isn't installed for her yet."),
    ("owner", "Owner", "The machine's owner: the friend tabs plus Friends, All running, Requests, Settings."),
    ("stranger", "Stranger", "Signed in to Cloudflare but has no account on this machine."),
]
NAMES = {k: n for k, n, _ in PERSONAS}


def user_of(persona):
    return fb.ME if persona == "owner" else persona


# ---------------- the demo's own data (on top of the e2e fake box), re-applied after every reset ----------------

def demo_extras():
    A, M = fb.ACCOUNTS, fb.M
    fb.PEOPLE[:] = ["alice", "bob", "dave", "erin", fb.ME]
    # the e2e data has a few test-only oddities (an HTML-escaping check, a name-length check) that look like bugs here
    fb.SHARED[0]["desc"] = "Discord bot that posts the week's hackathon schedule every Monday"
    for items in fb.RUNNING.values():
        for i in items:
            if i["name"].startswith("legacy-siege-mongo"):
                i["name"] = "class-project-mongo-with-a-really-long-container-name"
    fb.WHERE["dave"] = {"status": "Working from the library", "act": "active 20m ago"}
    fb.WHERE["erin"] = {"act": "active yesterday"}
    shop = fb.PORT_OF["alice"] + 5
    A["alice"]["servers"] += [
        {"name": "shop", "state": "running", "port": str(shop), "importance": "safe-to-stop", "by": "alice",
         "agent": "Codex", "created": "2026-11-25 09:40", "exec": "/usr/bin/node server.js"},
        {"name": "scraper", "state": "failed", "port": str(fb.PORT_OF["alice"] + 6),
         "about": "Checks the course page for new marks every hour", "by": "alice", "agent": "Claude Code",
         "exec": "/usr/bin/node scrape.js"}]
    fb.RUNNING["alice"].append({"key": "u:sb-shop.service", "kind": "server", "name": "shop", "detail": "node",
                                "mem": 95 * M, "age": 3 * 86400, "pids": 2, "stop": "server", "cpu_usec": 0,
                                "exec": "/usr/bin/node server.js"})
    fb.RUNNING["alice"].sort(key=lambda i: (i["kind"] == "background", -i["mem"]))
    A["dave"]["servers"] = [{"name": "notes-api", "state": "running", "port": str(fb.PORT_OF["dave"] + 3),
                             "by": "dave", "agent": "Codex", "exec": "/usr/bin/python3 -m uvicorn app:app"}]
    fb.RUNNING["dave"] = [
        {"key": "", "kind": "agent", "name": "Codex session", "detail": "codex, node", "mem": 640 * M, "age": 2400,
         "pids": 6, "stop": None, "cpu_usec": 0},
        {"key": "u:sb-notes-api.service", "kind": "server", "name": "notes-api", "detail": "python3", "mem": 80 * M,
         "age": 86400, "pids": 1, "stop": "server", "cpu_usec": 0, "exec": "/usr/bin/python3 -m uvicorn app:app"}]
    now = int(time.time())
    fb.MSGS["bob"] = [{"id": "18f0aa000021", "from": fb.ME, "to": "bob", "title": "Welcome to the box",
                       "level": "normal", "ts": now - 1800, "read": False,
                       "context": "Sign in to Claude on your dashboard, then open T3 Code. Ask me if anything is stuck."}]
    fb.MSGS["dave"] = [{"id": "18f0aa000022", "from": "alice", "to": "dave", "title": "Can your notes API take tags?",
                        "level": "low", "ts": now - 7200, "read": False,
                        "context": "My blog wants to pull notes by tag. No rush, sometime this week."}]
    fb.MSGS[fb.ME] = [{"id": "18f0aa000023", "from": "alice", "to": fb.ME, "title": "ffmpeg for my video bot?",
                       "level": "normal", "ts": now - 3000, "read": False,
                       "context": "I asked for it in Requests too. It's for the hackathon demo video."}]


fb.RESET_HOOKS.append(demo_extras)
fb.w.HOST = fb.BOX_NAME = "treebox"  # this machine's name everywhere (the Machines tab calls it that), not the real host's
fb.serve(fb.PORT_OF["alice"] + 5, fb.AppServer)  # "shop" answers under /p/
fb.serve(fb.PORT_OF["dave"] + 3, fb.AppServer)   # "notes-api"
PEER_DOWN = [False]
_peer_url = fb.w.peer_url
fb.w.peer_url = lambda ip: (f"http://127.0.0.1:{fb.free_port()}/api/box" if PEER_DOWN[0] and ip == "100.64.0.6"
                            else _peer_url(ip))


def reset_demo():
    with fb.lock:
        PEER_DOWN[0] = False
        fb.reset()
    fb.refresh_peers()


reset_demo()


def peers_timer():  # a real web asks the other machines on a timer; so "last seen" stays fresh
    while True:
        time.sleep(60)
        try:
            fb.refresh_peers()
        except Exception as ex:  # noqa: BLE001 - keep the timer alive
            print("demo_site: peers:", ex, file=sys.stderr, flush=True)


threading.Thread(target=peers_timer, daemon=True).start()

# ---------------- things that happen outside the dashboard ----------------


def undescribed():
    return [(u, s["name"]) for u in ("alice", "dave", fb.ME) for s in fb.ACCOUNTS[u]["servers"] if not s.get("about")]


DESCRIPTIONS = {"bot": "Discord bot that answers questions about the course schedule",
                "shop": "Practice online shop for the web dev class, with fake products",
                "notes-api": "Small API that serves Dave's study notes to his other apps"}


def act(op, arg):
    """A demo control: returns the sentence shown afterwards."""
    with fb.lock:
        now = int(time.time())
        if op == "message":
            fb._msg_seq[0] += 1
            fb.MSGS.setdefault("alice", []).insert(0, {
                "id": f"{fb._msg_seq[0]:x}", "from": "bob", "to": "alice", "title": "Lunch after the demo?",
                "level": "normal", "ts": now, "read": False, "context": "We could go to the place on College St."})
            return "Bob sent Alice a message. Continue as Alice and open Messages."
        if op == "describe":
            user, _, name = arg.partition("/")
            for s in (fb.ACCOUNTS.get(user) or {}).get("servers", []):
                if s["name"] == name and not s.get("about"):
                    s["about"] = DESCRIPTIONS.get(name, f"What {name} does, written by the agent that started it")
                    for i in fb.RUNNING.get(user, []):
                        if i["key"] == f"u:sb-{name}.service":
                            i["about"] = s["about"]
                    return f"{NAMES.get(user, user)}'s agent described {name}. Its card on the Servers tab shows it now."
            return "That server already has a description."
        if op == "request":
            db = fb.sb.read_json(os.path.join(fb.STATE, "requests.json"), {}) or {"requests": []}
            db["requests"].append({"id": f"r-{now}", "user": "alice", "kind": "install", "packages": ["imagemagick"],
                                   "why": "resize photos for the blog", "ts": now, "state": "waiting",
                                   "reason": "not on the auto-install list"})
            fb.write("requests.json", db)
            return "Alice's agent asked for imagemagick. Continue as the owner and open Requests."
        if op == "crash":
            for s in fb.ACCOUNTS["alice"]["servers"]:
                if s["name"] == "shop":
                    s["state"] = "failed"
                    fb._sync_running("alice", s)
                    return "Alice's shop server crashed. Her Home and Servers tabs say so."
            return "Alice has no shop server right now."
        if op == "peer":
            PEER_DOWN[0] = not PEER_DOWN[0]
    if op == "peer":
        fb.refresh_peers()
        return f"minastirith is {'offline' if PEER_DOWN[0] else 'back online'}. See the Machines tab."
    return "Nothing happened (unknown control)."


# ---------------- pages the front serves itself ----------------

STYLE = """
:root{--bg:#fafafa;--fg:#1a1a1a;--mut:#666;--card:#fff;--line:#e3e3e3;--acc:#2563eb;--bar:#fff4cc;--barfg:#5c4400}
@media (prefers-color-scheme: dark){:root{--bg:#111;--fg:#eee;--mut:#9a9a9a;--card:#1b1b1b;--line:#2c2c2c;
--acc:#6b9bff;--bar:#3a2f0b;--barfg:#f3dd9b}}
"""
BAR_CSS = """
#demo-bar{font:13px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--demo-bar,#fff4cc);
color:var(--demo-barfg,#5c4400);border-bottom:1px solid rgba(0,0,0,.08);padding:6px 16px;display:flex;flex-wrap:wrap;
gap:2px 14px;align-items:baseline;position:relative;z-index:50}
#demo-bar a{color:inherit;text-decoration:underline;text-underline-offset:2px}
#demo-bar .who a{margin-right:8px}#demo-bar .who a[aria-current]{font-weight:600;text-decoration:none}
#demo-bar b{font-weight:600}
@media (prefers-color-scheme: dark){#demo-bar{--demo-bar:#3a2f0b;--demo-barfg:#f3dd9b;border-color:rgba(255,255,255,.08)}}
"""


def bar(persona, here):
    nxt = urllib.parse.quote(here if here.startswith("/_box/me") else "", safe="")
    who = "".join(f"<a href='/_demo/as/{k}?next={nxt}'{' aria-current=\"true\"' if k == persona else ''}>{n}</a>"
                  for k, n, _ in PERSONAS)
    return (f"<style>{BAR_CSS}</style><div id='demo-bar' role='note'><span><b>Demo: fake data, nothing here is "
            f"real.</b></span><span class='who'>Viewing as: {who}</span>"
            f"<span><a href='/_demo'>Demo controls</a> · <a href='/_demo/reset?next={nxt}'>Reset demo</a></span></div>")


def shell(title, body, persona=None):
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title>
<style>{STYLE}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:760px;margin:0 auto;padding:24px 16px 48px}}h1{{font-size:22px;margin:8px 0 4px}}
h2{{font-size:16px;margin:28px 0 8px}}p{{margin:6px 0}}.mut{{color:var(--mut)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px}}
ul.people{{list-style:none;margin:12px 0 0;padding:0}}ul.people li{{display:flex;gap:12px;align-items:center;
justify-content:space-between;padding:12px 14px;border-top:1px solid var(--line)}}ul.people li:first-child{{border-top:0}}
ul.people .d{{color:var(--mut);font-size:13px}}a.btn,button{{display:inline-block;white-space:nowrap;
background:var(--acc);color:#fff;border:0;border-radius:7px;padding:7px 12px;font:inherit;font-size:14px;
text-decoration:none;cursor:pointer}}button.sec,a.btn.sec{{background:transparent;color:var(--fg);
border:1px solid var(--line)}}form{{display:inline}}.acts{{display:flex;flex-wrap:wrap;gap:8px}}
.note{{padding:10px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);margin:12px 0}}
code{{font-size:13px;background:var(--card);border:1px solid var(--line);border-radius:5px;padding:1px 5px;word-break:break-all}}
a{{color:var(--acc)}}:focus-visible{{outline:2px solid var(--acc);outline-offset:2px}}
@media (max-width:520px){{ul.people li{{flex-direction:column;align-items:flex-start}}}}
</style></head><body>{bar(persona, "") if persona else
        "<style>" + BAR_CSS + "</style><div id='demo-bar' role='note'><b>Demo: fake data, nothing here is real.</b></div>"}
<main>{body}</main></body></html>"""


def picker(persona, msg=""):
    rows = "".join(
        f"<li><div><b>{n}</b><div class='d'>{e(d)}</div></div>"
        f"<a class='btn{' sec' if k == persona else ''}' href='/_demo/as/{k}'>Continue as {n}</a></li>"
        for k, n, d in PERSONAS)
    ctl = [("message", "", "Bob sends Alice a message")]
    ctl += [("describe", f"{u}/{s}", f"{NAMES.get(u, 'The owner') if u != fb.ME else 'The owner'}'s agent describes "
             f"the server {s}") for u, s in undescribed()]
    ctl += [("request", "", "Alice's agent asks the owner to install a package"),
            ("crash", "", "Alice's shop server crashes"),
            ("peer", "", "minastirith comes back online" if PEER_DOWN[0] else "The machine minastirith goes offline")]
    btns = "".join(f"<form method='post' action='/_demo/act'><input type='hidden' name='op' value='{op}'>"
                   f"<input type='hidden' name='arg' value='{e(arg)}'><button class='sec'>{e(label)}</button></form>"
                   for op, arg, label in ctl)
    return shell("treehouse demo", f"""<h1>treehouse demo</h1>
<p class="mut">A working copy of the treehouse dashboard with made-up people, machines and servers. Buttons really
change what you see (until you reset), but nothing touches a real machine or account.</p>
{f'<div class="note" role="status">{e(msg)}</div>' if msg else ''}
<h2>Who do you want to be?</h2>
<ul class="people card">{rows}</ul>
<h2>Make something happen</h2>
<p class="mut">Things that happen away from the dashboard on a real machine: someone's agent, another person, a
crash. Press one, then look at the dashboard.</p>
<div class="acts">{btns}</div>
<h2>Start over</h2>
<p><a class="btn sec" href="/_demo/reset">Reset demo</a> <span class="mut">Puts every person, server and message
back the way it started.</span></p>
<h2>What's pretend</h2>
<ul class="mut">
<li>Signing in to Claude, ChatGPT, GitHub or Google, connecting Telegram and the T3 app open a page that explains
what would happen, with a button to pretend it worked.</li>
<li>"Open T3" opens a stand-in page instead of T3 Code. Server links open a stand-in page too.</li>
<li>Emails and Telegram messages aren't sent. Memory and CPU numbers are made up, except this machine's own.</li>
<li>Everyone shares one demo: a change made as Alice is there when you come back as Alice.</li>
</ul>""", persona)


OUTSIDE = [  # (url prefix, what it is, sign-in key or None, how it finishes)
    ("https://claude.ai/", "Claude's sign-in page", "claude", "paste"),
    ("https://accounts.google.com/", "Google's sign-in page (for Antigravity)", "antigravity", "paste"),
    ("https://auth.openai.com/", "ChatGPT's sign-in page (for Codex)", "codex", "device"),
    ("https://github.com/login/device", "GitHub's sign-in page", "gh", "device"),
    ("https://accounts.t3.codes/", "T3's page that links the T3 app on your phone or laptop", "t3connect", "device"),
    ("https://t.me/", "Telegram, to connect your phone for alerts", "telegram", "telegram"),
    ("t3code://", "the T3 Code app on your laptop, already paired with this machine", None, None),
    ("http://100.64.0.6:8090", "the dashboard of the machine minastirith", None, None),
    (f"http://{fb.TS_IP}:", "this address on the private Tailscale network (it only works on a real machine)", None, None),
]


def outside(persona, to, msg=""):
    what, key, how = next(((w, k, h) for p, w, k, h in OUTSIDE if to.startswith(p)),
                          (f"the website {urllib.parse.urlsplit(to).netloc or to}", None, None))
    body = [f"<h1>In the demo this would open {e(what)}</h1>",
            f"<p class='mut'>The real link: <code>{e(to)}</code></p>"]
    if msg:
        body.append(f"<div class='note' role='status'>{e(msg)}</div>")
    if how == "paste":
        body.append("<p>A real sign-in ends with a code to copy back into the dashboard. Here any text works, "
                    "for example <code>demo-code-1234</code>: paste it into the box on the dashboard and press "
                    "Finish.</p>")
    elif how in ("device", "telegram"):
        verb = "pressed Start in Telegram" if how == "telegram" else "typed the code and signed in"
        body.append(f"<p>On a real machine you'd do that here, and the dashboard notices by itself within a few "
                    f"seconds.</p><form method='post' action='/_demo/finish'><input type='hidden' name='key' "
                    f"value='{e(key)}'><input type='hidden' name='to' value='{e(to)}'>"
                    f"<button>Pretend I {verb}</button></form>")
    body.append("<p style='margin-top:20px'><a href='/_box/me'>Back to the dashboard</a> <span class='mut'>(or close "
                "this tab if the dashboard is still open in another one)</span></p>")
    return shell("Outside the demo", "".join(body), persona)


# ---------------- the front: demo pages, else the real gate with a fake Cloudflare login ----------------

HREF = re.compile(r"""href=(['"])((?:https?|t3code)://[^'"]*)\1""")
SKIP_REQ = {"host", "connection", "keep-alive", "proxy-connection", "proxy-authorization", "te", "upgrade",
            "content-length", "accept-encoding", "cookie", "transfer-encoding"}
SKIP_RESP = {"connection", "keep-alive", "transfer-encoding", "content-length"}


class Front(BaseHTTPRequestHandler):
    server_version = "treehouse-demo"

    def log_message(self, fmt, *a):
        pass

    def do_GET(self):
        self.route()

    def do_HEAD(self):
        self.route()

    def do_POST(self):
        self.route()

    # -- helpers --
    def origin(self):
        return "http://" + (self.headers.get("Host") or f"{ARGS.host}:{ARGS.port}")

    def persona(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and v in NAMES:
                return v
        return None

    def form(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 <= n <= 65536:
            return {}
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode(errors="replace")).items()}

    def send(self, code, body=b"", ctype="text/html; charset=utf-8", headers=()):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def redirect(self, where, headers=()):
        self.send(303, "", None, [("Location", where), *headers])

    def default_next(self, persona):
        if persona == "stranger":
            return "/"
        if persona == "bob" and (fb.sb.registry()["users"].get("bob") or {}).get("welcome"):
            return "/_box/me?welcome=1"
        return "/_box/me"

    # -- routing --
    def route(self):
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        persona = self.persona()
        nxt = q.get("next", "")
        if not (nxt.startswith("/") and not nxt.startswith("//") and not nxt.startswith("/_demo")):
            nxt = ""
        if u.path in ("/_demo", "/_demo/"):
            return self.send(200, picker(persona, q.get("msg", "")))
        if u.path.startswith("/_demo/as/"):
            p = u.path[len("/_demo/as/"):]
            if p not in NAMES:
                return self.redirect("/_demo")
            if p == "stranger" or (nxt and "welcome=1" in nxt):
                nxt = ""
            return self.redirect(nxt or self.default_next(p),
                                 [("Set-Cookie", f"{COOKIE}={p}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax")])
        if u.path == "/_demo/reset":
            reset_demo()
            if persona and nxt:
                return self.redirect(nxt)
            return self.redirect("/_demo?msg=" + urllib.parse.quote("Demo reset: everything is back the way it started."))
        if u.path == "/_demo/act" and self.command == "POST":
            f = self.form()
            return self.redirect("/_demo?msg=" + urllib.parse.quote(act(f.get("op", ""), f.get("arg", ""))))
        if u.path == "/_demo/outside":
            return self.send(200, outside(persona, q.get("to", ""), q.get("msg", "")))
        if u.path == "/_demo/finish" and self.command == "POST":
            f = self.form()
            key, to = f.get("key", ""), f.get("to", "")
            if persona and key in ("claude", "antigravity", "codex", "gh", "t3connect", "telegram"):
                with fb.lock:
                    fb.FINISHED.add((user_of(persona), key))
                msg = ("Done. Back on the dashboard Telegram shows as connected within a few seconds."
                       if key == "telegram" else "Done. Back on the dashboard the sign-in finishes within a few seconds.")
            else:
                msg = "Pick a person on the demo page first."
            return self.redirect("/_demo/outside?" + urllib.parse.urlencode({"to": to, "msg": msg}))
        if u.path.startswith("/_demo"):
            return self.redirect("/_demo")
        if u.path == "/cdn-cgi/access/logout":  # Cloudflare's sign-out: back to picking a person
            return self.redirect("/_demo", [("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")])
        if not persona:
            return self.redirect("/_demo")
        return self.forward(persona)

    def forward(self, persona):
        origin = self.origin()
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if 0 < n <= 1 << 20 else b""
        hdrs = []
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in SKIP_REQ or kl.startswith(("cf-", "x-forwarded-", "x-sharedbox-")):
                continue
            if kl == "origin" and v == origin:
                v = FAKE_PUBLIC
            elif kl == "referer" and v.startswith(origin):
                v = FAKE_PUBLIC + v[len(origin):]
            hdrs.append((k, v))
        cookies = "; ".join(p.strip() for p in (self.headers.get("Cookie") or "").split(";")
                            if p.strip() and p.strip().partition("=")[0] != COOKIE)
        if cookies:
            hdrs.append(("Cookie", cookies))
        hdrs += [("Host", fb.PUBLIC_HOST), ("Cf-Access-Jwt-Assertion", "tok-" + persona),
                 ("X-Forwarded-For", self.client_address[0])]
        if body or self.command == "POST":
            hdrs.append(("Content-Length", str(len(body))))
        fb.w.portal_rate.clear()  # one person clicking around a demo is busier than the per-person form limit expects
        try:
            conn = http.client.HTTPConnection("127.0.0.1", fb.GATE_PORT, timeout=300)
            conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
            for k, v in hdrs:
                conn.putheader(k, v)
            conn.endheaders(body or None)
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
        except OSError as ex:
            return self.send(502, shell("Demo error", f"<h1>The demo's machine didn't answer</h1><p>{e(str(ex))}</p>"))
        out = []
        for k, v in resp.getheaders():
            kl = k.lower()
            if kl in SKIP_RESP:
                continue
            if kl == "location":
                v = v.replace(FAKE_PUBLIC, origin)
            elif kl == "set-cookie":
                v = re.sub(r";\s*Secure", "", v, flags=re.I)  # plain HTTP: a Secure cookie would never come back
            out.append((k, v))
        ctype = resp.getheader("Content-Type") or ""
        if ctype.startswith(("text/html", "text/plain", "text/markdown", "application/json")):
            text = data.decode("utf-8", errors="replace").replace(FAKE_PUBLIC, origin)
            text = text.replace(fb.PUBLIC_HOST, urllib.parse.urlsplit(origin).netloc)
            if ctype.startswith("text/html"):
                text = HREF.sub(lambda m: m.group(0) if m.group(2).startswith(origin) else
                                "href=" + m.group(1) + "/_demo/outside?to="
                                + urllib.parse.quote(html.unescape(m.group(2)), safe="") + m.group(1), text)
                here = urllib.parse.urlsplit(self.path)
                banner = bar(persona, here.path + ("?" + here.query if here.query else ""))
                m = re.search(r"<body[^>]*>", text, re.I)
                text = text[:m.end()] + banner + text[m.end():] if m else banner + text
            data = text.encode()
        self.send_response(resp.status)
        for k, v in out:
            if k.lower() != "content-type":
                self.send_header(k, v)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))  # `box serve stop`, tests: clean up the temp box
    srv = Server((ARGS.host, ARGS.port), Front)
    print(f"treehouse demo on http://{ARGS.host}:{srv.server_port}/_demo (fake box in {fb.TMP})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


main()
