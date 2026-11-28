"""A whole treehouse box with only the root boundary faked: the REAL sharedbox-gate and sharedbox-web, in-process.

    import fakebox as fb   # after setting FAKEBOX_PUBLIC (and optionally FAKEBOX_TMP_PREFIX) in os.environ

Importing it builds a throwaway box and starts it:
- a temp STATE_DIR and sharedbox.conf (PUBLIC_MODE=cloudflare, PUBLIC_URL=$FAKEBOX_PUBLIC), never the real ones;
- fake friends (alice, bob, dave, erin) that aren't Linux users, the owner as this process's own account;
- sharedbox-web's spool_call answered in-process by fake_root() (what root's provisioner would say: status,
  logins, servers, messages, Telegram, teammates, requests, invites) with in-memory state that buttons change;
- Cloudflare Access faked: the gate's verifier accepts the tokens in TOKENS ("tok-alice" -> alice@x.io, ...);
- fake T3 Code servers on each account's port, the friends' apps and the group server, and a peer machine;
- the gate on 127.0.0.1:GATE_PORT, in an asyncio loop on its own thread (LOOP), relaying to sharedbox-web's unix socket.

Nothing reads or writes the real box: no /var/lib/sharedbox, no /etc/sharedbox, no real spool or socket.
Used by tests/e2e_browser.py (a TLS front + Chromium on top) and tests/demo_site.py (a clickable demo).
reset() puts every fake back to its starting state; RESET_HOOKS run after it (the demo adds data there).
"""
import asyncio
import hashlib
import html as htmlmod
import importlib.machinery
import importlib.util
import json
import os
import pwd
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WT = os.path.abspath(os.environ.get("FAKEBOX_REPO") or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


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


TMP = tempfile.mkdtemp(prefix=os.environ.get("FAKEBOX_TMP_PREFIX") or "fakebox-")
STATE = os.path.join(TMP, "state")
os.makedirs(os.path.join(STATE, "health"))
os.makedirs(os.path.join(STATE, "gate", "invites"))
os.makedirs(os.path.join(STATE, "spool"))
GATE_PORT = free_port()
PORT_BASE = free_block(80)
OWNER_T3_PORT = PORT_BASE + 75
SHARED_BASE = PORT_BASE + 60
TS_IP = "100.64.0.5"  # the box's Tailscale IP in every address a friend sees (never its short name)
PUBLIC = os.environ.get("FAKEBOX_PUBLIC") or "https://treehouse.test"
PUBLIC_HOST = urllib.parse.urlsplit(PUBLIC).netloc
ME = pwd.getpwuid(os.getuid()).pw_name  # the owner's own account must exist on the system
UIDS = {"alice": 2000, "bob": 2001, "carol": 2002, "dave": 2003, "erin": 2004}
PORT_OF = {u: PORT_BASE + (uid - 2000) * 10 for u, uid in UIDS.items()}
CONF = os.path.join(TMP, "sharedbox.conf")


def write_conf():
    with open(CONF, "w") as f:
        f.write(f"PUBLIC_URL={PUBLIC}\nPUBLIC_MODE=cloudflare\nCF_TEAM_DOMAIN=x.cloudflareaccess.com\n"
                f"CF_ACCESS_AUD=aaaaaaaaaaaaaaaaaaaa\nGATE_BACKEND_HOST=127.0.0.1\nCF_OWNER_EMAILS=owner@x.io\n"
                f"PORT_BASE={PORT_BASE}\nPORT_BLOCK=10\nFRIEND_UID_MIN=2000\nOWNER_T3_PORT={OWNER_T3_PORT}\n"
                f"OWNER_T3_USER={ME}\nADMIN_USERS={ME}\nSHARED_PUBLIC_LINKS=yes\nBRAND=treehouse\n"
                "UPDATE_SOURCE=https://github.com/example/treehouse\n"
                "SSH_HOST=treehouse-ssh.test.io\nCF_SSH_APP_ID=sshapp12345\n"
                f"BOXES=treebox={TS_IP} minastirith=100.64.0.6 krishbox=100.64.0.7\n")


write_conf()
sock_dir = tempfile.mkdtemp(prefix="sbw-", dir="/tmp" if len(TMP) > 60 else None)  # AF_UNIX path limit
os.environ.update(SHAREDBOX_CONF=CONF, SHAREDBOX_LIB=os.path.join(WT, "lib"),
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

G = 1 << 30
M = 1 << 20
LABELS = {"claude": "Claude Code", "codex": "Codex", "gh": "GitHub", "t3connect": "T3 Connect",
          "antigravity": "Google (Antigravity)"}
TOKENS = {"tok-alice": "alice@x.io", "tok-bob": "bob@x.io", "tok-owner": "owner@x.io",
          "tok-stranger": "nobody@x.io", "tok-new": "new@x.io", "tok-dave": "dave@x.io", "tok-erin": "erin@x.io"}
INVITE_TOK = "I" * 43
BOX_NAME = "mybox"  # this machine's name in root's answers (the demo calls it treebox, like BOXES)

# The fakes' state. reset() fills these in place (never rebinds them), so `from fakebox import *` stays live.
NOW = int(time.time())
ACCOUNTS, PROJECTS, RUNNING, TEAM, WHERE, TG, MSGS, SENT = {}, {}, {}, {}, {}, {}, {}, {}
SHARED, PEOPLE = [], []
FINISHED = set()  # (user, key): a sign-in someone finished outside the site (the demo's "pretend" button)
PENDING = {}  # (user, key) -> where the sign-in that's waiting applies: [None] = this machine, else machine names
PAIR_TOKENS = set()
ROOT_LOG = []
RESET_HOOKS = []
PEER = {}  # what the peer machine (minastirith) answers on /api/box
lock = threading.RLock()


def write(rel, obj):
    with open(os.path.join(STATE, rel), "w") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj))


