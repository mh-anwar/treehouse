"""Weekly safe cleanup: only things that rebuild themselves.

Two halves:
  run_user(plan)   AS one account (the janitor runs it via setpriv as `box _weekly-clean`; `box clean` runs it for
                   yourself): package-manager and build caches, old Playwright builds, headless Chrome leftovers, and
                   build cache + dangling images of that account's own rootless Docker daemon.
  run_root(plan)   root: build cache + dangling images of the rootful Docker daemon (if there is one) and a
                   journal trim to JOURNAL_KEEP_DAYS / JOURNAL_MAX_SIZE.

Never touched, by construction and re-checked before every deletion (safe_target):
  - Docker volumes, tagged images and containers (only `builder prune --filter until=` and `image prune` without
    -a, which removes untagged images only; see docker_commands)
  - ~/keep, ~/.claude, ~/.codex, ~/.t3, and anything inside a git repository
  - anything outside the fixed list of cache folders below, and anything reached through a symlink

Nothing here stops a running program: a cache whose tool is running right now (npm, go, pip, chrome, ...) is left
for next week. A Docker daemon that isn't running is never started.

Every function returns rows {"path", "bytes", "reason"} (+ "error"); bytes is what was freed, or in a dry run what
would be (an estimate where the tool can't say in advance: "est" is then set).
"""

import os
import re
import stat
import subprocess
import time

import sbclean

DAY = 86400
WEEK = 7 * DAY
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# never deleted or descended into, whatever a target list says
PROTECTED = ("keep", ".claude", ".codex", ".t3")

# folders whose whole contents rebuild themselves. (path in ~, reason, tools that must not be running)
DIR_TARGETS = [
    (".npm/_cacache", "npm cache", ("npm", "npx")),
    (".cache/go-build", "Go build cache", ("go",)),
    (".cache/sccache", "sccache", ("sccache",)),
    (".cache/pip", "pip cache", ("pip", "pip3")),
    (".cache/node-gyp", "node-gyp cache", ("node-gyp",)),
    (".cache/google-chrome", "Chrome cache", ("chrome",)),
    (".cache/chromium", "Chromium cache", ("chromium", "chrome")),
]
# folders of per-run leftovers: entries older than a day go (a run in progress keeps its folder)
AGED_TARGETS = [
    (".cache/google-chrome-for-testing-headless", "headless Chrome leftovers", DAY),
]
PLAYWRIGHT = ".cache/ms-playwright"
PW_BUILD_RE = re.compile(r"^([a-z][a-z0-9_]*?)-(\d+)$")   # chromium-1254, chromium_headless_shell-1254, ffmpeg-1013
UV_DIR = ".cache/uv"
PNPM_STORE = ".local/share/pnpm/store"

# every path a deletion may start from; safe_target refuses anything else
ALLOWED = tuple(r for r, *_ in DIR_TARGETS + AGED_TARGETS) + (PLAYWRIGHT, UV_DIR, PNPM_STORE)

DOCKER_BIN = ("/usr/bin/docker", "/usr/local/bin/docker", "/bin/docker")
BUILD_CACHE_AGE = "168h"   # build cache unused for 7 days


# ---------- the guard ----------

def _in_git_repo(path, home):
    """True if `path` or a folder above it (up to, not including, home) is a git work tree."""
    d = os.path.dirname(path)
    while d.startswith(home + os.sep):
        if os.path.lexists(os.path.join(d, ".git")):
            return True
        d = os.path.dirname(d)
    return os.path.lexists(os.path.join(path, ".git"))


def safe_target(path, home):
    """The last check before anything is deleted. Refuses (returns a reason) unless `path` is inside one of the
    ALLOWED cache folders of `home`, reached without symlinks, outside the PROTECTED folders and outside any git
    repository. Returns None when it's safe."""
    home = os.path.normpath(home)
    path = os.path.normpath(path)
    if not path.startswith(home + os.sep):
        return "outside the home folder"
    rel = os.path.relpath(path, home)
    if rel.split(os.sep, 1)[0] in PROTECTED:
        return "protected folder"
    if not any(rel == a or rel.startswith(a + os.sep) for a in ALLOWED):
        return "not a known cache"
    # every component from home down must be a real folder (or the final entry itself not a symlink)
    cur = home
    parts = rel.split(os.sep)
    for i, p in enumerate(parts):
        cur = os.path.join(cur, p)
        st = sbclean._lstat(cur)
        if st is None:
            return "gone"
        if stat.S_ISLNK(st.st_mode) and i < len(parts) - 1:
            return "reached through a symlink"
    if _in_git_repo(path, home):
        return "inside a git repository"
    return None


