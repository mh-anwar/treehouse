"""Multi-box: see every treehouse box's load and run heavy work on whichever has room.

  box boxes [--json]                         load on each box in BOXES, whether you can reach it, the best pick
  box boxes setup                            (re)connect your account on the other boxes now: T3 links, GitHub
  box boxes login claude|codex|gh --on NAME [--code X | --wait]   log an agent in on another box (device login)
  box run [--anywhere|--on NAME] [--shards N] [--prep CMD] [--back PATH]... [--git] -- CMD...
                                             run CMD on the least-loaded box: sync this project there
                                             (incrementally), stream the output back, exit with CMD's code

Design (docs/multibox.md):
- Load comes from each box's existing GET /api/box (sharedbox-web, port WEB_PORT, answers only boxes in BOXES),
  so `box boxes` costs one small HTTP call per box and no SSH.
- Work runs over plain SSH as the same user on the other box. Destination: a Host in ~/.ssh/config whose
  HostName is the box's IP (the owner's alias, or the `Host <box>` block federation writes for every friend,
  with their per-user ~/.ssh/id_treehouse key and pinned host keys: lib/sbfed.py), else ~/.sharedbox/boxes.json {"name": "user@host"}, else
  <same username>@<tailscale ip> (what the provisioner sets up for friends).
- The project goes to a persistent mirror on the other box (~/.sharedbox/mirror/<slug>), synced with rsync
  using the project's .gitignore files as the filter: only source changes travel, and the other box's own
  node_modules/.venv/build outputs (ignored, so excluded) survive between runs instead of being re-copied.
- `--prep CMD` (e.g. "npm ci") runs there only when the lockfiles or CMD change.
- Anything wrong before CMD starts there (no BOXES, box down, no SSH, sync failed) -> run here instead, through
  `box heavy`, with one line on stderr saying why. Never fails *because* another box exists.
Stdlib only. Every function that touches the network or a subprocess takes its inputs explicitly so the
planning logic (score, pick, shard plan, rsync/ssh argv, remote script) is unit-testable without either.
"""

import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

API_TIMEOUT = 3
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4", "-o", "StrictHostKeyChecking=accept-new"]
LOCKFILES = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "uv.lock", "poetry.lock",
             "requirements.txt", "requirements-dev.txt", "Pipfile.lock", "Cargo.lock", "go.sum", "Gemfile.lock",
             "composer.lock")
# never copied when the folder isn't a git repo (no .gitignore to go by); rebuilt on the other side instead
NONGIT_EXCLUDES = ("node_modules", ".venv", "venv", "__pycache__", "target", ".next", ".cache", "dist", "build",
                   ".pytest_cache", ".mypy_cache", ".turbo", ".gradle")
REMOTE_BEAT = 1.5   # another box must have this many times our free capacity...
REMOTE_MIN = 1.0    # ...and at least this many free (speed-weighted) cores more, to be worth a sync
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------- load ----------

def speeds(cfg):
    """BOX_SPEED="oldbox=0.45 ..." (optional): per-core speed relative to 1.0, so an old i3 doesn't look
    as roomy as its idle cores suggest. Unknown/bad entries -> 1.0."""
    out = {}
    for item in re.split(r"[\s,]+", str(cfg.get("BOX_SPEED") or "")):
        name, _, v = item.partition("=")
        try:
            f = float(v)
        except ValueError:
            continue
        if name and 0.05 <= f <= 10:
            out[name.strip().lower()] = f
    return out


def local_summary(sb, name):
    """This box's numbers, in the same shape as a peer's /api/box answer."""
    b = sb.box_load()
    mem = round(100 * (1 - b["mem_avail"] / b["mem_total"]), 1) if b["mem_total"] else None
    free = round(100 * b["disk_free"] / b["disk_total"], 1) if b.get("disk_total") else None
    return {"v": 1, "name": name, "ncpu": b["ncpu"], "load1": round(b["loadavg"][0], 2), "level": b["level"],
            "mem": mem, "disk_free": free}


def fetch_box(ip, port, timeout=API_TIMEOUT):
    """A peer's /api/box as a dict, or (None, why)."""
    try:
        with _OPENER.open(f"http://{ip}:{port}/api/box", timeout=timeout) as r:
            d = json.loads(r.read(16385))
        if isinstance(d, dict) and d.get("v") == 1:
            return d, ""
        return None, "odd answer"
    except urllib.error.HTTPError as ex:
        return None, {403: "doesn't list this box in BOXES", 404: "needs a treehouse update"}.get(ex.code,
                                                                                                  f"error {ex.code}")
    except Exception:  # noqa: BLE001 - refused, timeout, bad JSON: all "down"
        return None, "not reachable"


