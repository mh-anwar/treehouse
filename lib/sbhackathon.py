"""`box hackathon check [--fix] [--json] [--repo NAME]`: is every machine in BOXES ready for a hackathon, for the
person running it? Per machine: reachable over SSH, T3 running (and linked into T3 here), AI sign-ins (Claude,
Codex, Antigravity), the hackathon project(s) there and in T3, and SSH from that machine to each of the others.

How it works:
- probe() below runs on every machine as this same person. Here it's called directly; on the others this file's own
  source is sent over SSH to `python3 -` (stdlib only, no treehouse imports needed), so the check works against any
  treehouse version there, including ones that predate this command.
- Everything a probe asks the machine goes through the person's own tools: `box login --json` (sign-ins: whether
  credential files exist, never their contents), /proc (is `t3 serve` running), systemd --user (T3's units), T3's
  own database read-only (which folders are projects), plain ssh with BatchMode (no prompts).
- assess() turns the probes into problems, each with either a safe automatic fix (`--fix` runs it) or the exact
  one-line command an agent should run next. Sign-ins are never copied: each machine keeps its own AI accounts.
Never acts as another account: all SSH is the caller's own, with the destinations `box projects` uses.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse

SIGNINS = ("claude", "codex", "antigravity")
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2"]
PROBE_TIMEOUT = 120  # one machine's whole probe, including its own SSH checks to the others


# ---------- the probe: runs on each machine as this person (keep it stdlib-only and self-contained) ----------

def _run(argv, timeout=60, input_text=None):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout,
                           input=input_text, stdin=None if input_text is not None else subprocess.DEVNULL)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as ex:
        return -1, "", type(ex).__name__


def _last_json(text):
    for line in reversed((text or "").strip().splitlines()):
        try:
            v = json.loads(line)
            if isinstance(v, dict):
                return v
        except ValueError:
            continue
    return {}


def _t3_serving(uid):
    """Is a `t3 serve` of this person's running (T3 itself, not just the socket that wakes it)?"""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if len(argv) > 1 and os.path.basename(argv[0]) == b"t3" and argv[1] == b"serve":
            return True
    return False


def _unit_state(unit):
    rc, out, _ = _run(["systemctl", "--user", "is-active", unit], 10)
    return out.strip() or "unknown"


def _unit_exists(unit):
    rc, out, _ = _run(["systemctl", "--user", "list-unit-files", "--no-legend", unit], 10)
    return rc == 0 and unit in out


