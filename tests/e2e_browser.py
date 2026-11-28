"""Browser QA for the public site: a real Chromium drives the REAL sharedbox-gate and sharedbox-web through TLS.

    uv run --no-project --with playwright python tests/e2e_browser.py [REPO] [--shots DIR] [--quick | --shots-only]

How much to run: a look-only change (layout, CSS, copy) needs just `--shots DIR --shots-only` (every page as
each persona, 1920, 1280 and 390 wide, dark and light, plus the page checks below; about two minutes). `--quick` clicks
one level deep. The full run (several minutes) is for changes to links, buttons, forms or flows, before a
deploy, in the background.

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
on a phone. Screenshots (1920, 1280 and 390 wide, dark and light) go to --shots.

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
SHOTS_ONLY = "--shots-only" in args
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
PORT_BASE = free_block(80)
OWNER_T3_PORT = PORT_BASE + 75
SHARED_BASE = PORT_BASE + 60
TS_IP = "100.64.0.5"  # the box's Tailscale IP in every address a friend sees (never its short name)
PUBLIC = f"https://treehouse.test:{TLS_PORT}"
ME = pwd.getpwuid(os.getuid()).pw_name  # the owner's own account must exist on the system
UIDS = {"alice": 2000, "bob": 2001, "carol": 2002, "dave": 2003, "erin": 2004}
PORT_OF = {u: PORT_BASE + (uid - 2000) * 10 for u, uid in UIDS.items()}

with open(os.path.join(TMP, "sharedbox.conf"), "w") as f:
    f.write(f"PUBLIC_URL={PUBLIC}\nPUBLIC_MODE=cloudflare\nCF_TEAM_DOMAIN=x.cloudflareaccess.com\n"
            f"CF_ACCESS_AUD=aaaaaaaaaaaaaaaaaaaa\nGATE_BACKEND_HOST=127.0.0.1\nCF_OWNER_EMAILS=owner@x.io\n"
            f"PORT_BASE={PORT_BASE}\nPORT_BLOCK=10\nFRIEND_UID_MIN=2000\nOWNER_T3_PORT={OWNER_T3_PORT}\n"
            f"OWNER_T3_USER={ME}\nADMIN_USERS={ME}\nSHARED_PUBLIC_LINKS=yes\nBRAND=treehouse\n"
            "UPDATE_SOURCE=https://github.com/example/treehouse\n"
            "SSH_HOST=treehouse-ssh.test.io\nCF_SSH_APP_ID=sshapp12345\n"
            f"BOXES=treebox={TS_IP} minastirith=100.64.0.6 krishbox=100.64.0.7\n")
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
    "alice": {"email": "alice@x.io", "status": "active", "created": NOW - 40 * 86400, "ts_id": "nAlice1"},
    "bob": {"email": "bob@x.io", "status": "active", "created": NOW - 3600, "welcome": True, "ts_id": "nBob1"},
    "dave": {"email": "dave@x.io", "status": "active", "created": NOW - 9 * 86400},  # joined through Cloudflare only
    "erin": {"email": "erin@x.io", "status": "active", "created": NOW - 2 * 86400, "ts_id": "nErin1"},
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
write("update.json", {"state": "done", "finished": NOW - 7200, "commit": "abc1234", "subject": "Fix things"})
write("usage.json", {"generated": NOW - 600, "users": {
    "alice": {"disk": 3 * 2**30, "budget": 40 * 2**30, "projects": 4, "services": [{"name": "blog", "state": "running"}],
              "pending_deletions": ["old-site"], "pending": [{"name": "old-site", "on": NOW + 3 * 86400}], "archives": [{"name": "toy", "until": NOW + 5 * 86400}]},
    "bob": {"disk": 2**20, "budget": 40 * 2**30, "projects": 1}}})
write("events.jsonl", "\n".join(json.dumps(e) for e in [
    {"ts": NOW - 9000, "kind": "user_created", "user": "alice"},
    {"ts": NOW - 8000, "kind": "cf_access", "user": "alice", "email": "alice@x.io"},
    {"ts": NOW - 7000, "kind": "invite_created", "user": "box", "email": "bob@x.io"},
    {"ts": NOW - 6000, "kind": "shared_restart", "user": "alice", "server": "web"},
    {"ts": NOW - 5000, "kind": "tools_updated"},
    {"ts": NOW - 4000, "kind": "skill_shared", "user": "alice", "skill": "deploy"},
    {"ts": NOW - 3000, "kind": "balance", "cpu_busy": True},
    {"ts": NOW - 2000, "kind": "owner_paired", "user": "bob"},
    {"ts": NOW - 1000, "kind": "something_new", "user": "alice", "detail": "x"},
]) + "\n")
write("requests.json", {"requests": [
    {"id": "r-1", "user": "alice", "kind": "install", "packages": ["ffmpeg"], "why": "video bot", "ts": NOW - 3600,
     "state": "waiting", "reason": "not on the auto-install list"},
    {"id": "r-2", "user": "bob", "kind": "other", "text": "Please open port 25\nfor my mail bot.\n\nThanks!", "ts": NOW - 600,
     "state": "new"},
]})
write("shared.json", {s_["name"]: {"port": SHARED_BASE + i, "cmd": ["node", "server.js"]}
                     for i, s_ in enumerate([{"name": "web"}, {"name": "down"}])})
INVITE_TOK = "I" * 43
write(f"gate/invites/{sb.token_hash(INVITE_TOK)}.json",
      {"hash": sb.token_hash(INVITE_TOK), "email": "new@x.io", "expires": NOW + 86400})

# ---------------- fake Cloudflare and the fake root boundary ----------------

TOKENS = {"tok-alice": "alice@x.io", "tok-bob": "bob@x.io", "tok-owner": "owner@x.io",
          "tok-stranger": "nobody@x.io", "tok-new": "new@x.io", "tok-dave": "dave@x.io", "tok-erin": "erin@x.io"}


class FakeVerifier:
    def verify(self, tok):
        if tok not in TOKENS:
            raise sbaccess.InvalidToken("nope")
        return {"email": TOKENS[tok], "exp": 4e9}


g.VERIFIER = FakeVerifier()

ACCOUNTS = {  # what `box _portal status` would say for each account
    "alice": {"boxes": {"minastirith": {"ssh": True, "logins": {"claude": True, "gh": True, "t3connect": False,
                                                                 "codex": False, "antigravity": False},
                                        "accounts": {"claude": "alice.work@example.com"}, "claude_own": True},
                        "krishbox": {"ssh": False, "logins": {}}},
              "accounts": {"claude": "alice@example.com", "antigravity": "alice@gmail.example"},
              "logins": {"claude": True, "codex": False, "gh": False, "t3connect": False, "antigravity": True},
              "servers": [{"name": "blog", "state": "running", "port": str(PORT_OF["alice"] + 3),
                           "about": "Alice's blog about the hackathon, read by the whole class", "keep": True,
                           "importance": "important", "project": "blog", "by": "alice", "agent": "Claude Code",
                           "created": "2026-11-20 14:02"},
                          {"name": "bot", "state": "stopped", "port": str(PORT_OF["alice"] + 4),
                           "exec": "/usr/bin/python3 bot.py"}], "ssh": True},
    "bob": {"logins": {"claude": False, "codex": False, "gh": False, "t3connect": False, "antigravity": False},
            "servers": [], "ssh": False},
    "carol": {"logins": {"claude": False, "codex": False, "gh": False, "t3connect": False}, "servers": [], "ssh": False},
    "dave": {"logins": {"claude": False, "codex": True, "gh": True, "t3connect": False}, "servers": [], "ssh": False,
             "boxes": {"minastirith": {"ssh": True, "logins": {"codex": False}}}},  # ChatGPT here, not there yet
    "erin": {"logins": {"claude": True, "codex": False, "gh": False, "t3connect": False}, "servers": [], "ssh": False,
             "no_t3": True},  # T3 Code isn't installed for her: the start card says so
    ME: {"logins": {"claude": True, "codex": False, "gh": True, "t3connect": False, "antigravity": False},
         "accounts": {"claude": "owner@x.io", "gh": "owner-gh"},
         "boxes": {"minastirith": {"ssh": True, "logins": {"claude": True, "codex": True, "antigravity": False},
                                   "accounts": {"claude": "owner@x.io", "codex": "owner.chatgpt@x.io"}},
                   "krishbox": {"ssh": True, "logins": {"claude": False, "codex": False, "antigravity": False}}},
         "servers": [{"name": "site", "state": "running", "port": "8080", "about": "The owner's portfolio site",
                      "importance": "safe-to-stop", "hackathon": "pause", "by": ME, "agent": "Codex"}], "ssh": True},
}
PROJECTS = {  # what `box _portal status` says under "projects" (lib/sbprojects.py summary), per account
    "alice": {"ok": True, "every": 1800, "last_sync": NOW - 300, "missing": [],
              "boxes": {"minastirith": {"last_sync": NOW - 300, "error": ""},
                        "krishbox": {"last_sync": NOW - 300, "error": "Not connected yet. Your boxes connect to each other by themselves within a few minutes; if this stays for a day, the owner can fix it."}},
              "projects": [
                  {"name": "blog", "url": "https://github.com/alice/blog.git", "available": True, "why": "",
                   "everywhere": True, "boxes": {"minastirith": {"state": "current", "note": "", "t3": True},
                                                 "krishbox": {"state": "waiting", "note": ""}}},
                  {"name": "game-jam-2026-final-version", "url": "git@github.com:alice/game-jam.git", "available": True,
                   "why": "", "everywhere": True,
                   "boxes": {"minastirith": {"state": "skipped", "note": "Has uncommitted changes there; not updated.",
                                             "t3": True}, "krishbox": {"state": "waiting", "note": ""}}},
                  {"name": "notes", "url": "https://github.com/alice/notes", "available": True, "why": "",
                   "everywhere": False, "boxes": {}},
                  {"name": "scratch", "url": "", "available": False, "why": "No remote yet. Push it to GitHub first.",
                   "everywhere": False, "boxes": {}}]},
    ME: {"ok": True, "every": 1800, "last_sync": None, "missing": [],
         "boxes": {"minastirith": {"last_sync": None, "error": ""}},
         "projects": [{"name": "treehouse", "url": "https://github.com/owner/treehouse.git", "available": True, "why": "",
                       "everywhere": True, "boxes": {"minastirith": {"state": "waiting", "note": ""}}}]},
}
G = 1 << 30
M = 1 << 20
RUNNING = {  # what `box _portal status` says under "running" (lib/sbrunning.py), biggest first
    "alice": [
        {"key": "c:shop-db", "kind": "database", "name": "shop-db", "detail": "Supabase · 5 containers",
         "mem": int(1.4 * G), "age": 3 * 86400 + 7200, "pids": 40, "stop": "containers", "cpu_usec": 3 * 86400 * 40_000},
        {"key": "", "kind": "agent", "name": "Claude session", "detail": "claude, node, python", "mem": 900 * M,
         "age": 5400, "pids": 12, "stop": None, "cpu_usec": 5400 * 50_000},
        {"key": "u:sb-blog.service", "kind": "server", "name": "blog", "detail": "node", "mem": 220 * M,
         "age": 86400 * 2, "pids": 3, "stop": "server", "cpu_usec": 1000, "keep": True,
         "about": "Alice's blog about the hackathon, read by the whole class"},
        {"key": "u:sb-t3.service", "kind": "t3", "name": "T3", "detail": "node", "mem": 180 * M,
         "age": 86400, "pids": 4, "stop": "t3", "cpu_usec": 1000},
        {"key": "c:3e184558f4de", "kind": "database", "name": "legacy-siege-mongo-with-a-really-long-container-name",
         "detail": "mongodb-atlas-local:8.0", "mem": 120 * M, "age": 600, "pids": 30, "stop": "containers",
         "cpu_usec": 600 * 300_000},
        {"key": "u:tmux-spawn-1.scope", "kind": "terminal", "name": "tmux terminal", "detail": "tmux, bash",
         "mem": 40 * M, "age": 4000, "pids": 2, "stop": "unit", "cpu_usec": 0},
        {"key": "", "kind": "background", "name": "Background", "detail": "Docker engine, dbus, init",
         "mem": 260 * M, "age": None, "pids": 9, "stop": None, "cpu_usec": 0},
    ],
}
RUNNING[ME] = [dict(x, name=x["name"].replace("shop-db", "uoft-index")) for x in RUNNING["alice"]] + [
    {"key": f"u:sb-s{i}.service", "kind": "server", "name": f"small-server-{i}", "detail": "python3", "mem": (9 - i) * M,
     "age": 3600 * i, "pids": 1, "stop": "server", "cpu_usec": 0, "exec": f"/usr/bin/python3 -m http.server 90{i}"}
    for i in range(1, 6)]
TEAM = {"alice": ["bob"]}  # alice already lets bob's agents into her T3
LABELS = {"claude": "Claude Code", "codex": "Codex", "gh": "GitHub", "t3connect": "T3 Connect",
          "antigravity": "Google (Antigravity)"}
SHARED = [{"name": "web", "state": "running", "port": SHARED_BASE, "created_by": "alice", "created": NOW - 86400,
           "updated_by": "bob", "updated": NOW - 600,
           "desc": "Discord bot that posts the week's hackathon schedule <b>every Monday</b>"},
          {"name": "down", "state": "stopped", "port": SHARED_BASE + 1, "created_by": "bob", "created": NOW - 3600}]
PAIR_TOKENS = set()
RUNNING_OK = {}  # account -> keys answered "Keep it" on Home
ISSUES_ON = {}  # account -> problem reports on (default) or off
ROOT_LOG = []
lock = threading.Lock()


def identity(req):
    if req.get("ts_id"):  # the tailnet page (port 8090): identity is the Tailscale login
        return next((n for n, r in sb.registry()["users"].items() if r.get("ts_id") == req["ts_id"]), None), False
    email = TOKENS.get(req.get("cf_jwt") or "")
    if not email:
        return None, False
    name, _ = sb.user_by_email(email)
    if name:
        return name, False
    if email in sb.owner_emails(w.CFG) and not sb.email_taken(email):
        return ME, True
    return None, False


WHERE = {"alice": {"name": "Alice", "status": "At the hackathon", "until_text": "today 18:00", "act": "active now"},
         "bob": {"act": "active 3h ago"}}
TG = {"ready": True, "linked": {"bob"}, "notify": {}}
MSGS = {"alice": [
    {"id": "18f0aa000001", "from": "bob", "to": "alice", "title": "Fix the login page before the demo", "level": "high",
     "due_text": "today 18:00", "ts": NOW - 3600, "context": "The demo starts at 6. Login 500s on Safari; done when it works there.",
     "read": False},
    {"id": "18f0aa000002", "from": "bob", "to": "alice", "title": "Can you review my PR?", "level": "normal",
     "ts": NOW - 86400, "context": "", "read": True}]}


def fake_msg(name, req):
    op = req.get("op")
    box = MSGS.setdefault(name, [])
    if op == "inbox":
        return {"ok": True, "messages": list(box), "open": len(box), "people": ["alice", "bob", ME], "you": name,
                "sent": ([{"id": "18f0aa000009", "to": "bob", "title": "Deploy is green", "ts": NOW - 600,
                           "seen": NOW - 300, "answer": ""}] if name == "alice" else [])}
    if op == "send":
        if not req.get("title") or not req.get("context"):
            return {"ok": False, "error": "title: required"}
        return {"ok": True, "message": f"sent 18f0aa00000f to {req.get('to')}"}
    m = next((x for x in box if x["id"] == req.get("id")), None)
    if not m:
        return {"ok": False, "error": "that message isn't in your inbox any more"}
    if op == "reply" and not req.get("text"):
        return {"ok": False, "error": "write a reply first"}
    if op in ("done", "archive"):
        box.remove(m)
    return {"ok": True, "message": {"done": "Marked done; they'll see it.", "reply": "Reply sent.",
                                    "archive": "Cleared from your inbox."}[op]}


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
        return {"ok": True, "pair_url": f"http://{TS_IP}:{OWNER_T3_PORT}/pair#token={tok}"}
    if owner and action not in ("status", "shared-ls", "serve-ctl", "serve-logs", "shared-ctl", "shared-logs",
                                "request-approve", "request-done", "request-decline", "self-update", "invite",
                                "boost", "hackathon", "running-stop", "running-all", "msg", "where", "telegram",
                                "telegram-setup", "restore", "projects", "running-stop-any", "serve-set", "login",
                                "code", "check", "peer-login", "peer-check", "peer-code", "logout", "peer-logout"):
        return {"ok": False, "error": "not available for the owner's account"}
    if action == "boost":  # root: boosts are gone (dynamic memory), it only explains
        return {"ok": True, "until": 0, "message": "Boosts aren't needed any more."}
    if action == "running-all":  # root: every account's Running list
        if not owner:
            return {"ok": False, "error": "not the owner"}
        accts = [{"user": n, "mem": sum(i["mem"] for i in RUNNING.get(n, [])),
                  "items": [dict(i) for i in RUNNING.get(n, [])]} for n in (ME, "alice", "bob")]
        return {"ok": True, "accounts": sorted(accts, key=lambda a: -a["mem"])}
    if action == "running-stop-any":  # root: the owner stops anyone's item, as that account
        if not owner:
            return {"ok": False, "error": "not the owner"}
        items = RUNNING.get(str(req.get("user")), [])
        it = next((i for i in items if i["key"] == req.get("key") and i["stop"]), None)
        if not it:
            return {"ok": False, "error": "That isn't running any more. Refresh the page."}
        if it.get("keep") and not req.get("force"):
            return {"ok": False, "error": f"{it['name']} is marked keep running, so it wasn't stopped."}
        items.remove(it)
        ROOT_LOG.append(("running-stop-any", req.get("user"), req.get("key"), bool(req.get("force"))))
        return {"ok": True, "message": f"{req.get('user')}: Stopped {it['name']}."}
    if action == "serve-set":
        srv = next((x for x in ACCOUNTS[name]["servers"] if x["name"] == req.get("key")), None)
        if not srv:
            return {"ok": False, "error": "no such server"}
        if req.get("what") == "keep":
            srv["keep"] = req.get("value") == "on"
            return {"ok": True, "message": f"{srv['name']} is marked keep running." if srv["keep"] else
                    f"{srv['name']} is no longer marked keep running."}
        srv["hackathon"] = "pause" if req.get("value") == "pause" else ""
        return {"ok": True, "message": f"{srv['name']} will be stopped while hackathon mode is on." if srv["hackathon"]
                else f"{srv['name']} keeps running during hackathons."}
    if action in ("serve-describe", "shared-describe"):  # box _portal serve-describe / box shared describe
        pool = ACCOUNTS[name]["servers"] if action == "serve-describe" else SHARED
        srv = next((x for x in pool if x["name"] == req.get("key")), None)
        about = " ".join(str(req.get("about") or "").split())
        if not srv:
            return {"ok": False, "error": "no such server"}
        if len(about) < 12:
            return {"ok": False, "error": "The description needs at least 12 characters."}
        srv["about" if action == "serve-describe" else "desc"] = about
        return {"ok": True, "message": f"Saved. {srv['name']}: {about}"}
    if action == "running-ok":
        RUNNING_OK.setdefault(name, set()).add(req.get("key"))
        return {"ok": True, "message": "Kept. It keeps running, and Home won't ask about it again for 7 days."}
    if action == "project-keep":
        return {"ok": True, "message": f"Kept {req.get('key')}. It stays in your home folder for at least another 20 days."}
    if action == "issues":
        ISSUES_ON[name] = req.get("op") == "on"
        return {"ok": True, "message": "Problem reports are on." if ISSUES_ON[name] else "Problem reports are off."}
    if action in ("friend-suspend", "friend-resume", "friend-remove", "invite-revoke", "hackathon-preview"):
        if not owner:
            return {"ok": False, "error": "not available for this account"}
        if action == "hackathon-preview":
            return {"ok": True, "servers": [{"user": ME, "name": "site", "about": "The owner's portfolio site"}]}
        if action == "invite-revoke":
            return {"ok": True, "message": "Invite cancelled. Its link no longer works."}
        reg = sb.registry()
        if req.get("key") not in reg["users"]:
            return {"ok": False, "error": f"{req.get('key')} is not a friend account on this machine"}
        if action == "friend-remove":
            return {"ok": True, "message": f"{req.get('key')}'s account is removed."}  # kept: other flows use them
        reg["users"][req["key"]]["status"] = "suspended" if action == "friend-suspend" else "active"
        save_registry(reg)
        return {"ok": True, "message": f"{req.get('key')} is paused." if action == "friend-suspend" else
                f"{req.get('key')} can sign in again."}
    if action in ("logout", "peer-logout"):
        return {"ok": True, "message": f"Signed out on {req.get('box') or 'treebox'}. You can sign in again with any account."}
    if action == "peer-code":
        return {"ok": True, "done": True, "message": f"Signed in on {req.get('box')}."}
    if action == "running-stop":  # `box _portal running-stop KEY`: only a live, stoppable item of this account
        items = RUNNING.get(name, [])
        it = next((i for i in items if i["key"] == req.get("key") and i["stop"]), None)
        if not it:
            return {"ok": False, "error": "That isn't running any more (or it can't be stopped from here). Refresh the page."}
        items.remove(it)
        return {"ok": True, "message": f"Stopped {it['name']}. It stays off, reboots included, until someone starts it again."}
    if action == "msg":
        return fake_msg(name, req)
    if action == "where":
        if req.get("op") == "set":
            if req.get("clear"):
                WHERE.get(name, {}).pop("status", None)
                return {"ok": True, "message": "Status cleared."}
            if not str(req.get("status") or "").strip():
                return {"ok": False, "error": "status: a few words, like \"In class\""}
            WHERE.setdefault(name, {})["status"] = req["status"]
            return {"ok": True, "message": f"Status saved. {name}: {req['status']}."}
        if req.get("op") == "private":
            WHERE.setdefault(name, {})["private"] = bool(req.get("private"))
            return {"ok": True, "message": "Hidden." if req.get("private") else "Shown again."}
        people = []
        for u in ("alice", "bob", ME):
            r = WHERE.get(u, {})
            v = {"user": u, "name": r.get("name", u), "activity": "private" if r.get("private") else r.get("act", "new here, no activity yet"),
                 "active_now": r.get("act") == "active now"}
            if r.get("status"):
                v["status"] = r["status"]
                if r.get("until_text"):
                    v["until_text"] = r["until_text"]
            people.append(v)
        return {"ok": True, "you": name, "people": people}
    if action == "telegram":
        op = req.get("op")
        if op == "status":
            return {"ok": True, "ready": TG["ready"], "bot": "treehouse_test_bot" if TG["ready"] else "",
                    **({"linked": True, "handle": "@" + name, "notify": TG["notify"].get(name, "important")}
                       if name in TG["linked"] else {"linked": False})}
        if not TG["ready"]:
            return {"ok": False, "error": "Telegram isn't set up on this box yet. The owner adds it on their dashboard."}
        if op == "link":
            return {"ok": True, "ready": True, "bot": "treehouse_test_bot",
                    "url": "https://t.me/treehouse_test_bot?start=" + hashlib.sha256(name.encode()).hexdigest()[:24]}
        if op == "unlink":
            TG["linked"].discard(name)
            return {"ok": True, "message": "Telegram disconnected"}
        if op == "notify":
            TG["notify"][name] = req.get("level")
            return {"ok": True, "message": "Saved."}
        if op == "test":
            return {"ok": True, "message": "Sent. Check Telegram on your phone."}
        return {"ok": False, "error": "unknown action"}
    if action == "telegram-setup":
        if not owner:
            return {"ok": False, "error": "not the owner"}
        if req.get("op") == "off":
            return {"ok": True, "message": "Telegram is off."}
        if ":" not in str(req.get("token") or ""):
            return {"ok": False, "error": "that doesn't look like a bot token (it looks like 123456789:ABC...)"}
        TG["ready"] = True
        return {"ok": True, "message": "Telegram is on: @treehouse_test_bot."}
    if action == "restore":
        return {"ok": True, "message": f"Restored {req.get('key')} to your home folder."}
    if action in ("peer-login", "peer-check"):  # the T3 app / ChatGPT on this person's other boxes
        if key_ := req.get("key"):
            if key_ not in ("t3connect", "codex", "claude", "antigravity"):
                return {"ok": False, "error": "unknown login"}
        boxes = ACCOUNTS[name].get("boxes") or {}
        if req.get("box"):
            boxes = {req["box"]: boxes.get(req["box"], {})}
            if key_ in ("claude", "antigravity"):  # these show a code on the page that comes back here
                return {"ok": True, "boxes": [{"box": req["box"], "done": False, "paste": True,
                                               "url": "https://claude.ai/oauth/authorize?code=true"}]}
        return {"ok": True, "boxes": [{"box": b, "done": False, "url": "https://accounts.t3.codes/device?user_code=WXYZ-1234",
                                       "code": "WXYZ-1234"} if action == "peer-login" else {"box": b, "done": False}
                                      for b in sorted(boxes)]}
    if action == "team":  # teammates' agents in your T3 (root: handle_team)
        mates = [u for u in UIDS if u != name]
        grants = TEAM.setdefault(name, [])
        op = req.get("op")
        if op == "list":
            return {"ok": True, "people": mates, "access": {"read-only": "Look only", "approval-required":
                    "Ask before changes", "full-access": "Full access"},
                    "grants": [{"to": g, "session": "s-" + g, "since": "2026-11-19", "last": ""} for g in grants]}
        if req.get("to") not in mates:
            return {"ok": False, "error": "pick a teammate"}
        if op == "grant":
            if req.get("access") not in ("read-only", "approval-required", "full-access"):
                return {"ok": False, "error": "pick what their agents may do"}
            if req["to"] not in grants:
                grants.append(req["to"])
            return {"ok": True, "message": f"{req['to']}'s agents can now work in your T3."}
        if op == "revoke":
            if req["to"] in grants:
                grants.remove(req["to"])
            return {"ok": True, "message": f"{req['to']}'s agents can't work in your T3 any more."}
        return {"ok": False, "error": "unknown action"}
    if action == "hackathon":  # root: sharedbox-admin hackathon_set
        if not owner:
            return {"ok": False, "error": "not the owner"}
        if req.get("op") == "off":
            sb.set_config({"HACKATHON_UNTIL": ""})
            return {"ok": True, "active": False, "until": "", "users": ""}
        ts = sb.parse_local_time(req.get("until"))
        if not ts or ts <= time.time():
            return {"ok": False, "error": f"--until must be a future local time (got {req.get('until')!r})"}
        sb.set_config({"HACKATHON_UNTIL": req["until"], "HACKATHON_USERS": req.get("users") or ""})
        return {"ok": True, "active": True, "until": req["until"], "users": req.get("users") or ""}
    if action == "invite":
        return {"ok": True, "invite_user": "sam", "invite_message": f"Open https://treehouse.test and sign in with "
                f"{req.get('email')}."}
    if action == "self-update":
        write("update.json", {"state": "running", "started": int(time.time())})
        return {"ok": True, "message": "Updating treehouse. This page may blink for a few seconds; refresh in a minute."}
    reg = sb.registry()
    if not owner and (reg["users"].get(name) or {}).pop("welcome", None):
        save_registry(reg)
    acct = ACCOUNTS[name]
    port = OWNER_T3_PORT if owner else PORT_OF[name]
    key = req.get("key")
    if action == "status":
        return {"ok": True, "logins": {k: {"label": LABELS[k], "ok": v} for k, v in acct["logins"].items()},
                "t3": None if acct.get("no_t3") else {"url": f"http://{TS_IP}:{port}/", "running": True, "sleeping": False,
                                                      "app": bool(acct.get("app"))},  # the T3 app connected
                "git": {"name": name, "email": ""}, "ssh": acct["ssh"], "box": "treebox",
                "running_ok": sorted(RUNNING_OK.get(name, ())), "issues_on": ISSUES_ON.get(name, True),
                "boxes": acct.get("boxes") or {}, "accounts": acct.get("accounts") or {},
                "boxes_checked": NOW - 240,
                "servers": [dict(s) for s in acct["servers"]],
                "running": [dict(i) for i in RUNNING.get(name, [])],
                **({"projects": json.loads(json.dumps(PROJECTS[name]))} if name in PROJECTS else {})}
    if action == "projects":  # "Projects on all your boxes": on/off
        pj = PROJECTS.get(name) or {"projects": []}
        r = next((r for r in pj["projects"] if r["name"] == key), None)
        if not r or not r["available"]:
            return {"ok": False, "error": f"There's no git project called {key} in your home folder."}
        r["everywhere"] = req.get("op") == "on"
        r["boxes"] = {b: {"state": "waiting", "note": ""} for b in pj["boxes"]} if r["everywhere"] else {}
        return {"ok": True, "message": f"{key} will be on all your boxes. The first copy starts within 10 minutes."
                if r["everywhere"] else f"{key} is no longer synced. Copies already on your other boxes stay where they are."}
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
    if action in ("t3", "t3app"):
        if acct.get("no_t3"):
            return {"ok": False, "error": "T3 Code isn't installed on this box yet. Ask the owner."}
        tok = hashlib.sha256(os.urandom(8)).hexdigest()[:24]
        PAIR_TOKENS.add(tok)
        pair = f"http://{TS_IP}:{port}/pair#token={tok}"
        return {"ok": True, "pair_url": pair, "url": f"http://{TS_IP}:{port}/",
                **({"app_link": "t3code://pair?pairingUrl=" + urllib.parse.quote(pair, safe="")} if action == "t3app" else {})}
    if action == "add-key":
        k, err = sb.validate_pubkey(req.get("sshkey", ""))
        return {"ok": False, "error": err} if err else {"ok": True, "key_added": True}
    if key not in LABELS:
        return {"ok": False, "error": "unknown login"}
    if action == "login":
        if acct["logins"][key]:
            return {"ok": True, "done": True}
        if key == "claude":
            return {"ok": True, "url": "https://claude.ai/oauth/authorize?code=true", "code": None, "paste": True}
        if key == "antigravity":
            return {"ok": True, "url": "https://accounts.google.com/o/oauth2/auth?access_type=offline", "code": None,
                    "paste": True}
        return {"ok": True, "url": "https://github.com/login/device", "code": "ABCD-1234", "paste": False}
    if action == "code":
        acct["logins"][key] = True
        return {"ok": True, "done": True}
    if action == "check":
        return {"ok": False, "error": f"{LABELS[key]} login hasn't finished: Waiting for you to sign in"}
    return {"ok": False, "error": "unknown action"}


w.spool_call = fake_root
w.SELF_IP = TS_IP

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
for p in (PORT_OF["alice"] + 3, SHARED_BASE):  # "blog" (running) and group "web"; "bot"/"down" stay closed
    serve(p, AppServer)



class PeerBox(BaseHTTPRequestHandler):  # another treehouse box answering /api/box (the Boxes card)
    def log_message(self, *a):
        pass

    def do_GET(self):
        data = json.dumps({"v": 1, "name": "minastirith", "version": "abc1234", "ncpu": 8, "load1": 1.25,
                           "level": "calm", "mem": 42.0, "disk_free": 81.0, "dashboard": "http://100.64.0.6:8090/",
                           "t3": "", "cpu": 23.0, "mem_used_b": 13 * 2**30, "mem_total_b": 31 * 2**30,
                           "disk_free_b": 370 * 2**30, "disk_total_b": 456 * 2**30, "people": ["alice", "dave"]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


PEER_PORT = free_port()
serve(PEER_PORT, PeerBox)
w.peer_url = lambda ip: f"http://127.0.0.1:{PEER_PORT if ip == '100.64.0.6' else free_port()}/api/box"  # "gone": refused
w.peers_tick()
for _ in range(100):
    if not any(p["busy"] for p in w.PEERS.values()):
        break
    time.sleep(0.05)

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

    def __init__(self, browser, persona, token, cookie=False, width=1280, scheme="dark", js=True):
        self.persona, self.token = persona, token
        extra = {"Cf-Access-Jwt-Assertion": token} if token and not cookie else {}
        self.ctx = browser.new_context(ignore_https_errors=True, extra_http_headers=extra, java_script_enabled=js,
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
        self.dialogs = getattr(self, "dialogs", [])
        page.on("dialog", lambda d: (self.dialogs.append(d.message), d.accept()))

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


AGENT_TEXT_PAGES = ("/_box/connect.md", "/_box/api/whoami")


def inspect(s, where, page=None, dashboard_link=True):
    """Content checks on whatever page is showing."""
    page = page or s.page
    try:
        page.wait_for_load_state("load", timeout=15000)
        text = page.inner_text("body", timeout=5000)
    except Exception as ex:  # noqa: BLE001
        fail(s.persona, where, f"page didn't load: {ex}")
        return
    if urllib.parse.urlsplit(page.url).path in AGENT_TEXT_PAGES:
        return  # plain-text instructions for an agent: box rules (sudo etc.) belong there, no nav needed
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
    tabs = page.locator("nav.tabs")
    if tabs.count():  # the dashboard's tab bar: links, exactly one marked as the current page
        cur = tabs.first.locator("a[aria-current='page']")
        if cur.count() != 1:
            fail(s.persona, where, f"tab bar has {cur.count()} current tabs")
        elif "tab=" in page.url and f"tab={cur.get_attribute('data-tab')}" not in page.url \
                and "tab=admin" not in page.url and "tab=boxes" not in page.url:  # old links: aliases
            fail(s.persona, where, f"tab bar marks '{cur.get_attribute('data-tab')}' on {page.url}")
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


def press(page, target, timeout=30):
    """Click a form's button and wait for its result: in place (the page's script swaps the card and counts
    window.sbDone up) or, without the script, a page load."""
    before = page.evaluate("window.sbDone || 0")
    page.evaluate("window.__stay = 1")
    target.click()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.evaluate(f"!window.__stay || (window.sbDone || 0) > {before}"):
                break
        except Exception:  # noqa: BLE001 - mid-navigation
            pass
        page.wait_for_timeout(40)
    else:
        raise TimeoutError("no result after pressing the button")
    page.wait_for_load_state("load")


def stayed(page):
    """True when the last press() didn't load a new page (the result came in place)."""
    return page.evaluate("window.__stay === 1")


SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBfW8kyhn1bfC1CK0D4Z0N8F0rG5uRoHxdQhqC1Vd8Df qa@laptop"


def submit(page, sig):
    """Fill and submit the form matching sig on the current page. Returns False if it isn't there."""
    for i, action, vals, label in form_sigs(page):
        if (action, vals, label) == sig[1:]:
            f = page.locator("form").nth(i)
            page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")  # a person opens it
            for inp in f.locator("input:not([type=hidden]), textarea").all():
                n = inp.get_attribute("name") or ""
                if inp.get_attribute("readonly") is not None or inp.get_attribute("type") in ("checkbox", "radio"):
                    continue
                inp.fill({"sshkey": SSH_KEY, "code": "sk-paste-code-123", "username": "carol",
                          "email": "sam@example.com", "note": "Sam", "hours": "2",
                          "until": time.strftime("%Y-%m-%dT%H:%M", time.localtime(time.time() + 7200))}.get(n, "x"))
            press(page, f.locator("button").first)
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
    tabs = []
    if start.startswith("/_box/me"):  # every tab of the dashboard is its own page: crawl each one
        page.goto(url(start))
        tabs = [urllib.parse.urlsplit(a.get_attribute("href")) for a in page.locator("nav.tabs a").all()]
        queue += [(t.path + "?" + t.query, ()) for t in tabs]
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
            page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")  # a person opens it
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
    for t in tabs:
        if (t.path + "?" + t.query, ()) not in seen_pages:
            fail(persona, "crawl", f"tab {t.query} never visited")
    s.close()
    return seen_pages