def score(d, speed=1.0):
    """Free capacity in speed-weighted cores, 0 when the box shouldn't take more work."""
    if not d:
        return 0.0
    ncpu, load = d.get("ncpu") or 0, d.get("load1") or 0.0
    mem = d.get("mem")
    if not ncpu or (isinstance(mem, (int, float)) and mem >= 90):
        return 0.0
    free = max(0.0, ncpu - load) * speed
    free *= {"busy": 0.3, "moderate": 0.7}.get(d.get("level"), 1.0)
    if isinstance(d.get("disk_free"), (int, float)) and d["disk_free"] < 5:
        free = 0.0
    return round(free, 2)


def pick(rows, self_name):
    """The box to run on: the best reachable one, but only leave this box when another clearly has more room
    (a sync costs time). rows: [{"name", "score", "ok"}]. Returns a name."""
    here = next((r for r in rows if r["name"] == self_name), None)
    here_s = here["score"] if here else 0.0
    best = max((r for r in rows if r["ok"] and r["name"] != self_name), key=lambda r: r["score"], default=None)
    if best and best["score"] >= here_s * REMOTE_BEAT and best["score"] - here_s >= REMOTE_MIN:
        return best["name"]
    return self_name


def plan_shards(n, rows, self_name):
    """Spread N shards over boxes in proportion to free capacity (largest remainder). Always at least one box;
    this box gets whatever nobody else has room for. Returns [(shard_index, box_name)]."""
    usable = [r for r in rows if r["ok"] or r["name"] == self_name]
    total = sum(r["score"] for r in usable)
    if total <= 0:
        return [(i, self_name) for i in range(n)]
    quota = [(r["name"], n * r["score"] / total) for r in usable]
    counts = {name: int(q) for name, q in quota}
    left = n - sum(counts.values())
    for name, q in sorted(quota, key=lambda x: (x[1] - int(x[1]), x[0] == self_name), reverse=True)[:left]:
        counts[name] += 1
    out, i = [], 0
    for name, _ in quota:
        for _ in range(counts[name]):
            out.append((i, name))
            i += 1
    return out


# ---------- ssh identity ----------

def ssh_dest(name, ip, user, home, ssh_g=None):
    """How to reach box `name` (see module doc). ssh_g(name) -> `ssh -G name` output (injectable for tests)."""
    try:
        with open(os.path.join(home, ".sharedbox", "boxes.json")) as f:
            d = json.load(f).get(name)
        if isinstance(d, str) and re.fullmatch(r"[A-Za-z0-9._@-]{1,100}", d) and not d.startswith("-"):
            return d
    except (OSError, ValueError, AttributeError):
        pass
    try:
        out = ssh_g(name) if ssh_g else subprocess.run(["ssh", "-G", name], capture_output=True, text=True,
                                                         timeout=5).stdout
        if re.search(rf"^hostname {re.escape(ip)}$", out, re.M):
            return name
    except (OSError, subprocess.TimeoutExpired):
        pass
    return f"{user}@{ip}"


def ssh_ok(dest):
    try:
        return subprocess.run(["ssh", *SSH_OPTS, "-o", "ConnectTimeout=3", dest, "true"], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=8).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# ---------- survey ----------

def survey(sb, cfg, user, home, check_ssh=True):
    """One row per box in BOXES: numbers, score, reachability. Peers are asked in parallel."""
    boxes = sb.boxes(cfg)
    my_ip = sb.tailscale_ip4()
    self_name = next((n for n, ip in boxes.items() if ip == my_ip), socket.gethostname().split(".")[0])
    sp, port = speeds(cfg), cfg.get("WEB_PORT", "8090")
    rows = [{"name": self_name, "ip": my_ip or "", "here": True, "ok": True, "why": "",
             "data": local_summary(sb, self_name), "dest": ""}]
    lock = threading.Lock()

    def ask(name, ip):
        d, why = fetch_box(ip, port)
        dest = ssh_dest(name, ip, user, home)
        ok = bool(d) and (ssh_ok(dest) if check_ssh else True)
        if d and not ok:
            why = f"no SSH login for you there ({dest})"
        with lock:
            rows.append({"name": name, "ip": ip, "here": False, "ok": ok, "why": why, "data": d, "dest": dest})
    ts = [threading.Thread(target=ask, args=(n, ip), daemon=True) for n, ip in boxes.items() if n != self_name]
    for t in ts:
        t.start()
    for t in ts:
        t.join(12)
    with lock:
        rows = list(rows)
    order = list(boxes)
    rows.sort(key=lambda r: (not r["here"], order.index(r["name"]) if r["name"] in order else 99))
    for r in rows:
        r["score"] = score(r["data"], sp.get(r["name"], 1.0)) if r["ok"] or r["here"] else 0.0
    return rows, self_name