def _t3_projects(home):
    """Folders that are projects in this person's T3 (T3's own database, read-only), or None when unknown."""
    import sqlite3
    db = os.path.join(os.environ.get("T3CODE_HOME") or os.path.join(home, ".t3"), "userdata", "statev2.sqlite")
    if not os.path.exists(db):
        return None
    try:
        con = sqlite3.connect(f"file:{urllib.parse.quote(db)}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute("SELECT workspace_root FROM projection_projects WHERE deleted_at IS NULL").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return {os.path.realpath(r[0]).rstrip("/") for r in rows if r and r[0]}


def _ssh_dest(name, ip, user, home, lib):
    """The destination `box projects` uses (treehouse's sbmulti.ssh_dest), else user@ip."""
    sys.path.insert(0, lib)
    try:
        import sbmulti
        return sbmulti.ssh_dest(name, ip, user, home)
    except Exception:  # noqa: BLE001 - an older or missing treehouse lib: the plain form still works for friends
        return f"{user}@{ip}"
    finally:
        sys.path.remove(lib)


def probe(req):
    """What this machine looks like for this person. req: {"peers": {name: ip} (the other machines), "repos": [NAME],
    "lib": treehouse's lib folder}. Never prints or returns a secret."""
    import pwd
    me = pwd.getpwuid(os.getuid())
    home, user = me.pw_dir, me.pw_name
    out = {"ok": True, "user": user, "signins": {}, "t3": "unknown", "t3_kind": "", "repos": {}, "ssh": {}}
    rc, so, _ = _run(["box", "login", "--json"], 90)
    logins = (_last_json(so).get("logins") or {}) if rc == 0 else {}
    out["signins_known"] = bool(logins)
    for k in SIGNINS:  # None: that agent isn't installed here
        v = logins.get(k)
        out["signins"][k] = bool(v.get("ok")) if isinstance(v, dict) else None
    sock, svc = _unit_exists("sb-t3.socket"), _unit_exists("t3code.service")
    out["t3_kind"] = "socket" if sock else "service" if svc else ""
    if _t3_serving(os.getuid()):
        out["t3"] = "running"
    elif sock and _unit_state("sb-t3.socket") == "active":
        out["t3"] = "asleep"  # the socket starts T3 on the first connection
    elif sock or svc:
        out["t3"] = "stopped"
    else:
        out["t3"] = "missing"
    projects = _t3_projects(home)
    for name in req.get("repos") or []:
        path = os.path.join(home, name)
        out["repos"][name] = {"present": os.path.isdir(os.path.join(path, ".git")),
                              "t3": None if projects is None else os.path.realpath(path).rstrip("/") in projects}
    peers = req.get("peers") or {}
    lib = req.get("lib") or "/usr/lib/sharedbox"

    dests = {n: _ssh_dest(n, ip, user, home, lib) for n, ip in peers.items()}

    def one(n):
        rc, _, _ = _run(["ssh", *SSH_OPTS, dests[n], "true"], 25)
        out["ssh"][n] = rc == 0
    threads = [threading.Thread(target=one, args=(n,), daemon=True) for n in sorted(peers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(40)
    for n in peers:
        out["ssh"].setdefault(n, False)
    return out


# ---------- the machine running the check ----------

def remote_script(req):
    """Python source that runs probe(req) on another machine and prints its JSON (fed to `ssh DEST python3 -`)."""
    with open(os.path.abspath(__file__)) as f:
        src = f.read()
    return src + f"\nprint(json.dumps(probe(json.loads({json.dumps(json.dumps(req))}))))\n"


def probe_remote(dest, req, timeout=PROBE_TIMEOUT):
    """(reachable, probe answer or {})."""
    rc, so, se = _run(["ssh", *SSH_OPTS, dest, "python3 -"], timeout, input_text=remote_script(req))
    if rc == 255 or rc == -1:
        return False, {"error": (se.strip().splitlines() or ["no answer"])[-1][:200]}
    ans = _last_json(so)
    if not ans:
        return True, {"error": "No answer from the check there: " + ((se.strip().splitlines() or ["?"])[-1][:150])}
    return True, ans


def t3_linked(envs, name, ip, now=None):
    """Is the machine `name` (Tailscale `ip`) one of T3's linked environments here, and not expired?"""
    now = time.time() if now is None else now
    for e in envs or []:
        if not isinstance(e, dict):
            continue
        hosts = {urllib.parse.urlsplit(str(u)).hostname or "" for u in e.get("urls") or []}
        if e.get("label") != name and ip not in hosts and not any(h == name or h.startswith(name + ".") for h in hosts):
            continue
        try:
            exp = time.mktime(time.strptime(str(e.get("expiresAt"))[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        except ValueError:
            return True  # no readable expiry: T3 lists it, so count it
        if exp > now:
            return True
    return False


def gather(boxes, me, repos, dests, lib, local_probe=probe, remote=probe_remote):
    """{machine: {"reachable", "probe"}} for every machine in BOXES, probed in parallel."""
    res = {}

    def one(n):
        req = {"peers": {p: ip for p, ip in boxes.items() if p != n}, "repos": list(repos), "lib": lib}
        if n == me:
            try:
                res[n] = {"reachable": True, "probe": local_probe(req)}
            except Exception as ex:  # noqa: BLE001 - report it as a problem rather than crash the whole check
                res[n] = {"reachable": True, "probe": {"error": str(ex)[:200]}}
            return
        ok, ans = remote(dests[n], req)
        res[n] = {"reachable": ok, "probe": ans}
    threads = [threading.Thread(target=one, args=(n,), daemon=True) for n in boxes]
    for t in threads:
        t.start()
    for t in threads:
        t.join(PROBE_TIMEOUT + 30)
    for n in boxes:
        res.setdefault(n, {"reachable": False, "probe": {"error": "The check there didn't finish in time."}})
    return res


def assess(res, me, boxes, repos, envs, chosen=(), named_repo=""):
    """Rows per machine: {"machine", "ok", "lines": [str], "problems": [{"what", "fix", "next"}]}. envs: T3's linked
    environments here (`t3 environment list --json`), None when T3 couldn't list them.
    fix: an automatic step for --fix, as ("here"|machine, argv). next: the exact command an agent runs next."""
    rows = []
    mine = ((res.get(me) or {}).get("probe") or {}).get("repos") or {}
    missing_here = {k for k in repos if not (mine.get(k) or {}).get("present")}
    for n in boxes:
        r = res.get(n) or {}
        p = r.get("probe") or {}
        here = n == me
        good, probs = [], []

        def prob(what, fix=None, nxt=""):
            probs.append({"what": what, "fix": fix, "next": nxt})
        if not r.get("reachable"):
            prob(f"can't reach it over SSH ({p.get('error') or 'no answer'})", ("here", ["box", "boxes", "setup"]),
                 "box boxes setup")
            rows.append({"machine": n, "here": here, "ok": False, "lines": [], "problems": probs})
            continue
        if p.get("error"):
            prob(p["error"])
            rows.append({"machine": n, "here": here, "ok": False, "lines": ["reachable"], "problems": probs})
            continue
        if not here:
            good.append("reachable")
        t3 = p.get("t3")
        if t3 in ("running", "asleep"):
            good.append("T3 " + ("running" if t3 == "running" else "asleep (wakes when opened)"))
        elif t3 == "stopped":
            fix = (["box", "_portal", "t3"] if p.get("t3_kind") == "socket"
                   else ["systemctl", "--user", "start", "t3code.service"])
            prob("T3 isn't running", (n, fix), "box hackathon check --fix")
        elif t3 == "missing":
            prob("T3 isn't set up for this account", None,
                 f'box request "set up T3 for {p.get("user") or "me"} on {n}"')
        if not here and envs is None:
            good.append("T3 link unknown (T3 here didn't list its links)")
        elif not here:
            if t3_linked(envs, n, boxes[n]):
                good.append("linked in T3 here")
            else:
                prob("not linked in T3 here", ("here", ["box", "boxes", "setup"]), "box boxes setup")
        if not p.get("signins_known"):
            prob("couldn't read its sign-ins (box login --json failed there)")
        else:
            missing, absent = [], []
            for k in SIGNINS:
                v = (p.get("signins") or {}).get(k)
                if v is None:
                    absent.append(k)
                elif not v:
                    missing.append(k)
            signed = [k for k in SIGNINS if (p.get("signins") or {}).get(k)]
            if signed:
                good.append("signed in: " + ", ".join(signed))
            for k in missing:
                prob(f"{k} not signed in", None, f"box login {k}" + ("" if here else f" --on {n}"))
            if absent:
                good.append("not installed: " + ", ".join(absent))
        for name in repos:
            rp = (p.get("repos") or {}).get(name) or {}
            if not here and name in missing_here:
                continue  # nothing to copy until it's back here; reported once, on this machine's row
            if not rp.get("present"):
                if here:
                    prob(f"project {name} isn't in ~ here, so it can't be copied", None,
                         f"box restore {name} (if archived), or box projects everywhere {name} off")
                else:
                    prob(f"project {name} not copied here yet", ("here", ["box", "projects", "sync"]),
                         "box projects sync")
            elif rp.get("t3") is False:
                prob(f"project {name} isn't in T3", ("here", ["box", "projects", "sync"]) if not here else None,
                     "box projects sync" if not here else f"t3 project add --title {name} ~/{name}")
            else:
                good.append(f"project {name}" + (" (in T3)" if rp.get("t3") else ""))
        if here and named_repo and named_repo not in chosen:
            prob(f"project {named_repo} isn't kept on all machines",
                 ("here", ["box", "projects", "everywhere", named_repo, "on"]),
                 f"box projects everywhere {named_repo} on")
        bad_ssh = sorted(k for k, v in (p.get("ssh") or {}).items() if not v)
        if bad_ssh:
            prob("can't SSH from here to " + ", ".join(bad_ssh),
                 (n, ["box", "boxes", "setup"]), "box boxes setup" + ("" if here else f" (on {n})"))
        elif p.get("ssh"):
            good.append("SSH to the others")
        rows.append({"machine": n, "here": here, "ok": not probs, "lines": good, "problems": probs})
    return rows


def summary_line(rows, repos):
    bad = [r["machine"] for r in rows if not r["ok"]]
    n = sum(len(r["problems"]) for r in rows)
    proj = f"; project{'s' if len(repos) != 1 else ''} {', '.join(repos)}" if repos else "; no project kept on all machines"
    if not bad:
        return f"Hackathon check: all {len(rows)} machines ready{proj}."
    return (f"Hackathon check: {n} problem{'s' if n != 1 else ''} on {', '.join(bad)} "
            f"({len(rows) - len(bad)} of {len(rows)} machines ready{proj}).")


def render(rows, repos):
    out = []
    for r in rows:
        head = f"{r['machine']}{' (here)' if r['here'] else ''}: {'ok' if r['ok'] else 'problem'}"
        out.append(head + (" - " + "; ".join(r["lines"]) if r["lines"] else ""))
        for pr in r["problems"]:
            out.append(f"  problem: {pr['what']}" + (f" -> {pr['next']}" if pr["next"] else ""))
    if not repos:
        out.append("No project is kept on all machines yet: box projects everywhere NAME on (or --repo NAME).")
    out.append(summary_line(rows, repos))
    return "\n".join(out)


def fixes(rows):
    """The distinct automatic steps the problems call for, in a safe order: choose the project, then SSH and T3
    links (box boxes setup), then start T3, then copy projects."""
    seen, out = set(), []
    for r in rows:
        for pr in r["problems"]:
            f = pr.get("fix")
            if f and (f[0], tuple(f[1])) not in seen:
                seen.add((f[0], tuple(f[1])))
                out.append((f[0], list(f[1])))
    return sorted(out, key=fix_rank)


def fix_rank(f):
    argv = f[1]
    if "everywhere" in argv:
        return 0
    if argv[:3] == ["box", "boxes", "setup"]:
        return 1
    if argv[:2] == ["box", "projects"]:
        return 3
    return 2


def fix_label(where, argv, me):
    return " ".join(argv) + ("" if where in ("here", me) else f" (on {where})")