def flow_open_t3(browser, persona, token, t3_port, cookie=False):
    """Dashboard -> "Open my T3 Code" -> paired into the right T3; second time straight in; bookmark -> dashboard."""
    s = Session(browser, persona, token, cookie=cookie)
    page = s.page
    page.goto(url("/"))
    if urllib.parse.urlsplit(page.url).path != "/_box/me":
        fail(persona, "open /", f"typed URL should land on the dashboard, got {page.url}")
    # the start card's link when it has one (not before an AI login), else the header's "T3 in browser"
    btn = page.locator("#start a[href='/_box/app']")
    if not btn.count():
        btn = page.get_by_role("link", name=re.compile("Open my T3 Code|Open T3|T3 in browser"))
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
    page.get_by_role("link", name=re.compile("Open my T3 Code|Open T3|T3 in browser")).first.click()
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
    press(page, page.get_by_role("button", name="Sign in with Claude"))
    inspect(s, "claude login card")
    if not page.locator("#start a.btn[href^='https://claude.ai']").count():
        fail("bob", "claude login", "no 'Open the login page' link in the start card")
    page.fill("input[name=code]", "sk-paste-code-123")
    press(page, page.get_by_role("button", name="Finish"))
    inspect(s, "claude code pasted")
    if "Signed in" not in page.locator("#start").inner_text():
        fail("bob", "claude login", "no 'Signed in' confirmation in the start card")
    if not stayed(page):
        fail("bob", "claude login", "the page reloaded instead of showing the result in place")
    page.goto(url("/_box/me?tab=account"))
    gh = page.get_by_role("button", name="Connect GitHub")
    if not gh.count():
        fail("bob", "account tab", "no Connect GitHub")
    else:
        press(page, gh)
        inspect(s, "github device code")
        if "ABCD-1234" not in page.locator("#account").inner_text():
            fail("bob", "github device code", "the code isn't shown in the account card")
        page.wait_for_timeout(4500)  # the page polls once by itself (its script ran after the swap)
        press(page, page.get_by_role("button", name="Check now"))
        inspect(s, "github check now")
        if "hasn't finished" not in page.locator("#account").inner_text():
            fail("bob", "github check now", "Check now's answer isn't in the account card")
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
    for path, want in ((f"/p/{PORT_OF['alice'] + 3}/", f"App {PORT_OF['alice'] + 3}"), ("/s/web/", f"App {SHARED_BASE}")):
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
                 if re.search(r"log ?out|sign out|different email", (a.inner_text() or ""), re.I)]
        if not hrefs or any(h != "/cdn-cgi/access/logout" for h in hrefs):
            fail(persona, "log out", f"log out links: {hrefs}")
        s.drain("logout")
        s.close()