def cmd_boxes(sb, cfg, user, home, as_json=False):
    rows, me = survey(sb, cfg, user, home)
    best = pick(rows, me)
    if as_json:
        print(json.dumps({"here": me, "best": best, "boxes": [
            {k: r[k] for k in ("name", "ip", "here", "ok", "why", "score")} | {"load": r["data"]} for r in rows]}))
        return
    if len(rows) == 1:
        print(f"only this box ({me}). The owner lists more with: box friends boxes \"NAME=IP NAME=IP\"")
    for r in rows:
        d = r["data"] or {}
        tag = " (here)" if r["here"] else ""
        if not d:
            print(f"  {r['name']:<14} {r['ip']:<16} down: {r['why']}")
            continue
        print(f"  {r['name'] + tag:<20} {d.get('ncpu', '?')} cpus  load {d.get('load1', '?'):<5} "
              f"{d.get('level', ''):<9} mem {d.get('mem', '?')}%  disk free {d.get('disk_free', '?')}%  "
              f"room {r['score']}" + ("" if r["ok"] else f"  ({r['why']})"))
    print(f"best place for heavy work now: {best}" + (" (here)" if best == me else
                                                      "  ->  box run --anywhere -- CMD"))


# ---------- sync + run ----------

def project_root(cwd):
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                           timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip(), True
    except (OSError, subprocess.TimeoutExpired):
        pass
    return cwd, False


def mirror_rel(host, root):
    """The mirror's path under the remote home: stable per (box, folder) so syncs are incremental."""
    base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(root.rstrip("/")) or "root")[:40]
    h = hashlib.sha1(f"{host}:{os.path.realpath(root)}".encode()).hexdigest()[:10]
    return f".sharedbox/mirror/{base}-{h}"


def rsync_argv(root, is_git, dest, rel, with_git=False):
    """rsync the project to dest:~/rel. Git projects: .gitignore files are the filter (dir-merge, so nested ones
    count) and --delete removes what was deleted here; ignored things on the far side (its node_modules, build
    outputs) are excluded and so never deleted. -z: the boxes may talk over Wi-Fi."""
    remote = f"mkdir -p ~/.sharedbox/mirror; flock -s ~/{shlex.quote(rel)}.lock sh -c {shlex.quote('touch ~/' + shlex.quote(rel) + '.lock; exec rsync "$@"')} sh"
    argv = ["rsync", "--rsync-path", remote, "-az", "--delete", "--mkpath", "-e", shlex.join(["ssh", *SSH_OPTS])]
    if is_git:
        argv += ["--filter=:- .gitignore"]
        if not with_git:
            argv += ["--exclude=/.git"]
    else:
        argv += [f"--exclude={x}" for x in NONGIT_EXCLUDES]
    return argv + [root.rstrip("/") + "/", f"{dest}:{rel}/"]


def prep_key(root, prep):
    h = hashlib.sha1(prep.encode())
    for name in LOCKFILES:
        try:
            with open(os.path.join(root, name), "rb") as f:
                h.update(name.encode() + b"\0" + hashlib.sha1(f.read()).digest())
        except OSError:
            pass
    return h.hexdigest()[:16]