def save_registry(reg):
    write("users.json", reg)


def fill(target, new):
    if isinstance(target, dict):
        target.clear()
        target.update(new)
    else:
        target[:] = new


def fake_friend_users(reg=None):
    out = {}
    for n, r in (reg or sb.registry()).get("users", {}).items():
        if n in UIDS:
            out[n] = dict(r, uid=UIDS[n], gid=UIDS[n], home=f"/home/{n}")
    return out


sb.friend_users = fake_friend_users  # the only system lookup we replace: these accounts aren't real Linux users


def reset():
    """Every fake back to its starting state: the state files root and the janitor/health jobs write, the conf,
    and what fake_root remembers. Then RESET_HOOKS."""
    global NOW
    with lock:
        NOW = int(time.time())
        _reset_files()
        _reset_memory()
        FINISHED.clear()
        PENDING.clear()
        PAIR_TOKENS.clear()
        w.portal_rate.clear()
        w.rate.clear()
        g._users = (0.0, {})
        for hook in RESET_HOOKS:
            hook()


def _reset_files():
    write_conf()
    for f in os.listdir(os.path.join(STATE, "gate", "invites")):
        os.unlink(os.path.join(STATE, "gate", "invites", f))
    save_registry({"users": {
        "alice": {"email": "alice@x.io", "status": "active", "created": NOW - 40 * 86400, "ts_id": "nAlice1"},
        "bob": {"email": "bob@x.io", "status": "active", "created": NOW - 3600, "welcome": True, "ts_id": "nBob1"},
        "dave": {"email": "dave@x.io", "status": "active", "created": NOW - 9 * 86400},  # joined through Cloudflare only
        "erin": {"email": "erin@x.io", "status": "active", "created": NOW - 2 * 86400, "ts_id": "nErin1"},
    }})
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
        {"ts": NOW - 2000, "kind": "owner_paired", "user": "bob"},
        {"ts": NOW - 1000, "kind": "something_new", "user": "alice", "detail": "x"},
    ]) + "\n")
    write("requests.json", {"requests": [
        {"id": "r-1", "user": "alice", "kind": "install", "packages": ["ffmpeg"], "why": "video bot", "ts": NOW - 3600,
         "state": "waiting", "reason": "not on the auto-install list"},
        {"id": "r-2", "user": "bob", "kind": "other", "text": "Please open port 25\nfor my mail bot.\n\nThanks!",
         "ts": NOW - 600, "state": "new"},
    ]})
    write("shared.json", {s_["name"]: {"port": SHARED_BASE + i, "cmd": ["node", "server.js"]}
                          for i, s_ in enumerate([{"name": "web"}, {"name": "down"}])})
    write(f"gate/invites/{sb.token_hash(INVITE_TOK)}.json",
          {"hash": sb.token_hash(INVITE_TOK), "email": "new@x.io", "expires": NOW + 86400})