def owner_requests(browser):
    s = Session(browser, "owner", "tok-owner")
    page = s.page
    page.goto(url("/_box/me"))
    for label in ("Approve", "Mark handled", "Decline"):
        page.goto(url("/_box/me?tab=requests"))
        ask = page.locator("#requests details.cf > summary", has_text=label)
        if ask.count():  # says what it does first: open the panel, check it says something, press its button
            ask.first.click()
            panel = page.locator("#requests details.cf[open] .cfb").first
            if len(panel.inner_text().strip()) < 40:
                fail("owner", f"request {label}", f"the panel doesn't say what happens: {panel.inner_text()!r}")
            b = panel.locator("button")
        else:
            b = page.locator("#requests button:visible", has_text=label)
        if not b.count():
            continue
        press(page, b.first)
        inspect(s, f"request {label}")
        if not page.locator("#requests .note.ok").count():
            fail("owner", f"request {label}", page.inner_text("body")[:200])
    s.drain("owner requests")
    s.close()


FRIEND_TAB_PAGES = ["/_box/me", "/_box/me?tab=messages", "/_box/me?tab=servers", "/_box/me?tab=machines",
                    "/_box/me?tab=account"]
SHOT_PAGES = {
    "alice": FRIEND_TAB_PAGES + ["/_box/me?tab=account&range=7d", "/_box/features"],
    "bob": ["/_box/me?welcome=1", "/_box/me"],
    "dave": ["/_box/me", "/_box/me?tab=account"],
    "erin": ["/_box/me"],
    "owner": FRIEND_TAB_PAGES + [f"/_box/me?tab={t}" for t in ("machines", "people", "running", "requests", "settings",
                                                                "events")],
    "stranger": ["/", f"/i/{INVITE_TOK}", "/_box/nope"],
}
SHOT_TOK = {"alice": "tok-alice", "bob": "tok-bob", "owner": "tok-owner", "stranger": "tok-stranger", "dave": "tok-dave",
            "erin": "tok-erin"}