def remote_script(rel, sub, cmd, env=None, prep="", key=""):
    """The sh script run on the other box (in ~/rel/sub). CMD runs in its own session/process group (setsid)
    with stdin from /dev/null; a watcher reads our stdin and kills that group when it closes (we were
    interrupted or the link dropped), so a Ctrl-C here never leaves an orphaned test run there. The --prep stamp
    lives next to the mirror (~/rel.prep), not in it, so a sync's --delete doesn't wipe it."""
    q = shlex.quote
    envs = " ".join(f"{k}={q(str(v))}" for k, v in (env or {}).items())
    where = rel if sub in ("", ".") else f"{rel}/{sub}"
    lines = [f"exec 9>~/{q(rel)}.lock", "flock -s 9 || exit 97",
             f"touch ~/{q(rel)}.lock", f"cd ~/{q(where)} || exit 97"]
    if prep:
        lines += [f'if [ "$(cat ~/{q(rel)}.prep 2>/dev/null)" != {q(key)} ]; then',
                  f"  echo {q('box run: preparing (' + prep[:60] + ')')} >&2",
                  f"  sh -c {q(prep)} </dev/null || exit 98",
                  f"  echo {q(key)} > ~/{q(rel)}.prep",
                  "fi"]
    # a background job in a non-interactive sh gets stdin from /dev/null, so hand ssh's stdin to the watcher as fd 3
    lines += ["exec 3<&0",
              f"setsid env SHAREDBOX_REMOTE=1 {envs} sh -c {q(shlex.join(cmd))} </dev/null 3<&- &",
              "p=$!",
              "( cat <&3; kill -TERM -$p ) >/dev/null 2>&1 &",  # no output fds: ssh closes when CMD ends
              "w=$!",
              "wait $p; rc=$?",
              "kill $w 2>/dev/null",
              "exit $rc"]
    return "\n".join(lines)


def run_local(cmd, why, box_bin):
    if why:
        print(f"box run: running here ({why})", file=sys.stderr, flush=True)
    os.execvp(cmd[0], cmd)