def _reset_memory():
    fill(ACCOUNTS, {  # what `box _portal status` would say for each account
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
    })
    fill(PROJECTS, {  # what `box _portal status` says under "projects" (lib/sbprojects.py summary), per account
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
    })
    running = {  # what `box _portal status` says under "running" (lib/sbrunning.py), biggest first
        "alice": [
            {"key": "c:shop-db", "kind": "database", "name": "shop-db", "detail": "Supabase · 5 containers",
             "mem": int(1.4 * G), "age": 3 * 86400 + 7200, "pids": 40, "stop": "containers", "cpu_usec": 3 * 86400 * 40_000},
            {"key": "", "kind": "agent", "name": "Claude session", "detail": "claude, node, python", "mem": 900 * M,
             "age": 5400, "pids": 12, "stop": None, "cpu_usec": 5400 * 50_000},
            {"key": "u:sb-blog.service", "kind": "server", "name": "blog", "detail": "node", "mem": 220 * M,
             "age": 86400 * 2, "pids": 3, "stop": "server", "cpu_usec": 1000, "keep": True,
             "about": "Alice's blog about the hackathon, read by the whole class"},
            {"key": "u:sb-t3.service", "kind": "t3", "name": "T3 Code", "detail": "node", "mem": 180 * M,
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
    running[ME] = [dict(x, name=x["name"].replace("shop-db", "uoft-index")) for x in running["alice"]] + [
        {"key": f"u:sb-s{i}.service", "kind": "server", "name": f"small-server-{i}", "detail": "python3",
         "mem": (9 - i) * M, "age": 3600 * i, "pids": 1, "stop": "server", "cpu_usec": 0,
         "exec": f"/usr/bin/python3 -m http.server 90{i}"} for i in range(1, 6)]
    fill(RUNNING, running)
    fill(TEAM, {"alice": {"bob": "2026-11-19"}})  # alice already lets bob's agents into her T3 (to -> since)
    fill(SHARED, [{"name": "web", "state": "running", "port": SHARED_BASE, "created_by": "alice", "created": NOW - 86400,
                   "updated_by": "bob", "updated": NOW - 600,
                   "desc": "Discord bot that posts the week's hackathon schedule <b>every Monday</b>"},
                  {"name": "down", "state": "stopped", "port": SHARED_BASE + 1, "created_by": "bob", "created": NOW - 3600}])
    fill(PEOPLE, ["alice", "bob", ME])  # who shows in Who's here and the message picker
    fill(WHERE, {"alice": {"name": "Alice", "status": "At the hackathon", "until_text": "today 18:00", "act": "active now"},
                 "bob": {"act": "active 3h ago"}})
    fill(TG, {"ready": True, "linked": {"bob"}, "notify": {}})
    fill(MSGS, {"alice": [
        {"id": "18f0aa000001", "from": "bob", "to": "alice", "title": "Fix the login page before the demo", "level": "high",
         "due_text": "today 18:00", "ts": NOW - 3600,
         "context": "The demo starts at 6. Login 500s on Safari; done when it works there.", "read": False},
        {"id": "18f0aa000002", "from": "bob", "to": "alice", "title": "Can you review my PR?", "level": "normal",
         "ts": NOW - 86400, "context": "", "read": True}]})
    fill(SENT, {"alice": [{"id": "18f0aa000009", "to": "bob", "title": "Deploy is green", "ts": NOW - 600,
                           "seen": NOW - 300, "answer": ""}]})
    fill(PEER, {"v": 1, "name": "minastirith", "version": "abc1234", "ncpu": 8, "load1": 1.25,
                "level": "calm", "mem": 42.0, "disk_free": 81.0, "dashboard": "http://100.64.0.6:8090/",
                "t3": "", "cpu": 23.0, "mem_used_b": 13 * 2**30, "mem_total_b": 31 * 2**30,
                "disk_free_b": 370 * 2**30, "disk_total_b": 456 * 2**30, "people": ["alice", "dave"]})


# ---------------- fake Cloudflare and the fake root boundary ----------------


class FakeVerifier:
    def verify(self, tok):
        if tok not in TOKENS:
            raise sbaccess.InvalidToken("nope")
        return {"email": TOKENS[tok], "exp": 4e9}


g.VERIFIER = FakeVerifier()


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


_msg_seq = [0x18f0aa000100]


def fake_msg(name, req):
    op = req.get("op")
    box = MSGS.setdefault(name, [])
    if op == "inbox":
        return {"ok": True, "messages": list(box), "open": len(box), "people": list(PEOPLE), "you": name,
                "sent": list(SENT.get(name, []))}
    if op == "send":
        if not req.get("title") or not req.get("context"):
            return {"ok": False, "error": "title: required"}
        to = req.get("to")
        if to not in PEOPLE or to == name:
            return {"ok": False, "error": "to: pick someone on this box"}
        _msg_seq[0] += 1
        mid = f"{_msg_seq[0]:x}"
        level = req.get("level") if req.get("level") in ("high", "normal", "low") else "normal"
        MSGS.setdefault(to, []).insert(0, {"id": mid, "from": name, "to": to, "title": req["title"], "level": level,
                                           "ts": int(time.time()), "context": req["context"], "read": False,
                                           **({"due_text": req["due"]} if req.get("due") else {})})
        SENT.setdefault(name, []).insert(0, {"id": mid, "to": to, "title": req["title"], "ts": int(time.time()),
                                             "seen": None, "answer": ""})
        return {"ok": True, "message": f"sent {mid} to {to}"}
    m = next((x for x in box if x["id"] == req.get("id")), None)
    if not m:
        return {"ok": False, "error": "that message isn't in your inbox any more"}
    if op == "reply" and not req.get("text"):
        return {"ok": False, "error": "write a reply first"}
    m["read"] = True
    sent = next((s for s in SENT.get(m.get("from"), []) if s["id"] == m["id"]), None)
    if sent is not None:
        sent["seen"] = int(time.time())
        sent["answer"] = req.get("text") if op == "reply" else "done" if op == "done" else sent["answer"]
    if op in ("done", "archive"):
        box.remove(m)
    return {"ok": True, "message": {"done": "Marked done; they'll see it.", "reply": "Reply sent.",
                                    "archive": "Cleared from your inbox."}[op]}


def _running_key(server):
    return f"u:sb-{server}.service"


def _sync_running(name, srv):
    """A friend's server going up or down shows in their Running list too (both come from systemd on a real box)."""
    items = RUNNING.setdefault(name, [])
    it = next((i for i in items if i["key"] == _running_key(srv["name"])), None)
    if srv["state"] == "running" and not it:
        items.append({"key": _running_key(srv["name"]), "kind": "server", "name": srv["name"], "detail": "server",
                      "mem": 60 * M, "age": 1, "pids": 1, "stop": "server", "cpu_usec": 0,
                      **{k: srv[k] for k in ("keep", "about", "exec") if srv.get(k)}})
        items.sort(key=lambda i: (i["kind"] == "background", -i["mem"]))
    elif srv["state"] != "running" and it:
        items.remove(it)


def _set_box_login(name, box, key, on):
    b = ACCOUNTS[name].setdefault("boxes", {}).setdefault(box, {"ssh": True, "logins": {}})
    b.setdefault("logins", {})[key] = on
    if not on:
        b.get("accounts", {}).pop(key, None)
        if key == "claude":
            b.pop("claude_own", None)


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
    _finish_outside(name)
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
                  "items": [dict(i) for i in RUNNING.get(n, [])]} for n in [ME] + [p for p in PEOPLE if p != ME]]
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
        _server_stopped(str(req.get("user")), it)
        ROOT_LOG.append(("running-stop-any", req.get("user"), req.get("key"), bool(req.get("force"))))
        return {"ok": True, "message": f"{req.get('user')}: Stopped {it['name']}."}
    if action == "serve-set":
        srv = next((x for x in ACCOUNTS[name]["servers"] if x["name"] == req.get("key")), None)
        if not srv:
            return {"ok": False, "error": "no such server"}
        it = next((i for i in RUNNING.get(name, []) if i["key"] == _running_key(srv["name"])), {})
        if req.get("what") == "keep":
            srv["keep"] = it["keep"] = req.get("value") == "on"
            return {"ok": True, "message": f"{srv['name']} is marked keep running." if srv["keep"] else
                    f"{srv['name']} is no longer marked keep running."}
        srv["hackathon"] = "pause" if req.get("value") == "pause" else ""
        return {"ok": True, "message": f"{srv['name']} will be stopped while hackathon mode is on." if srv["hackathon"]
                else f"{srv['name']} keeps running during hackathons."}
    if action == "logout":  # this machine
        acct = ACCOUNTS[name]
        acct["logins"][req.get("key")] = False
        acct.get("accounts", {}).pop(req.get("key"), None)
        return {"ok": True, "message": f"Signed out on {BOX_NAME}. You can sign in again with any account."}
    if action == "peer-logout":
        _set_box_login(name, req.get("box"), req.get("key"), False)
        return {"ok": True, "message": f"Signed out on {req.get('box')}. You can sign in again with any account."}
    if action == "peer-code":
        _set_box_login(name, req.get("box"), req.get("key"), True)
        return {"ok": True, "done": True, "message": f"Signed in on {req.get('box')}."}
    if action == "running-stop":  # `box _portal running-stop KEY`: only a live, stoppable item of this account
        items = RUNNING.get(name, [])
        it = next((i for i in items if i["key"] == req.get("key") and i["stop"]), None)
        if not it:
            return {"ok": False, "error": "That isn't running any more (or it can't be stopped from here). Refresh the page."}
        items.remove(it)
        _server_stopped(name, it)
        return {"ok": True, "message": f"Stopped {it['name']}. It stays off, reboots included, until someone starts it again."}
    if action == "msg":
        return fake_msg(name, req)
    if action == "where":
        if req.get("op") == "set":
            if req.get("clear"):
                WHERE.get(name, {}).pop("status", None)
                WHERE.get(name, {}).pop("until_text", None)
                return {"ok": True, "message": "Status cleared."}
            if not str(req.get("status") or "").strip():
                return {"ok": False, "error": "status: a few words, like \"In class\""}
            WHERE.setdefault(name, {})["status"] = req["status"]
            WHERE[name].pop("until_text", None)
            if str(req.get("until") or "").strip():
                WHERE[name]["until_text"] = str(req["until"]).strip()
            return {"ok": True, "message": f"Status saved. {name}: {req['status']}."}
        if req.get("op") == "private":
            WHERE.setdefault(name, {})["private"] = bool(req.get("private"))
            return {"ok": True, "message": "Hidden." if req.get("private") else "Shown again."}
        people = []
        for u in PEOPLE:
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
            PENDING[(name, "telegram")] = ["telegram"]
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
            TG["ready"] = False
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
                                               "url": "https://claude.ai/oauth/authorize?code=true" if key_ == "claude"
                                               else "https://accounts.google.com/o/oauth2/auth?access_type=offline"}]}
        rows = []
        if action == "peer-login":
            PENDING[(name, key_)] = sorted(boxes)
        for b in sorted(boxes):
            done = action == "peer-check" and bool(boxes[b].get("logins", {}).get(key_))
            rows.append({"box": b, "done": True} if done else
                        {"box": b, "done": False, "url": "https://accounts.t3.codes/device?user_code=WXYZ-1234",
                         "code": "WXYZ-1234"} if action == "peer-login" else {"box": b, "done": False})
        return {"ok": True, "boxes": rows}
    if action == "team":  # teammates' agents in your T3 (root: handle_team)
        mates = [u for u in UIDS if u != name]
        grants = TEAM.setdefault(name, {})
        op = req.get("op")
        if op == "list":
            return {"ok": True, "people": mates, "access": {"read-only": "Look only", "approval-required":
                    "Ask before changes", "full-access": "Full access"},
                    "grants": [{"to": t, "session": "s-" + t, "since": since, "last": ""} for t, since in grants.items()]}
        if req.get("to") not in mates:
            return {"ok": False, "error": "pick a teammate"}
        if op == "grant":
            if req.get("access") not in ("read-only", "approval-required", "full-access"):
                return {"ok": False, "error": "pick what their agents may do"}
            grants.setdefault(req["to"], time.strftime("%Y-%m-%d"))
            return {"ok": True, "message": f"{req['to']}'s agents can now work in your T3."}
        if op == "revoke":
            grants.pop(req["to"], None)
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
                "git": {"name": name, "email": ""}, "ssh": acct["ssh"], "box": BOX_NAME,
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
            s.update(updated_by=name, updated=int(time.time()))
            return {"ok": True, "message": {"start": "started", "stop": "stopped", "restart": "restarted"}[op]
                    + f" {key}", "name": key, "op": op}
        _sync_running(name, s)
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
        if not err:
            acct["ssh"] = True
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
        PENDING[(name, key)] = [None]
        if key == "codex":
            return {"ok": True, "url": "https://auth.openai.com/codex/device", "code": "ABCD-1234", "paste": False}
        return {"ok": True, "url": "https://github.com/login/device", "code": "ABCD-1234", "paste": False}
    if action == "code":
        acct["logins"][key] = True
        return {"ok": True, "done": True}
    if action == "check":
        if acct["logins"].get(key):  # they finished the device-code page
            return {"ok": True, "done": True}
        return {"ok": False, "error": f"{LABELS[key]} login hasn't finished: Waiting for you to sign in"}
    return {"ok": False, "error": "unknown action"}


