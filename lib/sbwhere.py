"""Presence (`box where`): where is someone, in their own words, plus a coarse "active on the box" hint.

  box where                         everyone
  box where mohammad                one person, by account or display name
  box where --set "In class" --until 15:00     your status (--clear to remove it)
  box where --name "Mohammad"       the name people (and their agents) ask for
  box where --private / --public    hide or show the automatic activity hint

STATE_DIR/presence.json (root-owned, 0644, readable by every account):
  {user: {"name", "status", "until", "set", "private", "active": {"last": ts, "checked": ts}}}

People set only their own entry: requests go through the member drop box, whose file owner is the identity.
The activity hint is root's sharedbox-janitor tick (every 10 min): the newest change among the account's agent
session files (Claude Code, Codex, opencode). It's shown rounded ("active now", "3h ago"), never as exact times,
and never anything about what they work on. Stdlib only.
"""

import fcntl
import json
import os
import stat
import time

import sbmsg

STATUS_MAX = 80
NAME_MAX = 40
ACTIVE_NOW = 15 * 60
SESSION_DIRS = (".claude/projects", ".codex/sessions", ".local/share/opencode/storage")


def path():
    import sbcommon as sb
    return os.path.join(sb.STATE_DIR, "presence.json")


def load(p=None):
    try:
        with open(p or path()) as f:
            d = json.load(f)
        return {u: r for u, r in d.items() if isinstance(r, dict)} if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _update(fn, p=None, as_root=True):
    """Read-modify-write presence.json under a lock (the drop handler and the janitor both write it)."""
    import sbcommon as sb
    p = p or path()
    lock = os.open(p + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        d = load(p)
        out = fn(d)
        sb.write_json(p, d, mode=0o644, owner=(0, 0) if as_root else None)
        return out
    finally:
        os.close(lock)


def _line(text, cap):
    t, n = sbmsg.scrub_title(text)
    if len(t) > cap:
        t = t[:cap - 3].rstrip() + "..."
    return t, n


def handle(user, req, now=None, p=None, as_root=True):
    """Root: apply one `box where --set/--clear/--name/--private` request from `user`."""
    now = time.time() if now is None else now
    change = {}
    if req.get("clear"):
        change.update(status=None, until=None)
    if req.get("status") is not None:
        status, masked = _line(req["status"], STATUS_MAX)
        if masked:
            raise sbmsg.MsgError("that status looks like it has a secret in it; leave it out")
        if not status:
            raise sbmsg.MsgError("status: a few words, like \"In class\" or \"Asleep, back 9am\"")
        change.update(status=status, until=None)
    until = req.get("until")
    if until is not None:
        if isinstance(until, bool) or not isinstance(until, (int, float)) or not (now < until <= now + 60 * 86400):
            raise sbmsg.MsgError("until: must be in the next 60 days")
        if not change.get("status"):
            raise sbmsg.MsgError("until goes with a status (--set TEXT --until WHEN)")
        change["until"] = int(until)
    if req.get("name") is not None:
        name, _ = _line(req["name"], NAME_MAX)
        change["name"] = name or None
    if req.get("private") is not None:
        change["private"] = bool(req["private"])
    if not change:
        raise sbmsg.MsgError("nothing to change")

    def apply(d):
        rec = d.setdefault(user, {})
        for k, v in change.items():
            if v is None:
                rec.pop(k, None)
            else:
                rec[k] = v
        if "status" in change or "until" in change:
            rec["set"] = int(now)
        if rec.get("private"):
            rec.pop("active", None)
        return dict(rec)
    rec = _update(apply, p, as_root)
    return {"ok": True, "you": view(user, rec, now), "message": describe(user, rec, now)}


# ---------- activity hint (root, janitor tick) ----------

def newest_session_change(home, budget=20000):
    """Newest mtime among agent session files under `home` (not following symlinks; bounded)."""
    best = 0.0
    seen = 0
    for rel in SESSION_DIRS:
        stack = [os.path.join(home, rel)]
        depth = {stack[0]: 0}
        while stack and seen < budget:
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    seen += 1
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        if depth[d] < 4:
                            depth[e.path] = depth[d] + 1
                            stack.append(e.path)
                    elif stat.S_ISREG(st.st_mode):
                        best = max(best, st.st_mtime)
    return int(best)


def refresh_activity(homes, now=None, p=None, as_root=True):
    """homes = {user: home}. Records the activity hint for everyone who isn't private."""
    now = time.time() if now is None else now
    found = {u: newest_session_change(h) for u, h in homes.items()}

    def apply(d):
        for u, last in found.items():
            rec = d.setdefault(u, {})
            if rec.get("private"):
                rec.pop("active", None)
                continue
            prev = (rec.get("active") or {}).get("last") or 0
            rec["active"] = {"last": max(last, prev) if last else prev, "checked": int(now)}
        for u in [u for u in d if u not in homes]:
            del d[u]  # accounts that are gone
    _update(apply, p, as_root)


# ---------- reading ----------

def ago(ts, now):
    if not ts:
        return "no agent activity seen"
    s = now - ts
    if s < ACTIVE_NOW:
        return "active now"
    if s < 3600:
        return "active within the hour"
    if s < 86400:
        return f"active {int(s // 3600)}h ago"
    return f"last active {int(s // 86400)} day{'s' if s >= 172800 else ''} ago"


def view(user, rec, now):
    """The public shape of one entry (expired statuses dropped, activity rounded)."""
    out = {"user": user, "name": rec.get("name") or user}
    if rec.get("status") and not (rec.get("until") and rec["until"] < now):
        out["status"] = rec["status"]
        if rec.get("until"):
            out["until"] = rec["until"]
            out["until_text"] = sbmsg.fmt_due(rec["until"], now)
        out["since"] = rec.get("set")
    if rec.get("private"):
        out["activity"] = "private"
    else:
        last = (rec.get("active") or {}).get("last")
        out["activity"] = ago(last, now) if rec.get("active") else "not checked yet"
        out["active_now"] = bool(last and now - last < ACTIVE_NOW)
    return out


def describe(user, rec, now):
    v = view(user, rec, now)
    who = v["name"] if v["name"] == user else f"{v['name']} ({user})"
    if v.get("status"):
        s = v["status"] + (f", until {v['until_text']}" if v.get("until") else "")
    else:
        s = "no status set"
    return f"{who}: {s}. On the box: {v['activity']}."


def find(query, people, d, aliases=None):
    """Accounts matching `query` by account, display name or alias (GitHub login, email name): exact matches
    first, then prefixes of any word. Case-insensitive."""
    q = " ".join(str(query).lower().split())
    if not q:
        return []
    aliases = aliases or {}
    words = {u: [u.lower(), (d.get(u, {}).get("name") or "").lower(), *aliases.get(u, [])] for u in people}
    exact = [u for u in people if q in words[u]]
    if exact:
        return exact
    return [u for u in people if any(w.startswith(q) for x in words[u] for w in [x, *x.split()] if w)]


# ---------- CLI ----------

def cli(a, sb, cfg, user, request):
    import sys
    now = time.time()

    def done(res):
        if a.json:
            print(json.dumps(res))
            sys.exit(0 if res.get("ok") else 1)
        if not res.get("ok"):
            sys.exit(f"box where: {res.get('error', 'failed')}")

    req = {"action": "presence"}
    try:
        if a.set is not None:
            req["status"] = a.set
        if a.until:
            if a.set is None:
                done({"ok": False, "error": "--until goes with --set TEXT"})
            req["until"] = sbmsg.parse_due(a.until, now)
    except ValueError as e:
        done({"ok": False, "error": str(e).replace("due:", "until:")})
    if a.clear:
        req["clear"] = True
    if a.name is not None:
        req["name"] = a.name
    if a.private or a.public:
        req["private"] = bool(a.private)
    if len(req) > 1:
        if a.who:
            done({"ok": False, "error": "you can only change your own status"})
        res = request(req)
        done(res)
        print(res.get("message", "done"))
        return
    people = sbmsg.recipients(sb, cfg)
    d = load()
    if a.who:
        hits = find(a.who, people, d, sbmsg.aliases(sb))
        if not hits:
            done({"ok": False, "error": f"nobody called {a.who!r} here; `box where` lists everyone"})
    else:
        hits = people
    views = [view(u, d.get(u, {}), now) for u in hits]
    if a.json:
        print(json.dumps({"ok": True, "people": views}))
        return
    for u in hits:
        print(describe(u, d.get(u, {}), now))
    if not a.who and not (d.get(user) or {}).get("status"):
        print('\nset yours: box where --set "In class" --until 15:00   (and --name "Your name")')


def add_parsers(s, sb, cfg, user, request):
    x = s.add_parser("where", help="where someone is: their status and whether they're active (box help where)")
    x.add_argument("who", nargs="?", help="account or display name; everyone without it")
    x.add_argument("--set", metavar="TEXT", help="your status, a few words (\"In class\", \"Asleep\")")
    x.add_argument("--until", metavar="WHEN", help="when it ends: 15:00, +2h, tomorrow 9am, fri")
    x.add_argument("--clear", action="store_true", help="remove your status")
    x.add_argument("--name", help="your display name, what people ask for (\"Mohammad\")")
    g = x.add_mutually_exclusive_group()
    g.add_argument("--private", action="store_true", help="hide whether you're active on the box")
    g.add_argument("--public", action="store_true", help="show it again")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=lambda a: cli(a, sb, cfg, user, request))