def sync(root, is_git, dest, rel, with_git):
    t = time.time()
    try:
        r = subprocess.run(rsync_argv(root, is_git, dest, rel, with_git), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return f"sync failed: {ex}"
    if r.returncode != 0:
        return "sync failed: " + (r.stderr.strip().splitlines() or ["rsync error"])[-1][:200]
    print(f"box run: synced to {dest} in {time.time() - t:.1f}s", file=sys.stderr, flush=True)
    return ""


def fetch_back(dest, rel, root, paths):
    for p in paths:
        p = p.strip("/")
        if not p or ".." in p.split("/"):
            continue
        subprocess.run(["rsync", "-az", "-e", shlex.join(["ssh", *SSH_OPTS]), f"{dest}:{rel}/{p}",
                        os.path.join(root, os.path.dirname(p)) + "/"], stdin=subprocess.DEVNULL)


def run_remote(dest, script, prefix=None):
    """Run script over ssh, streaming output (prefixed per line when sharding). Returns the exit code."""
    out = subprocess.PIPE if prefix else None
    p = subprocess.Popen(["ssh", *SSH_OPTS, dest, script], stdin=subprocess.PIPE, stdout=out,
                         stderr=subprocess.STDOUT if prefix else None)
    try:
        if prefix:
            for line in p.stdout:
                sys.stdout.write(f"[{prefix}] {line.decode(errors='replace')}")
                sys.stdout.flush()
        return p.wait()
    except KeyboardInterrupt:
        p.stdin.close()  # the remote watcher kills the job
        p.wait(10)
        return 130
    finally:
        if p.stdin and not p.stdin.closed:
            p.stdin.close()


def cmd_run(sb, cfg, user, home, a, box_bin):
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    a.shards = max(1, min(a.shards, 64))
    if not cmd:
        sys.exit("usage: box run [--anywhere|--on NAME] [--shards N] -- COMMAND...")
    if os.environ.get("SHAREDBOX_REMOTE") == "1":  # already on the far side: no ping-pong
        run_local(cmd, "", box_bin)
    if not sb.boxes(cfg):
        run_local(cmd, "no other boxes listed", box_bin)
    rows, me = survey(sb, cfg, user, home)
    if a.on:
        r = next((r for r in rows if r["name"] == a.on), None)
        if not r:
            sys.exit(f"box run: no box called {a.on}; see box boxes")
        if not r["ok"] and not r["here"]:
            run_local(cmd, f"{a.on}: {r['why']}", box_bin)
        plan = [(i, a.on) for i in range(a.shards)] if a.shards > 1 else [(0, a.on)]
    elif a.shards > 1:
        plan = plan_shards(a.shards, rows, me)
    else:
        plan = [(0, pick(rows, me))]
    if all(b == me for _, b in plan) and a.shards <= 1:
        here = next(r for r in rows if r["here"])
        run_local(cmd, f"{me} has the most room ({here['score']})", box_bin)
    root, is_git = project_root(os.getcwd())
    sub = os.path.relpath(os.getcwd(), root)
    by_name = {r["name"]: r for r in rows}
    ready = {}
    for name in {b for _, b in plan if b != me}:  # sync once per box, before any shard starts
        dest = by_name[name]["dest"]
        rel = mirror_rel(socket.gethostname(), root)
        err = sync(root, is_git, dest, rel, a.git)
        ready[name] = (dest, rel) if not err else None
        if err:
            print(f"box run: {name}: {err}; its shards run here", file=sys.stderr, flush=True)
    key = prep_key(root, a.prep) if a.prep else ""

    def one(i, name, prefix):
        env = {"BOX_SHARD": i, "BOX_SHARDS": len(plan)} if len(plan) > 1 else {}
        if name == me or not ready.get(name):
            e = {**os.environ, "SHAREDBOX_REMOTE": "1", **{k: str(v) for k, v in env.items()}}
            p = subprocess.Popen([box_bin, "heavy", "--", *cmd], env=e, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE if prefix else None,
                                 stderr=subprocess.STDOUT if prefix else None)
            if prefix:
                for line in p.stdout:
                    sys.stdout.write(f"[{prefix}] {line.decode(errors='replace')}")
                    sys.stdout.flush()
            return p.wait()
        dest, rel = ready[name]
        rc = run_remote(dest, remote_script(rel, sub, cmd, env, a.prep, key), prefix)
        if rc in (97, 98):
            print(f"box run: {name}: " + ("project folder missing there" if rc == 97 else "--prep failed"),
                  file=sys.stderr, flush=True)
        return rc

    if len(plan) == 1:
        i, name = plan[0]
        if not ready.get(name):
            run_local(cmd, f"couldn't copy the project to {name}", box_bin)
        print(f"box run: running on {name}", file=sys.stderr, flush=True)
        rc = one(i, name, None)
        if a.back and ready.get(name):
            fetch_back(*ready[name], root, a.back)
        sys.exit(rc)
    print("box run: shards " + ", ".join(f"{i}->{b}" for i, b in plan), file=sys.stderr, flush=True)
    rcs = [0] * len(plan)

    def worker(k, i, name):
        rcs[k] = one(i, name, f"{name}#{i}")
    ts = [threading.Thread(target=worker, args=(k, i, b)) for k, (i, b) in enumerate(plan)]
    for t in ts:
        t.start()
    try:
        for t in ts:
            t.join()
    except KeyboardInterrupt:
        sys.exit(130)
    for name, v in ready.items():
        if v and a.back:
            fetch_back(*v, root, a.back)
    bad = [f"#{plan[k][0]} on {plan[k][1]}: exit {rc}" for k, rc in enumerate(rcs) if rc]
    print("box run: all shards passed" if not bad else "box run: failed shards: " + "; ".join(bad),
          file=sys.stderr, flush=True)
    sys.exit(next((rc for rc in rcs if rc), 0))


# ---------- your account on the other boxes (federation: lib/sbfed.py) ----------

LOGIN_KEYS = ("claude", "codex", "gh")


def remote_json(dest, cmd, stdin_text=None, timeout=120):
    try:
        r = subprocess.run(["ssh", *SSH_OPTS, dest, cmd], input=stdin_text, capture_output=True, text=True,
                           timeout=timeout)
        return json.loads((r.stdout.strip().splitlines() or ["{}"])[-1])
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"ok": False, "error": "no answer from that box (try box boxes)"}


def login_text(name, key, out):
    """What the agent tells the human after starting a login on another box."""
    if out.get("done"):
        return f"{key}: already logged in on {name}."
    if not out.get("ok"):
        return f"{key} on {name}: {out.get('error', 'failed')}"
    lines = [f"Ask the human to open this link and approve the {key} login for the box {name}:", "", f"  {out['url']}"]
    if out.get("code"):
        lines += ["", f"and type this code there: {out['code']}", "",
                  f"Then run: box boxes login {key} --on {name} --wait"]
    elif out.get("paste"):
        lines += ["", "It shows a code at the end. Get it from the human, then run:",
                  f"  box boxes login {key} --on {name} --code THE_CODE"]
    return "\n".join(lines)


