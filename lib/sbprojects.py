"""Projects on all my machines: the same git projects in the same folder on every treehouse box a person has.

Why: the T3 app's automatic load balancing (Settings > Environments > Load balancing) only moves work for a
project that exists on several connected environments. Federation (lib/sbfed.py) already gives a person the
same account, uid, SSH key and GitHub login on every machine and links their T3s; this adds the projects.

  box projects [--json]                       git repos directly in ~ (with their remote), which ones are kept on
                                              every machine, and the last sync to each machine
  box projects everywhere NAME on|off         choose (on this machine); off stops syncing, the copies stay
  box projects sync [--json]                  sync now instead of at the next janitor tick

How it runs:
- The choice lives on the machine where it was made (~/.sharedbox/projects.json). That machine pushes it: root's
  `sharedbox-janitor tick` (every 10 min) runs `box _projects-sync` AS THE USER under nice/ionice, which, when
  due (every SYNC_EVERY, or right after the choice changed), SSHes to each other machine in BOXES as the same user
  (the `Host <box>` block and per-user key federation writes; the owner's own alias works too) and runs
  `nice ionice box _projects-here` there with the list of {name, url}.
- On the other machine, as that person: ~/NAME missing -> blobless clone from the project's remote (GitHub; never
  from this machine, so only pushed work travels). ~/NAME is that same repo -> git fetch, then fast-forward only when
  nothing is uncommitted or untracked and the branch has no commits of its own. Anything else (a different repo,
  not a repo, local changes, unpushed commits, diverged) is skipped and reported. Nothing here ever resets,
  overwrites or deletes; turning a project off only stops the syncing.
- A copy that holds the repo is added to that person's T3 on that machine (`t3 project add PATH --title NAME`, once),
  so it shows in T3 and load balancing can pick it.
- Every git call has a timeout and runs non-interactively (no credential prompt can hang it). The receiving side
  stops starting new clones after HERE_BUDGET seconds; the rest wait for the next sync.

Pure logic plus small I/O helpers that take home folders and runners explicitly, so the whole round trip is
unit-testable with a fake second machine (tests/test_projects.py). Stdlib only.
"""

import json
import os
import re
import subprocess
import time

CHOICE_REL = ".sharedbox/projects.json"         # {"everywhere": [names]}: chosen on this machine
STATE_REL = ".sharedbox/projects-state.json"    # this machine's last sync to each other machine
HERE_REL = ".sharedbox/projects-here.json"      # on a receiving box: copies already added to T3 there
SYNC_EVERY = 1800        # seconds between syncs when nothing changed (the janitor ticks every 10 min)
CLONE_TIMEOUT = 600
FETCH_TIMEOUT = 180
GIT_TIMEOUT = 30
HERE_BUDGET = 480        # the receiving box starts no new clone after this many seconds (next sync does the rest)
PEER_TIMEOUT = 840       # one other machine's whole answer over SSH (the janitor gives the user pass 900 s)
SYNC_BUDGET = 600        # the sending box starts no new machine after this
MAX_PROJECTS = 50
NICE = ("nice", "-n", "10", "ionice", "-c", "2", "-n", "7")   # low priority; best-effort IO so it never starves
NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,99}")
# a remote another box can clone from: https (no password in it), ssh:// or scp-style user@host:path. Local paths
# and file:// can't be reached from another box; a URL with a token in it is never copied around.
URL_RE = re.compile(r"https://[A-Za-z0-9.-]+(:\d{1,5})?/[^\s@]+|ssh://[A-Za-z0-9._@:/~-]+"
                    r"|[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._/~-]+")
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "LC_ALL": "C",
           "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=15"}
OK_STATES = ("cloned", "updated", "current")
STATES = OK_STATES + ("skipped", "error", "waiting")


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def cloneable(url):
    return bool(URL_RE.fullmatch(str(url or "")))