# ---------- helpers ----------

def parse_size(s):
    """'1.23GB' / '512kB' / '374.2M' / '0B' -> bytes (docker uses powers of 1000, journalctl powers of 1024)."""
    m = re.match(r"^\s*([\d.]+)\s*([kKMGTP]?)(i?)(B?)\s*$", str(s or ""))
    if not m:
        return 0
    num, unit, binary, b = float(m.group(1)), m.group(2).upper(), m.group(3), m.group(4)
    base = 1024 if (binary or not b) else 1000   # journalctl prints "374.2M" (no B): binary
    return int(num * base ** " KMGTP".index(unit or " ")) if unit else int(num)


def running_tools(uid=None):
    """Basenames of argv[0] and argv[1] of this account's processes (`node /usr/bin/npm` counts as npm)."""
    uid = os.getuid() if uid is None else uid
    out = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read(4096).split(b"\0")[:2]
        except OSError:
            continue
        out.update(os.path.basename(a.decode(errors="replace")) for a in argv if a)
    return out


def _children(d):
    try:
        return sorted(os.scandir(d), key=lambda e: e.name)
    except OSError:
        return []


def playwright_old_builds(pw_dir):
    """Entries of ~/.cache/ms-playwright that a newer build of the same browser replaced. The newest build of each
    browser (chromium, chromium_headless_shell, firefox, webkit, ffmpeg, ...) stays; anything that isn't a
    NAME-NUMBER build folder (Playwright's own bookkeeping: .links, daemon, ...) is never selected."""
    newest, builds = {}, []
    for e in _children(pw_dir):
        m = PW_BUILD_RE.match(e.name)
        if not m or not sbclean._is_real_dir(e.path):
            continue
        kind, rev = m.group(1), int(m.group(2))
        builds.append((kind, rev, e.path))
        newest[kind] = max(newest.get(kind, -1), rev)
    return [p for kind, rev, p in builds if rev < newest[kind]]


def select_targets(home, now=None, running=frozenset(), in_use=frozenset()):
    """(path, reason) of everything the file part of the weekly pass would delete in `home`. Pure selection (no
    deletion), so tests can prove what is and isn't picked. `running`: tool names running now; `in_use`: paths
    this account's processes have open or run from (an old Playwright build still running stays)."""
    now = time.time() if now is None else now
    out = []
    for rel, reason, tools in DIR_TARGETS:
        d = os.path.join(home, rel)
        if not sbclean._is_real_dir(d) or running & set(tools):
            continue
        out += [(e.path, reason) for e in _children(d) if not sbclean._inside(e.path, in_use)]
    for rel, reason, age in AGED_TARGETS:
        d = os.path.join(home, rel)
        if not sbclean._is_real_dir(d):
            continue
        out += [(e.path, reason) for e in _children(d)
                if now - sbclean.last_used(e.path) > age and not sbclean._inside(e.path, in_use)]
    pw = os.path.join(home, PLAYWRIGHT)
    if sbclean._is_real_dir(pw):
        out += [(p, "old Playwright build") for p in playwright_old_builds(pw) if not sbclean._inside(p, in_use)]
    return [(p, r) for p, r in out if safe_target(p, home) is None]


# ---------- docker ----------

def docker_bin():
    """The real docker CLI (not a wrapper in ~/.local/bin), or None."""
    for p in DOCKER_BIN:
        if os.access(p, os.X_OK):
            return p
    return None


def docker_commands(sock):
    """The only two Docker commands the weekly pass ever runs, against the daemon at `sock`:
    build cache unused for 7 days, and dangling (untagged) images. No -a/--all, no volumes, no containers, no
    `system prune`. Tested in tests/test_weekly_clean.py."""
    base = ["-H", f"unix://{sock}"]
    return [
        (base + ["builder", "prune", "--force", "--filter", f"until={BUILD_CACHE_AGE}"], "Docker build cache"),
        (base + ["image", "prune", "--force"], "Docker dangling images"),
    ]


def _sock_ok(sock):
    st = sbclean._lstat(sock)
    return st is not None and stat.S_ISSOCK(st.st_mode) and os.access(sock, os.R_OK | os.W_OK)


