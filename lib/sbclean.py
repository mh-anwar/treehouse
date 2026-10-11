"""Per-user cleanup pass. Always executed AS the friend (via setpriv), never as root,
so a symlink planted in a home directory can never make root delete the wrong thing.

Input: a plan dict from the janitor. Output: a report dict.
"""

import os
import shutil
import stat
import time

DAY = 86400

CACHE_DIRS = [
    ".cache", ".npm/_cacache", ".npm/_npx", ".gradle/caches", ".gradle/wrapper/dists",
    ".gradle/daemon", ".gradle/native", ".m2/repository", ".cargo/registry", ".cargo/git",
    "go/pkg/mod", ".bun/install/cache", ".local/share/pnpm/store", ".yarn/berry/cache",
    ".android/cache", ".konan/cache", ".nuget/packages", ".pub-cache/hosted",
    ".rustup/downloads", ".rustup/tmp",
]

# directory name -> marker files; empty tuple = always a build artifact
ARTIFACTS = {
    "node_modules": (), ".next": (), ".nuxt": (), ".turbo": (), ".parcel-cache": (),
    ".svelte-kit": (), ".angular": (), ".dart_tool": (), ".cxx": (), ".gradle": (),
    "__pycache__": (), ".pytest_cache": (), ".mypy_cache": (), ".ruff_cache": (),
    ".vite": (), ".expo": (),
    "build": ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
              "pubspec.yaml"),
    "target": ("Cargo.toml", "pom.xml"),
    ".venv": (), "venv": (),
}
TMP_SKIP_PREFIX = ("tmux-", "ssh-", ".X11-unix", ".ICE-unix", "systemd-private", "snap-private")


def _lstat(p):
    try:
        return os.lstat(p)
    except OSError:
        return None


def _is_real_dir(p):
    st = _lstat(p)
    return st is not None and stat.S_ISDIR(st.st_mode)


def tree_size(path):
    total = 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += st.st_blocks * 512
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(e.path)
        except OSError:
            pass
    return total


def last_used(path):
    """Newest sign of use in a tree: file mtime/atime and directory mtime.
    Directory atime is ignored because merely listing a directory (du, our own scans)
    bumps it; stat-ing files never changes their atime."""
    st = _lstat(path)
    if st is None:
        return 0.0
    if not stat.S_ISDIR(st.st_mode):
        return max(st.st_mtime, st.st_atime)
    newest = st.st_mtime
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        newest = max(newest, st.st_mtime)
                        stack.append(e.path)
                    else:
                        newest = max(newest, st.st_mtime, st.st_atime)
        except OSError:
            pass
    return newest


def is_artifact(dirpath, name):
    if name not in ARTIFACTS:
        return False
    if name in (".venv", "venv"):
        return os.path.exists(os.path.join(dirpath, name, "pyvenv.cfg"))
    markers = ARTIFACTS[name]
    return not markers or any(os.path.exists(os.path.join(dirpath, m)) for m in markers)


def scan_project(path):
    """-> (size, newest_mtime, [(artifact_path, size)])"""
    size, newest, artifacts = 0, 0.0, []
    st = _lstat(path)
    if st:
        newest = st.st_mtime
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    size += st.st_blocks * 512
                    if stat.S_ISDIR(st.st_mode):
                        if is_artifact(d, e.name):
                            a = tree_size(e.path)
                            size += a
                            artifacts.append((e.path, a))
                        else:
                            newest = max(newest, st.st_mtime)
                            stack.append(e.path)
                    else:
                        # reads count as use too (file atime; our scans only stat, never read)
                        newest = max(newest, st.st_mtime, st.st_atime)
        except OSError:
            pass
    return size, newest, artifacts


def my_exes():
    out = set()
    me = os.getuid()
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                if os.stat(f"/proc/{pid}").st_uid == me:
                    out.add(os.readlink(f"/proc/{pid}/exe"))
            except OSError:
                pass
    return out


def my_paths():
    """Paths this user's own processes are using: cwd, executable, absolute paths in
    their command line, and open files. A server started from anywhere (nohup, tmux,
    `node ~/projects/x/server.js`) thereby protects the project it runs from.
    Other users' /proc entries are unreadable anyway."""
    out = set()
    me = os.getuid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        base = f"/proc/{pid}"
        try:
            if os.stat(base).st_uid != me:
                continue
            out.add(os.readlink(base + "/cwd"))
            out.add(os.readlink(base + "/exe"))
            with open(base + "/cmdline", "rb") as f:
                for arg in f.read().decode(errors="replace").split("\0"):
                    for part in (arg, arg.partition("=")[2]):
                        if part.startswith("/"):
                            out.add(os.path.normpath(part))
            for fd in os.listdir(base + "/fd"):
                t = os.readlink(f"{base}/fd/{fd}")
                if t.startswith("/"):
                    out.add(t.removesuffix(" (deleted)"))
        except OSError:
            pass
    return out


def service_paths(home):
    """Paths referenced by this user's `box serve` units (WorkingDirectory and absolute
    paths in ExecStart). Counted even while the service is stopped, failed, or between
    restarts, so a registered server's project is never deleted. `box serve rm` releases it."""
    import shlex
    out = set()
    udir = os.path.join(home, ".config", "systemd", "user")
    try:
        names = [n for n in os.listdir(udir) if n.startswith("sb-") and n.endswith(".service")]
    except OSError:
        return out
    for n in names:
        try:
            with open(os.path.join(udir, n)) as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for line in lines:
            key, _, val = line.partition("=")
            val = val.replace("$$", "$").replace("%%", "%")
            if key == "WorkingDirectory":
                out.add(os.path.normpath(val))
            elif key == "ExecStart":
                try:
                    args = shlex.split(val)
                except ValueError:
                    continue
                for arg in args:
                    for part in (arg, arg.partition("=")[2]):
                        if part.startswith("/"):
                            out.add(os.path.normpath(part))
    return out