def screenshots(browser):
    os.makedirs(SHOTS, exist_ok=True)
    for persona, paths in SHOT_PAGES.items():
        for width in (1920, 1280, 390):
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
                    s.page.goto(url("/_box/me?tab=servers"))
                    press(s.page, s.page.locator("button", has_text="View logs").first)
                    s.page.screenshot(path=os.path.join(SHOTS, f"{persona}-logs-{width}-{scheme}.png"), full_page=True)
                if persona == "alice":
                    s.page.goto(url("/_box/me"))
                    s.page.locator("#start summary", has_text="Advanced").click()
                    b = s.page.locator("#start button:visible", has_text="Get setup instructions")
                    if not b.count():
                        fail("alice", f"screenshots {width} {scheme}", "no 'Get setup instructions' button")
                    else:
                        press(s.page, b.first)
                        inspect(s, f"setup prompt @{width} {scheme}")
                        s.page.screenshot(path=os.path.join(SHOTS, f"alice-app-prompt-{width}-{scheme}.png"), full_page=True)
                        s.page.locator("#start summary", has_text="Do it by hand").click()
                        s.page.screenshot(path=os.path.join(SHOTS, f"alice-app-byhand-{width}-{scheme}.png"), full_page=True)
                        ACCOUNTS["alice"]["app"] = True  # as if the app on her laptop connected: "Open T3" leads
                        try:
                            s.page.goto(url("/_box/me"))
                            s.page.wait_for_load_state("load")
                            s.page.screenshot(path=os.path.join(SHOTS, f"alice-me-app-set-{width}-{scheme}.png"), full_page=True)
                        finally:
                            ACCOUNTS["alice"]["app"] = False
                s.drain(f"screenshots {width} {scheme}")
                s.close()
                w.portal_rate.clear()


MAIN_JS = """() => [...document.querySelectorAll('main a.btn, main button')].filter(el =>
  !el.matches('.secondary, .sm, .linkbtn') && el.offsetParent !== null && !el.closest('details:not([open])'))
  .map(el => el.textContent.trim())"""
HOSTNAME = socket.gethostname()


def no_hostname_addresses(s, where, page=None):
    """Friends who joined the tailnet as shared-in users can't resolve the box's short name: no address shows it."""
    page = page or s.page
    vals = page.evaluate("""() => [...document.querySelectorAll('code, pre, textarea, input')]
        .map(el => el.matches('textarea, input') ? el.value : el.textContent)""")
    hrefs = page.evaluate("() => [...document.querySelectorAll('a[href]')].map(a => a.getAttribute('href'))")
    for v in vals + hrefs:
        if re.search(rf"(//|@){re.escape(HOSTNAME)}\b", v or ""):
            fail(s.persona, where, f"address uses the box's name, not its Tailscale IP: {v[:120]!r}")