def norm_url(url):
    """github.com/owner/repo for every spelling of the same remote (https, ssh://, git@host:path, .git, /)."""
    u = str(url or "").strip()
    u = re.sub(r"^[a-z+]+://", "", u, flags=re.I)
    u = re.sub(r"^[^@/]+@", "", u)
    m = re.match(r"^([^/:]+):(?!\d+/)(.*)$", u)  # scp style host:path (not host:port/path)
    if m:
        u = f"{m.group(1)}/{m.group(2)}"
    u = re.sub(r"^([^/]+):\d+/", r"\1/", u)
    u = u.rstrip("/")
    u = u[:-4] if u.endswith(".git") else u
    host, _, path = u.partition("/")
    return host.lower() + ("/" + path if path else "")


def run_git(args, cwd=None, timeout=GIT_TIMEOUT):
    """(returncode, stdout, stderr) of a non-interactive git call; 124 on timeout."""
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL, env={**os.environ, **GIT_ENV})
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as ex:
        return 127, "", str(ex)


def last_line(text, n=160):
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    return (lines[-1] if lines else "no details")[:n]


def read_json(path, default):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, type(default)) else default
    except (OSError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


# ---------- this box: what there is, what's chosen ----------

def list_repos(home, git=run_git):
    """Git repos directly in ~ (not hidden, not symlinks): [{"name", "url", "available", "why"}]. "url" is only
    filled when another machine can clone from it."""
    out = []
    try:
        names = sorted(os.listdir(home), key=str.lower)
    except OSError:
        return out
    for name in names:
        p = os.path.join(home, name)
        if not NAME_RE.fullmatch(name) or os.path.islink(p) or not os.path.isdir(p) \
                or not os.path.exists(os.path.join(p, ".git")):
            continue
        rc, url, _ = git(["config", "--get", "remote.origin.url"], cwd=p, timeout=10)
        url = url.strip() if rc == 0 else ""
        ok = cloneable(url)
        why = "" if ok else ("No remote yet. Push it to GitHub first." if not url else
                             "Its remote can't be reached from another machine. Push it to GitHub first.")
        out.append({"name": name, "url": url if ok else "", "available": ok, "why": why})
    return out


def choice_path(home):
    return os.path.join(home, CHOICE_REL)


def load_choice(home):
    names = read_json(choice_path(home), {}).get("everywhere") or []
    return sorted({n for n in names if isinstance(n, str) and NAME_RE.fullmatch(n)}, key=str.lower)


def set_choice(home, name, on, repos=None):
    """(ok, message). Turning on needs a repo in ~ another machine can clone; off always works (the copies stay)."""
    if not NAME_RE.fullmatch(str(name or "")):
        return False, "That isn't a project name."
    cur = set(load_choice(home))
    if on:
        r = next((r for r in (repos if repos is not None else list_repos(home)) if r["name"] == name), None)
        if not r:
            return False, f"There's no git project called {name} in your home folder."
        if not r["available"]:
            return False, f"{name}: {r['why']}"
        cur.add(name)
        msg = f"{name} will be on all your machines. The first copy starts within 10 minutes."
    else:
        cur.discard(name)
        msg = f"{name} is no longer synced. Copies already on your other machines stay where they are."
    if len(cur) > MAX_PROJECTS:
        return False, f"At most {MAX_PROJECTS} projects can be kept on every machine."
    write_json(choice_path(home), {"everywhere": sorted(cur, key=str.lower)})
    return True, msg


def summary(home, peers=(), git=run_git, repos=None):
    """What `box projects --json` and the dashboard show: every repo in ~, whether it's kept on every machine, and
    per other machine the last result for it."""
    repos = repos if repos is not None else list_repos(home, git)
    chosen = set(load_choice(home))
    st = read_json(os.path.join(home, STATE_REL), {})
    sboxes = st.get("boxes") if isinstance(st.get("boxes"), dict) else {}
    peers = sorted(set(peers) | set(sboxes))
    boxes = {}
    for b in peers:
        v = sboxes.get(b) if isinstance(sboxes.get(b), dict) else {}
        boxes[b] = {"last_sync": int(v.get("ts") or 0) or None, "error": str(v.get("error") or "")}
    out = []
    for r in repos:
        row = dict(r, everywhere=r["name"] in chosen, boxes={})
        if row["everywhere"]:
            for b in peers:
                v = sboxes.get(b) if isinstance(sboxes.get(b), dict) else {}
                p = (v.get("projects") or {}).get(r["name"]) if isinstance(v.get("projects"), dict) else None
                row["boxes"][b] = dict(p) if isinstance(p, dict) else {"state": "waiting", "note": ""}
        out.append(row)
    gone = [n for n in sorted(chosen) if n not in {r["name"] for r in repos}]
    return {"ok": True, "projects": out, "boxes": boxes, "missing": gone, "every": SYNC_EVERY,
            "last_sync": int(st.get("ts") or 0) or None}


# ---------- this box -> the others ----------

def due(home, now, every=SYNC_EVERY):
    st = read_json(os.path.join(home, STATE_REL), {})
    last = float(st.get("ts") or 0)
    try:
        changed = os.path.getmtime(choice_path(home)) > last
    except OSError:
        changed = False
    return changed or now - last >= every


def sync(home, peers, remote, git=run_git, now=None, force=False, clock=time.monotonic, budget=SYNC_BUDGET):
    """Push the chosen projects to each other machine. peers: {box: ssh destination}; remote(dest, request) -> the
    other machine's `box _projects-here` answer (a dict). Returns {"ok", "ran", "boxes": {box: summary line}}."""
    now = time.time() if now is None else now
    repos = {r["name"]: r for r in list_repos(home, git)}
    want = [{"name": n, "url": repos[n]["url"]} for n in load_choice(home) if n in repos and repos[n]["available"]]
    if not want or not peers or not (force or due(home, now)):
        return {"ok": True, "ran": False, "boxes": {}}
    path = os.path.join(home, STATE_REL)
    st = read_json(path, {})
    old = st.get("boxes") if isinstance(st.get("boxes"), dict) else {}
    boxes, lines, t0 = {}, {}, clock()
    for b, dest in sorted(peers.items()):
        prev = old.get(b) if isinstance(old.get(b), dict) else {}
        if clock() - t0 > budget:  # out of time: this machine waits for the next sync, its last results stay
            boxes[b] = prev
            continue
        ans = remote(dest, {"projects": want})
        row = {"ts": int(now)}
        if isinstance(ans, dict) and ans.get("ok") and isinstance(ans.get("results"), list):
            row["projects"] = {}
            for x in ans["results"]:
                if isinstance(x, dict) and NAME_RE.fullmatch(str(x.get("name") or "")) and x.get("state") in STATES:
                    row["projects"][x["name"]] = {"state": x["state"], "note": str(x.get("note") or "")[:200],
                                                  "t3": bool(x.get("t3")), "ts": int(now)}
            n_ok = sum(1 for x in row["projects"].values() if x["state"] in OK_STATES)
            lines[b] = f"{n_ok} of {len(want)} in place" + (
                "" if n_ok == len(want) else " (box projects shows why)")
        else:
            row["error"] = str((ans or {}).get("error") or "no answer")[:200]
            row["projects"] = prev.get("projects") or {}
            lines[b] = row["error"]
        boxes[b] = row
    write_json(path, {"ts": round(now, 3), "boxes": boxes})  # not rounded down: a choice made earlier in the
    #                                                          same second must not look newer than this sync
    return {"ok": True, "ran": True, "boxes": lines}


# ---------- the other box: clone or fast-forward, never anything else ----------

def apply_one(home, name, url, git=run_git, allow=cloneable):
    """{"name", "state", "note", "repo"}: repo=True when ~/NAME holds this project (so T3 may list it)."""
    def res(state, note="", repo=False):
        return {"name": name, "state": state, "note": note, "repo": repo}
    if not NAME_RE.fullmatch(str(name or "")) or not allow(url):
        return res("error", "Not a project treehouse can copy.")
    path = os.path.join(home, name)
    if not os.path.lexists(path):
        rc, _, err = git(["clone", "--quiet", "--filter=blob:none", "--", url, path], timeout=CLONE_TIMEOUT)
        if rc != 0:  # git removes a half-made clone itself; nothing of ours to clean up
            return res("error", "Couldn't clone it: " + last_line(err))
        return res("cloned", "", True)
    if os.path.islink(path) or not os.path.isdir(path) or not os.path.exists(os.path.join(path, ".git")):
        return res("skipped", f"~/{name} there isn't a git repo; left alone.")
    rc, there, _ = git(["config", "--get", "remote.origin.url"], cwd=path)
    if norm_url(there.strip()) != norm_url(url):
        return res("skipped", f"~/{name} there is a different repo; left alone.")
    rc, dirty, err = git(["status", "--porcelain"], cwd=path)
    if rc != 0:
        return res("error", "git status failed: " + last_line(err), True)
    if dirty.strip():
        return res("skipped", "Has uncommitted changes there; not updated.", True)
    rc, _, err = git(["fetch", "--quiet", "origin"], cwd=path, timeout=FETCH_TIMEOUT)
    if rc != 0:
        return res("error", "Couldn't fetch: " + last_line(err), True)
    rc, _, _ = git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=path)
    if rc != 0:
        return res("current", "Fetched; the branch checked out there doesn't track one on the remote.", True)
    rc, counts, err = git(["rev-list", "--left-right", "--count", "HEAD...@{u}"], cwd=path)
    try:
        ahead, behind = (int(x) for x in counts.split())
    except ValueError:
        return res("error", "git rev-list failed: " + last_line(err), True)
    if ahead and behind:
        return res("skipped", f"Has {plural(ahead, 'commit')} of its own there and the remote moved on; not updated.",
                   True)
    if ahead:
        return res("skipped", f"Has {plural(ahead, 'commit')} there that aren't pushed; not updated.", True)
    if not behind:
        return res("current", "", True)
    rc, _, err = git(["merge", "--ff-only", "--quiet", "@{u}"], cwd=path)
    if rc != 0:
        return res("error", "Couldn't fast-forward: " + last_line(err), True)
    return res("updated", f"{plural(behind, 'new commit')}.", True)