def _inside(path, cwds):
    p = path.rstrip("/") + "/"
    return any((c + "/").startswith(p) for c in cwds)


def remove(path, dry_run):
    st = _lstat(path)
    if st is None:
        return 0
    if not stat.S_ISDIR(st.st_mode):
        size = st.st_blocks * 512
        if not dry_run:
            os.unlink(path)
        return size
    size = tree_size(path)
    if dry_run:
        return size

    def onexc(fn, p, exc):
        # read-only trees (go module cache) — make writable and retry once
        try:
            os.chmod(os.path.dirname(p), 0o700)
            if os.path.isdir(p) and not os.path.islink(p):
                os.chmod(p, 0o700)
            fn(p)
        except OSError:
            pass

    shutil.rmtree(path, onexc=onexc)
    return size


def run_pass(plan):
    home = os.path.expanduser("~")
    now = time.time()
    dry = plan.get("dry_run", False)
    ttl = plan["project_ttl_days"] * DAY
    art_ttl = plan["artifact_ttl_days"] * DAY
    cache_ttl = plan["cache_ttl_days"] * DAY
    tmp_ttl = plan["tmp_ttl_days"] * DAY
    warn = plan["warn_days"] * DAY
    usage = plan.get("usage", 0)
    budget = plan.get("budget")
    over = budget is not None and usage > budget
    deleted = []
    cwds = my_paths()
    pinned = service_paths(home)

    def rm(path, reason):
        try:
            n = remove(path, dry)
        except OSError as e:
            deleted.append({"path": path, "bytes": 0, "reason": reason, "error": str(e)})
            return 0
        deleted.append({"path": path, "bytes": n, "reason": reason})
        return n

    # 1. caches: stale entries always; everything when over budget
    for rel in CACHE_DIRS:
        d = os.path.join(home, rel)
        if not _is_real_dir(d):
            continue
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if over or plan.get("force_caches") or now - last_used(e.path) > cache_ttl:
                rm(e.path, "cache" + (" (over budget)" if over else ""))

    # 2. /tmp and /var/tmp leftovers owned by me
    me = os.getuid()
    for t in plan.get("tmp_dirs", ["/tmp", "/var/tmp"]):
        try:
            entries = list(os.scandir(t))
        except OSError:
            continue
        for e in entries:
            if e.name.startswith(TMP_SKIP_PREFIX):
                continue
            st = _lstat(e.path)
            if not st or st.st_uid != me or stat.S_ISSOCK(st.st_mode):
                continue
            if now - last_used(e.path) > tmp_ttl and not _inside(e.path, cwds):
                rm(e.path, "tmp")

    # 2b. old T3 Code versions (~360 MB each): keep the 2 newest and anything running
    vdir = os.path.join(home, ".t3", "runtime", "versions")
    if _is_real_dir(vdir):
        exes = my_exes()
        vers = sorted((e for e in os.scandir(vdir) if _is_real_dir(e.path)),
                      key=lambda e: _lstat(e.path).st_mtime, reverse=True)
        for e in vers[2:]:
            if not any(x.startswith(e.path + "/") for x in exes):
                rm(e.path, "old T3 Code version")

    # 3. projects
    projects = []
    pdir = os.path.join(home, "projects")
    if _is_real_dir(pdir):
        for e in sorted(os.scandir(pdir), key=lambda e: e.name):
            if not _is_real_dir(e.path):
                continue
            size, newest, arts = scan_project(e.path)
            idle = now - newest
            active = _inside(e.path, cwds) or _inside(e.path, pinned)
            projects.append({"name": e.name, "path": e.path, "size": size,
                             "idle_days": round(idle / DAY, 1), "active": active,
                             "artifacts": arts, "newest": newest})

    freed = sum(d["bytes"] for d in deleted)
    for p in projects:
        if p["active"]:
            continue
        if p["idle_days"] * DAY > ttl:
            p["deleted"] = True
            freed += rm(p["path"], f"project idle {p['idle_days']}d (ttl {plan['project_ttl_days']}d)")
            continue
        if over or p["idle_days"] * DAY > art_ttl:
            for apath, _ in p["artifacts"]:
                freed += rm(apath, "build artifacts of idle project")

    # 4. still over budget after the grace period: oldest idle projects go first
    if over and plan.get("over_budget_delete"):
        est = usage - freed
        for p in sorted(projects, key=lambda p: -p["idle_days"]):
            if est <= budget:
                break
            if p.get("deleted") or p["active"] or p["idle_days"] < 1:
                continue
            p["deleted"] = True
            est -= rm(p["path"], "over disk budget, oldest idle project")

    report_projects = []
    for p in projects:
        if p.get("deleted"):
            continue
        r = {k: p[k] for k in ("name", "size", "idle_days", "active")}
        if not p["active"] and p["idle_days"] * DAY > ttl - warn:
            r["delete_on"] = int(p["newest"] + ttl)
        report_projects.append(r)

    return {"projects": report_projects, "deleted": deleted,
            "freed": sum(d["bytes"] for d in deleted), "dry_run": dry}