def start_cards(browser):
    """The start card: exactly one main button per state (S0..S3), and the T3 app setup prompt."""
    cases = [("erin", "tok-erin", [], "T3 isn't set up on this machine yet"),
             ("bob", "tok-bob", ["Sign in with Claude"], "Sign in with ChatGPT"),
             ("bob", "tok-bob", ["Sign in with Claude"], "You only need one"),
             ("alice", "tok-alice", ["Open T3"], "Set up the T3 app"),  # under Advanced (optional)
             ("dave", "tok-dave", ["Open T3"], "Get setup instructions"),  # no Tailscale id on record
             ("alice", "tok-alice", ["Open T3"], "Start a chat", 390)]
    for persona, tok, want, text, *width in cases:
        s = Session(browser, persona, tok, **({"width": width[0]} if width else {}))
        s.page.goto(url("/_box/me"))
        where = f"start card ({persona})"
        inspect(s, where)
        got = s.page.evaluate(MAIN_JS)
        if got != want:
            fail(persona, where, f"main buttons {got}, expected {want}")
        if s.page.locator("#start details.adv[open]").count():
            fail(persona, where, "Advanced is open on arrival: the three steps should be all a new friend sees")
        s.page.evaluate("document.querySelectorAll('#start details').forEach(d => d.open = true)")
        card = s.page.inner_text("#start") if s.page.locator("#start").count() else ""
        if text not in card:
            fail(persona, where, f"start card lacks {text!r}: {card[:200]!r}")
        if "Ask the owner to add you to Tailscale" in card:
            fail(persona, where, "tells a friend who may already be on Tailscale to ask for it")
        no_hostname_addresses(s, where)
        if s.page.locator("text=Link my T3 account").count():
            fail(persona, where, "T3 Connect is shown")
        s.drain(where)
        s.close()
    # S2 -> the prompt -> S3 once the app has really connected (a T3 session labelled for it)
    s = Session(browser, "alice (prompt)", "tok-alice")
    page = s.page
    page.goto(url("/_box/me"))
    page.locator("#start summary", has_text="Advanced").click()
    press(page, page.locator("#start button:visible", has_text="Get setup instructions"))
    inspect(s, "setup prompt")
    prompt = page.locator("#app-prompt").input_value() if page.locator("#app-prompt").count() else ""
    pair = page.locator("#start input.mono").input_value() if page.locator("#start input.mono").count() else ""
    port = PORT_OF["alice"]
    if f"t3code://pair?pairingUrl=http%3A%2F%2F{TS_IP}%3A{port}%2Fpair%23token%3D" not in prompt:
        fail("alice", "setup prompt", f"no t3code:// link built from the IP: {prompt[:300]!r}")
    if not pair.startswith(f"http://{TS_IP}:{port}/pair#token=") or pair not in prompt:
        fail("alice", "setup prompt", f"pairing URL {pair!r} not in the prompt / not on the IP")
    for must in (f"Add the backend at http://{TS_IP}:{port}?", f"{PUBLIC}/_box/me", "https://t3.codes",
                 "Settings, Environments, Add environment, pick Remote link", "Start-Process",
                 "If it still times out, stop and tell me",
                 f"curl -s --noproxy '*' --max-time 15 http://{TS_IP}:{port}/.well-known/t3/environment",
                 "0. Already working?", "ask me what the app showed"):
        if must not in prompt:
            fail("alice", "setup prompt", f"missing {must!r}")
    if re.search(r"\{[^}]*\}", prompt):
        fail("alice", "setup prompt", f"leftover placeholder: {re.search(r'[{][^}]*[}]', prompt).group(0)}")
    link = page.locator("#start a[href^='t3code://']")
    if not link.count() or "btn" in (link.first.get_attribute("class") or ""):
        fail("alice", "setup prompt", "no plain t3code:// link")
    if page.evaluate(MAIN_JS) != ["Copy prompt"]:
        fail("alice", "setup prompt", f"main buttons {page.evaluate(MAIN_JS)}")
    page.get_by_role("button", name="Copy prompt").click()
    try:
        page.wait_for_function("[...document.querySelectorAll('#start button')].some(b => b.textContent === 'Copied')",
                               timeout=3000)
    except Exception:  # noqa: BLE001
        fail("alice", "setup prompt", "Copy prompt didn't say Copied")
    if re.search(r"\b\d{1,2}:\d{2}\b", page.inner_text("#start")):
        fail("alice", "setup prompt", "shows a clock time")
    no_hostname_addresses(s, "setup prompt")
    if page.locator("#start details.byhand[open]").count():
        fail("alice", "setup prompt", "the do-it-by-hand steps are open: one obvious next step only")
    if "Paste it into Claude Code, Codex or any agent on your laptop" not in page.inner_text("#start"):
        fail("alice", "setup prompt", "no next step after Copy prompt")
    page.locator("#start summary", has_text="Do it by hand").click()
    page.locator("#start button", has_text="Copy").last.click()
    try:
        page.wait_for_function("[...document.querySelectorAll('#start details button')].some(b => b.textContent === 'Copied')",
                               timeout=3000)
    except Exception:  # noqa: BLE001
        fail("alice", "setup prompt", "the pairing link's Copy didn't say Copied")
    if any(c["name"] == "tb_app" for c in s.ctx.cookies()):
        fail("alice", "setup prompt", "sets the old tb_app cookie")
    page.goto(url("/_box/me"))  # only made a prompt, the app never connected: still the setup prompt
    if page.evaluate(MAIN_JS) != ["Open T3"]:
        fail("alice", "after the prompt, app not connected", f"main buttons {page.evaluate(MAIN_JS)}")
    ACCOUNTS["alice"]["app"] = True
    try:
        page.goto(url("/_box/me"))
        if page.evaluate(MAIN_JS) != ["Open T3"]:
            fail("alice", "app connected (S3)", f"main buttons {page.evaluate(MAIN_JS)}")
        if not page.locator("#start button.linkbtn", has_text="Get new setup instructions").count():
            fail("alice", "app connected (S3)", "no 'Get new setup instructions'")
        page.locator("#start summary", has_text="Advanced").click()
        press(page, page.locator("#start button", has_text="Get new setup instructions"))
        if not page.locator("#app-prompt").count():
            fail("alice", "S3 new prompt", "no prompt")
    finally:
        ACCOUNTS["alice"]["app"] = False
    s.drain("setup prompt")
    s.close()


def boxes_card(browser):
    """The Machines tab (everyone): one card per machine (this one, a live one with full numbers, an offline one).
    The owner sees all of BOXES with links; alice sees the machines she has an account on, without links to the
    owner's T3. Old ?tab=boxes links land on Machines."""
    for width, scheme in ((1280, "dark"), (390, "light")):
        s = Session(browser, "owner", "tok-owner", width=width, scheme=scheme)
        s.page.goto(url("/_box/me?tab=boxes"))
        where = f"machines tab @{width}"
        if s.page.locator("nav.tabs a.on").inner_text().strip() != "Machines":
            fail("owner", where, "?tab=boxes didn't land on Machines")
        if SHOTS and s.page.locator("#machines").count():
            os.makedirs(SHOTS, exist_ok=True)
            s.page.screenshot(path=os.path.join(SHOTS, f"owner-machines-{width}-{scheme}.png"), full_page=True)
        inspect(s, where)
        cards = s.page.locator("#machines .mach")
        texts = [cards.nth(i).inner_text().replace("\xa0", " ") for i in range(cards.count())]
        if len(texts) != 3:
            fail("owner", where, f"expected 3 machines, got {texts}")
        else:
            this, peer, gone = texts
            for row, musts in ((this, ["treebox", "Online", "(this machine)"]),
                               (peer, ["minastirith", "Online", "23% busy", "13 GB", "of 31 GB", "86 GB", "1.2",
                                       "8 cores", "Alice", "dave", "abc1234", "Open its dashboard"]),
                               (gone, ["krishbox", "Offline", "doesn't answer"])):
                for m in musts:
                    if m not in row:
                        fail("owner", where, f"card lacks {m!r}: {row!r}")
        if not s.page.locator("#machines a[href='http://100.64.0.6:8090/']").count():
            fail("owner", where, "no link to the other machine's dashboard")
        s.drain(where)
        s.close()
    for width in (1280, 390):
        s = Session(browser, "alice", "tok-alice", width=width, scheme="light")
        s.page.goto(url("/_box/me?tab=machines"))
        where = f"machines tab (alice) @{width}"
        if SHOTS:
            s.page.screenshot(path=os.path.join(SHOTS, f"alice-machines-{width}-light.png"), full_page=True)
        inspect(s, where)
        body = s.page.locator("#machines").inner_text() if s.page.locator("#machines").count() else ""
        for m in ("treebox", "minastirith", "krishbox", "Memory", "Using it now"):
            if m not in body:
                fail("alice", where, f"lacks {m!r}")
        if s.page.locator("#machines a[href*='3773']").count() or "treehouse version" in body:
            fail("alice", where, "a friend sees the owner's links or versions")
        # her per-machine AI accounts: a different Claude account on minastirith, and Sign in there
        s.page.goto(url("/_box/me?tab=account"))
        acct = s.page.locator("#accounts").inner_text() if s.page.locator("#accounts").count() else ""
        for m in ("alice@example.com", "alice.work@example.com", "This machine's own account", "Not connected yet"):
            if m not in acct:
                fail("alice", where, f"AI accounts table lacks {m!r}")
        if width == 1280:
            row = s.page.locator("#accounts tr", has_text="minastirith")
            press(s.page, row.locator("button", has_text="Sign in").last)
            out = s.page.locator("#accounts [data-out]").inner_text()
            if "Sign in to" not in out or "minastirith" not in out or not stayed(s.page):
                fail("alice", where, f"Sign in on minastirith didn't show its steps in place: {out!r}")
        s.drain(where)
        s.close()