def _docker(exe, args, timeout=600):
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/"),
                                "DOCKER_CLI_HINTS": "false"})
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def _docker_estimate(exe, sock):
    """Dry run: (build cache reclaimable, dangling image bytes). Build cache isn't filtered by age here, so it's an
    upper bound."""
    base = ["-H", f"unix://{sock}"]
    rc, out, _ = _docker(exe, base + ["system", "df", "--format", "{{.Type}}\t{{.Reclaimable}}"], 120)
    build = 0
    for line in out.splitlines() if rc == 0 else []:
        t, _, rec = line.partition("\t")
        if t.strip() == "Build Cache":
            build = parse_size(rec.split("(")[0])
    rc, out, _ = _docker(exe, base + ["image", "ls", "--filter", "dangling=true", "--format", "{{.Size}}"], 120)
    dangling = sum(parse_size(s) for s in out.split()) if rc == 0 else 0
    return build, dangling


TOTAL_RE = re.compile(r"Total(?: reclaimed space)?:\s*([\d.]+\s*[kKMGTP]?i?B)")


def docker_pass(sock, dry, label):
    """Run (or estimate) docker_commands against `sock`. Never starts a daemon: a missing or unusable socket is
    skipped silently."""
    exe = docker_bin()
    if not exe or not _sock_ok(sock):
        return []
    rc, _, _ = _docker(exe, ["-H", f"unix://{sock}", "version", "--format", "{{.Server.Version}}"], 30)
    if rc != 0:
        return []  # daemon not answering: leave it alone
    rows = []
    if dry:
        build, dangling = _docker_estimate(exe, sock)
        for (args, reason), n in zip(docker_commands(sock), (build, dangling)):
            rows.append({"path": f"{label}: docker {' '.join(args[2:])}", "bytes": n, "reason": reason, "est": 1})
        return rows
    for args, reason in docker_commands(sock):
        rc, out, err = _docker(exe, args)
        m = TOTAL_RE.search(out)
        row = {"path": f"{label}: docker {' '.join(args[2:])}", "bytes": parse_size(m.group(1)) if m else 0,
               "reason": reason}
        if rc != 0:
            row["error"] = (err or out).strip()[-200:]
        rows.append(row)
    return rows


# ---------- uv / pnpm ----------