def _finish_outside(name):
    """Sign-ins this person finished on another site since the last call (on a real box root sees the login
    tool exit; Telegram's bot sees /start): apply them where they were started."""
    for n, key in [f for f in FINISHED if f[0] == name]:
        FINISHED.discard((n, key))
        for where in PENDING.pop((n, key), []):
            if where == "telegram":
                TG["linked"].add(n)
            elif where is None:
                ACCOUNTS[n]["logins"][key] = True
            else:
                _set_box_login(n, where, key, True)


def _server_stopped(name, item):
    """Stopping a server from the Running list stops the server itself (its card says stopped)."""
    if item.get("kind") != "server":
        return
    for s in (ACCOUNTS.get(name) or {}).get("servers", []):
        if _running_key(s["name"]) == item["key"]:
            s["state"] = "stopped"


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
                return self.reply(200, "<!doctype html><title>Fake T3 Code</title>"
                                  "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                                  "<body style='font-family:system-ui,sans-serif;margin:24px'>"
                                  f"<h1>T3 on {self.server.server_port}</h1><p>This page stands in for T3 Code, where "
                                  "your AI agents work. On a real box you'd see your projects and chats here.</p>"
                                  "<p><a href='/_box/me'>Back to the dashboard</a></p>")
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
        data = (f"<!doctype html><title>App {self.server.server_port}</title>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<body style='font-family:system-ui,sans-serif;margin:24px'>"
                f"<p>This page stands in for a web app running on port {self.server.server_port}.</p>"
                f"<p>path {htmlmod.escape(self.path)}</p><a href='/_box/me'>dashboard</a>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class PeerBox(BaseHTTPRequestHandler):  # another treehouse box answering /api/box (the Machines tab)
    def log_message(self, *a):
        pass

    def do_GET(self):
        data = json.dumps(PEER).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(port, handler):
    s = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


reset()

for p in list(PORT_OF.values()) + [OWNER_T3_PORT]:
    serve(p, FakeT3)
for p in (PORT_OF["alice"] + 3, SHARED_BASE):  # "blog" (running) and group "web"; "bot"/"down" stay closed
    serve(p, AppServer)

PEER_PORT = free_port()
serve(PEER_PORT, PeerBox)
w.peer_url = lambda ip: f"http://127.0.0.1:{PEER_PORT if ip == '100.64.0.6' else free_port()}/api/box"  # "gone": refused


def refresh_peers():
    """Ask the other machines again (the web does this on a timer on a real box)."""
    w.peers_tick()
    for _ in range(100):
        if not any(p["busy"] for p in w.PEERS.values()):
            break
        time.sleep(0.05)


refresh_peers()
threading.Thread(target=w.serve_gate, daemon=True).start()

# ---------------- the gate, on its own asyncio loop ----------------

LOOP = asyncio.new_event_loop()


def _run_loop():
    asyncio.set_event_loop(LOOP)
    LOOP.run_until_complete(asyncio.start_server(g.client, "127.0.0.1", GATE_PORT, limit=g.MAX_HEAD))
    LOOP.run_forever()


threading.Thread(target=_run_loop, daemon=True).start()


def on_loop(coro):
    """Run a coroutine on the gate's loop (e.g. start another server next to it) and wait for it."""
    return asyncio.run_coroutine_threadsafe(coro, LOOP).result(30)


for _ in range(100):
    if os.path.exists(os.environ["SHAREDBOX_WEB_SOCK"]):
        break
    time.sleep(0.05)