def running_card(browser):
    """The Running card (alice: rows, the forgotten-database flag, Stop with its result in the card), the owner's
    Running tab, and group-server descriptions (escaped; a missing one asks for it)."""
    import copy
    saved = copy.deepcopy(RUNNING)
    s = Session(browser, "alice", "tok-alice")
    s.page.goto(url("/_box/me?tab=servers"))
    where = "running card"
    inspect(s, where)
    card = s.page.locator("#running section")
    text = card.inner_text().replace("\xa0", " ") if card.count() else ""
    for must in ("shop-db", "Supabase · 5 containers", "1.4 GB", "running 3d 2h", "Claude session", "blog",
                 "Show 1 more", "always get", "Your agent can list"):
        if must not in text:
            fail("alice", where, f"card lacks {must!r}")
    if card.locator("li").first.locator(".pill.warn").count() != 1:
        fail("alice", where, "a database up 3 days with 1.4G isn't flagged")
    stops = card.locator("details.cf > summary", has_text="Stop")
    if stops.count() != 4:  # shop-db, blog, mongo, tmux (+0 for the agent and the background line)
        fail("alice", where, f"expected 4 Stop buttons, got {stops.count()}")
    stops.first.click()  # Stop first says what happens, in the page, with the button that does it
    panel = card.locator("details.cf[open] .cfb").first
    if "all 5 of its containers" not in panel.inner_text() or "data is kept" not in panel.inner_text():
        fail("alice", where, f"Stop on shop-db didn't say what happens: {panel.inner_text()!r}")
    if s.dialogs:
        fail("alice", where, f"a browser pop-up appeared: {s.dialogs}")
    press(s.page, panel.locator("button"))
    if "Stopped shop-db" not in s.page.locator("#running [data-out]").inner_text():
        fail("alice", where, "no 'Stopped shop-db' notice in the card")
    if "shop-db" in s.page.locator("#running ul.run").first.inner_text():
        fail("alice", where, "shop-db still listed after Stop")
    if not stayed(s.page):
        fail("alice", where, f"Stop reloaded the page instead of answering in the card: {s.page.url}")
    grp = s.page.locator("#shared .srv .desc")
    if not grp.count() or "every Monday" not in grp.first.inner_text() or grp.first.locator("b").count():
        fail("alice", where, "group server description missing or not escaped")
    if "No description yet" not in s.page.inner_text("body"):
        fail("alice", where, "a group server without a description doesn't ask for one")
    if s.page.locator("button", has_text="boost").count() or "Emergency memory boost" in s.page.inner_text("body"):
        fail("alice", where, "the boost button is still there")
    s.drain(where)
    s.close()
    RUNNING.clear()
    RUNNING.update(copy.deepcopy(saved))  # alice stopped shop-db above; the owner's tab starts from the full list
    s = Session(browser, "owner", "tok-owner")
    s.page.goto(url("/_box/me?tab=running"))
    where = "owner running tab"
    inspect(s, where)
    t = s.page.locator(".runall").inner_text() if s.page.locator(".runall").count() else ""
    for must in ("alice", "bob", "uoft-index", "Nothing running", "People's programs use", "Protected from automatic stops",
                 "No description yet. Started with"):
        if must not in t:
            fail("owner", where, f"tab lacks {must!r}")
    if not s.page.locator(".runall summary", has_text="Stop").count():
        fail("owner", where, "no Stop buttons on All running")

    def stop_says(row):
        """Open a row's Stop: (what it says will happen, the button that does it)."""
        row.locator("details.cf > summary").first.click()
        panel = row.locator("details.cf[open] .cfb").first
        return panel.inner_text(), panel.locator("button")
    # someone else's database: the panel names whose it is, the result shows in the card, no jump
    s.page.evaluate("window.scrollTo(0, 400)")
    row = s.page.locator(".runall li", has_text="shop-db")  # alice's (the owner's copy is called uoft-index)
    said, go = stop_says(row)
    if "This is alice's" not in said:
        fail("owner", where, f"Stop on alice's database didn't say whose it is: {said!r}")
    press(s.page, go)
    out = s.page.locator("#running-all [data-out]").inner_text()
    if "Alice: Stopped shop-db" not in out or not stayed(s.page):
        fail("owner", where, f"no in-place result for stopping alice's database: {out!r}")
    # T3: says chats disconnect; protected: says so, and "Stop anyway" sends force
    said, go = stop_says(s.page.locator(".runall li", has_text=re.compile(r"^T3")).filter(
        has=s.page.locator("details.cf")).last)
    if "open chats disconnect" not in said:
        fail("owner", where, f"stopping a T3 didn't warn about chats: {said!r}")
    press(s.page, go)
    said, go = stop_says(s.page.locator(".runall li", has_text="Protected from automatic stops").first)
    if "protected" not in said or go.inner_text().strip() != "Stop anyway":
        fail("owner", where, f"a protected server's Stop didn't say so: {said!r}")
    press(s.page, go)
    if not any(isinstance(x, tuple) and x[0] == "running-stop-any" and x[3] for x in ROOT_LOG):
        fail("owner", where, "Stop anyway didn't send force")
    if s.dialogs:
        fail("owner", where, f"a browser pop-up appeared: {s.dialogs}")
    s.drain(where)
    # the Machines tab and the per-machine AI accounts
    for tab, musts in (("machines", ("Machines", "Online", "CPU", "Memory", "Disk", "Load", "Using it now")),
                       ("account", ("AI accounts on each machine", "minastirith", "krishbox",
                                    "owner.chatgpt@x.io", "Sign in"))):
        s.page.goto(url(f"/_box/me?tab={tab}"))
        inspect(s, f"owner {tab} tab")
        body = s.page.inner_text("body")
        for m in musts:
            if m not in body:
                fail("owner", f"owner {tab} tab", f"lacks {m!r}")
    s.drain("owner machines/account")
    s.close()
    RUNNING.clear()
    RUNNING.update(saved)


def in_view(page, sel):
    return page.evaluate("""s => { const el = document.querySelector(s); if (!el) return false;
        const r = el.getBoundingClientRect(); return r.bottom > 0 && r.top < innerHeight }""", sel)