def _native(home, name, args, rel, dry, reason, est_bytes=0):
    d = os.path.join(home, rel)
    exe, path = sbclean._tool(home, name)
    if not exe or not sbclean._is_real_dir(d) or safe_target(d, home):
        return []
    if dry:  # uv and pnpm can't say beforehand what a prune frees ("unknown"); a full clean frees the folder's size
        return [{"path": f"{name} {' '.join(args)}", "bytes": est_bytes, "reason": reason, "est": 1,
                 **({} if est_bytes else {"unknown": 1})}]
    before = sbclean.tree_size(d)
    try:
        subprocess.run([exe, *args], cwd=home, env={**os.environ, "PATH": path, "UV_LOCK_TIMEOUT": "30"},
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=900, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [{"path": f"{name} {' '.join(args)}", "bytes": 0, "reason": reason, "error": str(e)[:200]}]
    return [{"path": f"{name} {' '.join(args)}", "bytes": max(0, before - sbclean.tree_size(d)), "reason": reason}]


def native_pruners(home, plan, running, dry):
    rows = []
    uv = os.path.join(home, UV_DIR)
    if "uv" not in running and sbclean._is_real_dir(uv):
        size = sbclean.tree_size(uv)
        if size > float(plan.get("uv_clean_gb", 10)) * (1 << 30):
            rows += _native(home, "uv", ["cache", "clean"], UV_DIR, dry, "uv cache (over the size cap)", size)
        else:
            rows += _native(home, "uv", ["cache", "prune"], UV_DIR, dry, "uv cache prune")
    if "pnpm" not in running:
        rows += _native(home, "pnpm", ["store", "prune"], PNPM_STORE, dry, "pnpm store prune")
    return rows


# ---------- the passes ----------

def run_user(plan):
    """One account's weekly pass, run AS that account. plan: {"dry_run": bool, "uv_clean_gb": number}."""
    home = os.path.expanduser("~")
    dry = bool(plan.get("dry_run"))
    running = running_tools()
    rows = []
    for path, reason in select_targets(home, running=running, in_use=sbclean.my_paths()):
        if safe_target(path, home):  # checked again right before deleting
            continue
        try:
            rows.append({"path": path, "bytes": sbclean.remove(path, dry), "reason": reason})
        except OSError as e:
            rows.append({"path": path, "bytes": 0, "reason": reason, "error": str(e)[:200]})
    rows += native_pruners(home, plan, running, dry)
    rows += docker_pass(f"/run/user/{os.getuid()}/docker.sock", dry, "your Docker")
    return {"deleted": rows, "freed": sum(r["bytes"] for r in rows), "dry_run": dry}


def journal_pass(plan, dry):
    keep_days = int(plan.get("journal_keep_days", 30))
    max_size = str(plan.get("journal_max_size", "1G"))
    args = [f"--vacuum-time={keep_days}d", f"--vacuum-size={max_size}"]
    label = f"journal: journalctl {' '.join(args)}"
    if dry:
        try:
            out = subprocess.run(["journalctl", "--disk-usage"], capture_output=True, text=True, timeout=60).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        m = re.search(r"take up ([\d.]+[KMGT]?)", out)
        used = parse_size(m.group(1)) if m else 0
        return [{"path": label, "bytes": max(0, used - parse_size(max_size)), "reason": "journal over the size cap"
                 if used > parse_size(max_size) else "journal entries older than the cap", "est": 1}]
    try:
        r = subprocess.run(["journalctl", *args], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [{"path": label, "bytes": 0, "reason": "journal trim", "error": str(e)[:200]}]
    freed = sum(parse_size(x) for x in re.findall(r"freed ([\d.]+[KMGT]?)", r.stdout + r.stderr))
    row = {"path": label, "bytes": freed, "reason": "journal trim"}
    if r.returncode != 0:
        row["error"] = r.stderr.strip()[-200:]
    return [row]


def run_root(plan):
    """Root's part: the rootful Docker daemon (if any) and the system journal."""
    dry = bool(plan.get("dry_run"))
    rows = docker_pass("/var/run/docker.sock", dry, "system Docker")
    rows += journal_pass(plan, dry)
    return {"deleted": rows, "freed": sum(r["bytes"] for r in rows), "dry_run": dry}


def summarize(rows, dry, human):
    """Human lines, one per reason (a pass can clear hundreds of folders of one kind)."""
    groups = {}
    for r in rows:
        g = groups.setdefault(r["reason"], {"n": 0, "bytes": 0, "est": False, "unknown": False, "errors": [],
                                            "path": r["path"]})
        g["n"] += 1
        g["bytes"] += r.get("bytes") or 0
        g["est"] |= bool(r.get("est"))
        g["unknown"] |= bool(r.get("unknown"))
        if r.get("error"):
            g["errors"].append(str(r["error"]))
    verb = "would free" if dry else "freed"
    out = []
    for reason, g in groups.items():
        size = "unknown" if g["unknown"] and not g["bytes"] else \
            ("about " if g["est"] and g["bytes"] else "") + human(g["bytes"])
        what = g["path"] if g["n"] == 1 else f"{g['n']} items, e.g. {g['path']}"
        err = f"  [failed: {g['errors'][0][:200]}]" if g["errors"] else ""
        out.append(f"{verb} {size:>10}  {reason}: {what}{err}")
    return out


# ---------- schedule ----------

def weekday(cfg):
    d = str(cfg.get("WEEKLY_CLEAN_DAY") or "Sun").strip()[:3].capitalize()
    return d if d in DAYS else "Sun"


def enabled(cfg):
    return str(cfg.get("WEEKLY_CLEAN", "1")).strip() not in ("0", "", "off", "no", "false")


def due(cfg, last, now=None):
    """Due on WEEKLY_CLEAN_DAY once it's been 6+ days, or any day once it's 8+ days overdue (the box was off that
    day, or the account's agent was busy every time). An account never cleaned waits for the day."""
    now = time.time() if now is None else now
    if not enabled(cfg) or now - (last or 0) < 6 * DAY:
        return False
    return time.strftime("%a", time.localtime(now)) == weekday(cfg) or bool(last and now - last > 8 * DAY)


def plan_from(cfg, dry):
    return {"dry_run": bool(dry), "uv_clean_gb": float(cfg.get("UV_CACHE_CLEAN_GB") or 10),
            "journal_keep_days": int(cfg.get("JOURNAL_KEEP_DAYS") or 30),
            "journal_max_size": str(cfg.get("JOURNAL_MAX_SIZE") or "1G")}


REASONS = ({r for _, r, _ in DIR_TARGETS} | {r for _, r, _ in AGED_TARGETS}
           | {"old Playwright build", "uv cache (over the size cap)", "uv cache prune", "pnpm store prune",
              "Docker build cache", "Docker dangling images", "journal trim"})


def by_reason(rows):
    """{reason: bytes freed}. Reasons outside REASONS (a reply from code running as a friend could say anything)
    are counted as "other", since this goes into the world-readable event log."""
    out = {}
    for r in rows:
        if r.get("bytes"):
            k = r["reason"] if r["reason"] in REASONS else "other"
            out[k] = out.get(k, 0) + r["bytes"]
    return out