def apply_all(home, req, git=run_git, t3_add=None, allow=cloneable, clock=time.monotonic, budget=HERE_BUDGET):
    """The receiving side of a sync. req: {"projects": [{"name", "url"}]}. t3_add(path, name) -> (ok, note).
    Returns {"ok", "results": [{"name", "state", "note", "t3"}]}."""
    items = [p for p in (req.get("projects") if isinstance(req, dict) else None) or [] if isinstance(p, dict)]
    here_path = os.path.join(home, HERE_REL)
    here = read_json(here_path, {})
    known = {n for n in here.get("t3") or [] if isinstance(n, str)}
    out, t0 = [], clock()
    for p in items[:MAX_PROJECTS]:
        name, url = str(p.get("name") or ""), str(p.get("url") or "")
        if clock() - t0 > budget and not os.path.lexists(os.path.join(home, name)):
            out.append({"name": name, "state": "waiting", "note": "Next sync.", "t3": name in known})
            continue
        r = apply_one(home, name, url, git, allow)
        t3 = name in known
        if r.pop("repo") and not t3 and t3_add:
            t3, note = t3_add(os.path.join(home, name), name)
            if t3:
                known.add(name)
            elif note:
                r["note"] = (r["note"] + " " if r["note"] else "") + "Not added to T3: " + note
        r["t3"] = bool(t3)
        out.append(r)
    if known != set(here.get("t3") or []):
        write_json(here_path, {"t3": sorted(known)})
    return {"ok": True, "results": out}