def cmd_boxes_login(sb, cfg, user, home, a):
    if a.key not in LOGIN_KEYS or not a.on:
        sys.exit("usage: box boxes login claude|codex|gh --on BOX [--code X | --wait]")
    boxes = sb.boxes(cfg)
    if a.on not in boxes:
        sys.exit(f"no box called {a.on}; see box boxes")
    dest = ssh_dest(a.on, boxes[a.on], user, home)
    if a.code:
        out = remote_json(dest, f"box _portal login-code {a.key}", stdin_text=a.code)
    elif a.wait:
        deadline = time.time() + 180
        out = {}
        while time.time() < deadline:
            out = remote_json(dest, f"box _portal login-check {a.key}")
            if out.get("done"):
                break
            time.sleep(3)
    else:
        out = remote_json(dest, f"box _portal login-start {a.key}", timeout=60)
        print(login_text(a.on, a.key, out))
        sys.exit(0 if out.get("ok") else 1)
    print(f"{a.key} on {a.on}: " + ("logged in" if out.get("done") else out.get("error", "not yet")))
    sys.exit(0 if out.get("done") else 1)


def cmd_boxes_setup(sb, cfg, user, home, box_bin):
    """The link pass the root timer runs every 12 h, now: T3 links both ways, GitHub, git identity."""
    boxes = sb.boxes(cfg)
    me = next((n for n, ip in boxes.items() if ip == sb.tailscale_ip4()), socket.gethostname().split(".")[0])
    peers = {n: ip for n, ip in boxes.items() if n != me}
    if not peers:
        sys.exit("only this box; nothing to connect")
    rec = (sb.registry().get("users") or {}).get(user) or {}
    req = json.dumps({"peers": peers, "self": me, "home": (rec.get("home_box") or me) == me})
    r = subprocess.run([box_bin, "_fed-user", "link"], input=req, capture_output=True, text=True, timeout=900)
    try:
        notes = json.loads(r.stdout.strip().splitlines()[-1]).get("notes") or []
    except (ValueError, IndexError):
        notes = [r.stderr.strip()[-300:] or "failed"]
    for n in notes:
        print(n)
    st = sb.read_json(os.path.join(home, ".sharedbox", "boxes-state.json"), {}) or {}
    for name, p in (st.get("peers") or {}).items():
        if not p.get("ssh"):
            print(f"{name}: your account isn't there yet (copied from the primary within ~5 minutes of joining)")
            continue
        print(f"{name}: ssh ok, T3 {'linked' if p.get('t3') else 'not linked yet'}")
        for k in p.get("missing_logins") or []:
            print(f"  {k} isn't logged in there yet: box boxes login {k} --on {name}")


def add_parsers(s, sb, cfg, user, home, box_bin):
    """The argparse hook lib/box calls: `box boxes` and `box run`."""
    x = s.add_parser("boxes", help="load on each of the owner's boxes and the best place for heavy work")
    x.add_argument("op", nargs="?", default="status", choices=["status", "setup", "login"])
    x.add_argument("key", nargs="?", help="login: claude | codex | gh")
    x.add_argument("--on", metavar="NAME", help="login: which box")
    x.add_argument("--code", help="login: the code the browser showed (Claude)")
    x.add_argument("--wait", action="store_true", help="login: wait for a device login to finish")
    x.add_argument("--json", action="store_true")

    def boxes_fn(a):
        if a.op == "login":
            return cmd_boxes_login(sb, cfg, user, home, a)
        if a.op == "setup":
            return cmd_boxes_setup(sb, cfg, user, home, box_bin)
        return cmd_boxes(sb, cfg, user, home, a.json)
    x.set_defaults(fn=boxes_fn)
    x = s.add_parser("run", help="run a heavy command on the least-loaded box (falls back to here)")
    g = x.add_mutually_exclusive_group()
    g.add_argument("--anywhere", action="store_true", help="the default: pick the box with the most room")
    g.add_argument("--on", metavar="NAME", help="this box by name")
    x.add_argument("--shards", type=int, default=1, help="split into N runs (env BOX_SHARD/BOX_SHARDS)")
    x.add_argument("--prep", default="", help="setup run there only when lockfiles change, e.g. 'npm ci'")
    x.add_argument("--back", action="append", default=[], help="path to copy back afterwards (repeatable)")
    x.add_argument("--git", action="store_true", help="also sync .git (for commands that need history)")
    x.add_argument("cmd", nargs="...")
    x.set_defaults(fn=lambda a: cmd_run(sb, cfg, user, home, a, box_bin))