def in_place(browser):
    """Every button's result shows inside the card holding it, without a reload and without jumping to the top
    (logs in a scrollable box in the server's card, Stop, status, a failed send); without JavaScript the redirect
    comes back to the same tab and card. Tabs: each its own URL, old #card links land on the right tab."""
    import copy
    saved = copy.deepcopy(RUNNING)
    s = Session(browser, "alice (in place)", "tok-alice", width=390)
    page = s.page
    page.goto(url("/_box/me?tab=servers"))
    where = "in place: shared server logs"
    b = page.locator("#shared button", has_text="View logs").first
    b.scroll_into_view_if_needed()
    page.evaluate("window.scrollBy(0, 120 - Math.max(0, innerHeight / 2 - document.querySelector('#shared').getBoundingClientRect().top))")
    y0 = page.evaluate("scrollY")
    if y0 <= 0:
        fail("alice", where, "the test needs the page scrolled down first")
    press(page, b)
    if not stayed(page):
        fail("alice", where, "the page reloaded")
    if page.locator("#shared [data-out] pre.logs").count() != 1:
        fail("alice", where, "the logs aren't in a scrollable box inside the group server's card")
    if page.evaluate("getComputedStyle(document.querySelector('#shared pre.logs')).overflowY") not in ("auto", "scroll"):
        fail("alice", where, "the logs box doesn't scroll")
    if page.evaluate("scrollY") < 1:
        fail("alice", where, "the page jumped to the top")
    if not in_view(page, "#shared [data-out] > *"):
        fail("alice", where, "the logs aren't on screen after pressing the button")
    if page.locator("#servers pre.logs").count():
        fail("alice", where, "logs showed in another card")
    page.locator("#shared [data-close]").click()
    if page.locator("#shared pre.logs").count():
        fail("alice", where, "Close didn't close the logs")
    inspect(s, where)
    s.drain(where)
    where = "in place: own server restart"
    y0 = page.evaluate("scrollY")
    press(page, page.locator("#servers button", has_text="Restart").first)
    if not stayed(page) or "Blog: restarted" not in page.locator("#servers [data-out]").inner_text():
        fail("alice", where, f"no result in the servers card: {page.locator('#servers').inner_text()[:200]!r}")
    if page.locator("#servers [data-out] .note.ok").count() != 1:
        fail("alice", where, "the result isn't one ok notice")
    s.drain(where)
    where = "in place: Stop in Running"
    stop = page.locator("#running details.cf > summary", has_text="Stop").last
    stop.scroll_into_view_if_needed()
    stop.click()
    press(page, page.locator("#running details.cf[open] .cfb button").first)
    if not stayed(page) or "Stopped" not in page.locator("#running [data-out]").inner_text():
        fail("alice", where, "Stop's result isn't in the Running card")
    if not in_view(page, "#running [data-out] > *") or page.evaluate("scrollY") < 1:
        fail("alice", where, "Stop's result is off screen or the page jumped to the top")
    s.drain(where)
    where = "in place: status on Home"
    page.goto(url("/_box/me"))
    page.fill("#people input[name=status]", "In class")
    press(page, page.locator("#people button", has_text="Show my status"))
    if not stayed(page) or "Status saved" not in page.locator("#people [data-out]").inner_text():
        fail("alice", where, "no 'Status saved' in the Who's here card")
    s.drain(where)
    where = "in place: failed send keeps the form open"
    page.goto(url("/_box/me?tab=messages"))
    page.locator("#messages summary", has_text="Write a message").click()
    page.fill("#messages input[name=title]", "Hi")
    page.evaluate("document.querySelector('#messages textarea[name=context]').required = false")  # root says no
    press(page, page.locator("#messages details.compose button", has_text="Send"))
    if not stayed(page) or not page.locator("#messages [data-out] .note.bad").count():
        fail("alice", where, "the error isn't in the Messages card")
    if not page.locator("#messages details.compose[open]").count():
        fail("alice", where, "the form closed after a failed send")
    s.drain(where)
    where = "in place: the machine unreachable"
    page.goto(url("/_box/me?tab=servers"))
    page.route("**/_box/me", lambda r: r.abort() if r.request.method == "POST" else r.continue_())
    press(page, page.locator("#servers button", has_text="View logs").first)
    if "Couldn't reach this machine" not in page.locator("#servers [data-out]").inner_text():
        fail("alice", where, "no error in the card when the request fails")
    if page.locator("#servers button.busy, #servers button:disabled").count():
        fail("alice", where, "the button stays busy after a failed request")
    page.unroute("**/_box/me")
    s.bad_responses.clear()
    s.console.clear()
    where = "tabs: old #card links"
    for old, tab in (("#running", "servers"), ("#messages", "messages"), ("#account", "account"), ("#dash", "account"),
                     ("#people", "home")):
        page.goto(url("/_box/me" + old))
        try:
            page.wait_for_function(f"document.querySelector('nav.tabs a[aria-current]').dataset.tab === '{tab}'",
                                   timeout=10000)
        except Exception:  # noqa: BLE001
            fail("alice", where, f"/_box/me{old} didn't land on the {tab} tab: {page.url}")
    page.goto(url("/_box/me?tab=servers"))
    page.goto(url("/_box/me?tab=account"))
    page.go_back()
    if "tab=servers" not in page.url or page.locator("nav.tabs a[aria-current]").get_attribute("data-tab") != "servers":
        fail("alice", where, f"Back didn't return to the servers tab: {page.url}")
    focused = []
    for _ in range(12):
        page.keyboard.press("Tab")
        focused.append(page.evaluate("document.activeElement.textContent.trim()"))
    if not any(f.startswith("Messages") for f in focused):
        fail("alice", where, f"the tabs can't be reached with the keyboard: {focused}")
    s.drain(where)
    s.close()
    # without JavaScript: post/redirect/get back to the same tab, scrolled to the card, the result inside it
    s = Session(browser, "alice (no js)", "tok-alice", js=False)
    page = s.page
    page.goto(url("/_box/me?tab=servers"))
    where = "no JavaScript: logs"
    with page.expect_navigation():
        page.locator("#servers button", has_text="View logs").first.click()
    if "tab=servers" not in page.url or not page.url.endswith("#servers"):
        fail("alice", where, f"didn't come back to the servers card: {page.url}")
    if page.locator("#servers [data-out] pre.logs").count() != 1:
        fail("alice", where, "the logs aren't inside the servers card")
    inspect(s, where)
    s.drain(where)
    s.close()
    s = Session(browser, "owner (in place)", "tok-owner")
    page = s.page
    page.goto(url("/_box/me?tab=admin"))
    if page.locator("nav.tabs a[aria-current]").get_attribute("data-tab") != "requests":
        fail("owner", "tabs", "?tab=admin (old links) doesn't open Requests while some are open")
    if page.locator("nav.tabs .tsep").count() != 1:
        fail("owner", "tabs", "the admin tabs aren't in the same bar after one divider")
    page.goto(url("/_box/me?tab=people"))
    page.fill("#invite input[name=email]", "sam@example.com")
    press(page, page.locator("#invite button", has_text="Create their account"))
    if not stayed(page) or "Invite ready" not in page.locator("#invite [data-out]").inner_text():
        fail("owner", "invite in place", "the invite message isn't in the Invite card")
    page.goto(url("/_box/settings"))
    if "tab=settings" not in page.url:
        fail("owner", "tabs", f"/_box/settings didn't open the Settings tab: {page.url}")
    s.drain("owner in place")
    s.close()
    RUNNING.clear()
    RUNNING.update(saved)


def tailnet_view():
    """The tailnet page (port 8090, gate=False): same start card, no device-link, no /_box/ links (404 there)."""
    alice = {"id": "nAlice1", "display": "Alice A", "login": "alice@github", "node": "laptop"}

    class Tailnet(w.Handler):
        def ident(self):
            return "100.64.0.9", dict(alice)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Tailnet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    w.ALLOWED_HOSTS.add("127.0.0.1")
    import urllib.error
    import urllib.request
    for app, want in ((False, ["Open T3"]), (True, ["Open T3"])):
        ACCOUNTS["alice"]["app"] = app
        try:
            body = urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/me", timeout=60).read().decode()
            acct = urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/me?tab=account", timeout=60).read().decode()
        finally:
            ACCOUNTS["alice"]["app"] = False
        where = "tailnet /me" + (" (app connected)" if app else "")
        if "device-link" in body:
            fail("alice (tailnet)", where, "device-link button (refused in Cloudflare mode)")
        rel = re.findall(r"""href=['"](/_box/[^'"]*)""", body + acct)
        if rel:
            fail("alice (tailnet)", where, f"relative /_box/ links (404 on the tailnet page): {rel}")
        if "href='/me?tab=account' data-tab='account'" not in body:
            fail("alice (tailnet)", where, "the tab bar doesn't link to /me?tab=...")
        if f"href='{PUBLIC}/'" not in acct or "Without Tailscale" not in acct:
            fail("alice (tailnet)", where, "no absolute 'Without Tailscale' link to the public site")
        if re.search(rf"(//|@){re.escape(HOSTNAME)}\b", re.sub(r"<header>.*?</header>", "", body, flags=re.S)):
            fail("alice (tailnet)", where, "an address uses the box's name")
        start = body[body.find("id='start'"):body.find("</section>")]
        start = re.sub(r"<div class='narrow'>.*?<!--/narrow-->", "", start, flags=re.S)  # phones only
        mains = re.findall(r"<button(?![^>]*class=)[^>]*>([^<]*)</button>|<a class='btn' [^>]*>([^<]*)</a>", start)
        got = [a or b for a, b in mains]
        if got != want:
            fail("alice (tailnet)", where, f"start card main buttons {got}, expected {want}")
    # no JavaScript on the tailnet page: post/redirect/get to the same tab, the result inside the card
    tok = w.token_for("100.64.0.9", alice["id"])

    class NoFollow(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    op = urllib.request.build_opener(NoFollow)
    body = urllib.parse.urlencode({"token": tok, "action": "where", "op": "set", "status": "Lunch"}).encode()
    try:
        op.open(urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/me", data=body), timeout=60)
        loc = ""
    except urllib.error.HTTPError as ex:
        loc = ex.headers.get("Location") or ""
    if not re.fullmatch(r"/me\?r=[\w-]+&tab=home#people", loc):
        fail("alice (tailnet)", "status without JS", f"redirect {loc!r}")
    else:
        page = urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}{loc.split('#')[0]}", timeout=60).read().decode()
        card = page[page.find("id='people'"):]
        if "Status saved" not in card[:card.find("</section>")]:
            fail("alice (tailnet)", "status without JS", "the result isn't inside the Who's here card")
    srv.shutdown()


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
        start_cards(browser)  # the start card's states, the setup prompt, the tailnet page: in every mode
        tailnet_view()
        boxes_card(browser)
        running_card(browser)
        in_place(browser)
        w.portal_rate.clear()
        reg = sb.registry()  # bob's first visit (flow_login) must still see the welcome page
        reg["users"]["bob"]["welcome"] = True
        save_registry(reg)
        g._users = (0.0, {})
        if SHOTS_ONLY:
            browser.close()
            print(f"\nscreenshots in {SHOTS}; {len(FAILS)} problem(s)")
            sys.exit(1 if FAILS else 0)
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
