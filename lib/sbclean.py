"""Per-user cleanup pass. Always executed AS the friend (via setpriv), never as root,
so a symlink planted in a home directory can never make root delete the wrong thing.

Input: a plan dict from the janitor. Output: a report dict.

Expired projects aren't deleted: they move to ~/.sharedbox/archive/<name>-<date>/<name>/ (with a note,
ARCHIVED.txt), `box restore NAME` puts one back, and archives are deleted ARCHIVE_DAYS later (or first,
when the user is over the disk budget).
"""

import json
import os
import re
import shutil
import stat
import subprocess
import time

DAY = 86400
ARCHIVE_REL = os.path.join(".sharedbox", "archive")
ARCHIVE_META = "archived.json"
ARCHIVE_NOTE = "ARCHIVED.txt"

CACHE_DIRS = [
    ".cache", ".npm/_cacache", ".npm/_npx", ".gradle/caches", ".gradle/wrapper/dists",
    ".gradle/daemon", ".gradle/native", ".m2/repository", ".cargo/registry", ".cargo/git",
    "go/pkg/mod", ".bun/install/cache", ".local/share/pnpm/store", ".yarn/berry/cache",
    ".android/cache", ".konan/cache", ".nuget/packages", ".pub-cache/hosted",
    ".rustup/downloads", ".rustup/tmp",
]

# caches whose children are separate things that age on their own: one entry per browser build, so an old
# Playwright revision goes once nothing has used it for CACHE_TTL_DAYS, even though the folder itself is in use
CACHE_NESTED = [".cache/ms-playwright", ".cache/puppeteer"]

# content-addressed caches that are used every day (so never stale as a whole) and grow forever: their own tool
# removes what nothing refers to any more. (tool, args, cache dir it cleans)
NATIVE_PRUNERS = [
    ("uv", ["cache", "prune"], ".cache/uv"),
    ("pnpm", ["store", "prune"], ".local/share/pnpm/store"),
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


# ---------- archive ----------

def archive_root(home):
    return os.path.join(home, ARCHIVE_REL)


def list_archives(home, archive_days=14):
    """[{name, dir, path, archived_at, until}] for every archived project, newest first."""
    out = []
    root = archive_root(home)
    if not _is_real_dir(root):
        return out
    for e in os.scandir(root):
        if not _is_real_dir(e.path):
            continue
        meta = {}
        try:
            with open(os.path.join(e.path, ARCHIVE_META)) as f:
                meta = json.loads(f.read(1 << 16))
        except (OSError, ValueError):
            pass
        meta = meta if isinstance(meta, dict) else {}
        name = str(meta.get("name") or re.sub(r"-\d{4}-\d{2}-\d{2}(-\d+)?$", "", e.name))
        try:
            at = float(meta.get("archived_at") or _lstat(e.path).st_mtime)
        except (TypeError, ValueError, AttributeError):
            at = 0.0
        out.append({"name": name, "dir": e.path, "path": os.path.join(e.path, name), "archived_at": at,
                    "until": int(at + archive_days * DAY), "git_remote": meta.get("git_remote")})
    out.sort(key=lambda a: -a["archived_at"])
    return out


def git_remote(path):
    """The project's first git remote URL (credentials stripped), or None."""
    if not os.path.isdir(os.path.join(path, ".git")):
        return None
    try:
        r = subprocess.run(["git", "-C", path, "remote"], capture_output=True, text=True, timeout=20)
        names = r.stdout.split()
        if not names:
            return None
        name = "origin" if "origin" in names else names[0]
        r = subprocess.run(["git", "-C", path, "remote", "get-url", name], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    url = re.sub(r"//[^/@\s]*@", "//", r.stdout.strip())
    return url[:300] or None


_gh = None


def github_connected():
    """`gh auth status` succeeds for this user (asked once per pass)."""
    global _gh
    if _gh is None:
        try:
            _gh = subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=30).returncode == 0
        except (OSError, subprocess.SubprocessError):
            _gh = False
    return _gh


def archive_project(home, path, name, now, idle_days, archive_days, dry_run):
    """Move ~/projects/NAME into the archive. Returns (archive dir, bytes)."""
    size = tree_size(path)
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    root = archive_root(home)
    dest, n = os.path.join(root, f"{name}-{day}"), 2
    while os.path.lexists(dest):
        dest, n = os.path.join(root, f"{name}-{day}-{n}"), n + 1
    if dry_run:
        return dest, size
    remote = git_remote(path)
    gh = bool(remote) and github_connected()
    os.makedirs(root, mode=0o700, exist_ok=True)
    os.mkdir(dest, 0o700)
    try:
        os.rename(path, os.path.join(dest, name))
    except OSError:
        os.rmdir(dest)
        raise
    until = time.strftime("%a %b %d", time.localtime(now + archive_days * DAY))
    note = (f"~/projects/{name} was archived on {time.strftime('%a %b %d %Y', time.localtime(now))}: nothing in it "
            f"was used for {idle_days:g} days.\n\nRestore it until {until}: box restore {name}\n"
            f"(or ask your agent: \"restore my project {name}\"). After that it's deleted for good.\n")
    if remote:
        note += (f"\nIt's a git repository with a remote ({remote}). "
                 + ("Your GitHub login is connected here, so everything you pushed is also on GitHub."
                    if gh else "Anything you pushed is also there; commits you didn't push are only in this archive.")
                 + "\n")
    with open(os.path.join(dest, ARCHIVE_NOTE), "w") as f:
        f.write(note)
    with open(os.path.join(dest, ARCHIVE_META), "w") as f:
        json.dump({"name": name, "from": path, "archived_at": int(now), "bytes": size, "idle_days": idle_days,
                   "git_remote": remote, "github": gh}, f)
    return dest, size


def _tool(home, name):
    """The user's own copy of a tool (the janitor runs us with a bare PATH): ~/.local/bin, nvm, pnpm's home."""
    import glob
    path = os.pathsep.join([os.path.join(home, ".local", "bin"), os.path.join(home, ".local", "share", "pnpm"),
                            *sorted(glob.glob(os.path.join(home, ".nvm", "versions", "node", "*", "bin")),
                                    reverse=True), "/usr/local/bin", "/usr/bin", "/bin"])
    return shutil.which(name, path=path), path


def _tool_running(name):
    """One of my processes is that tool (an install in progress): don't prune under it."""
    me = os.getuid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != me:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read(4096).split(b"\0")[:2]
        except OSError:
            continue
        if any(os.path.basename(a.decode(errors="replace")) == name for a in argv):
            return True
    return False


def clean_caches(plan, home, now, cwds, over, dry):
    """Caches: stale entries always, everything when over budget; then native prunes. Returns deleted rows."""
    cache_ttl = plan["cache_ttl_days"] * DAY
    tmp_ttl = plan["tmp_ttl_days"] * DAY
    deleted = []

    def rm(path, reason):
        try:
            n = remove(path, dry)
        except OSError as e:
            deleted.append({"path": path, "bytes": 0, "reason": reason, "error": str(e)})
            return 0
        deleted.append({"path": path, "bytes": n, "reason": reason})
        return n

    nested = {os.path.join(home, r) for r in CACHE_NESTED}
    for rel in CACHE_DIRS + CACHE_NESTED:
        d = os.path.join(home, rel)
        if not _is_real_dir(d):
            continue
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if e.path in nested and not over:
                continue  # judged entry by entry below
            if _inside(e.path, cwds):  # a running build/daemon (e.g. ~/.gradle/daemon) is using it
                continue
            if over or plan.get("force_caches") or now - last_used(e.path) > cache_ttl:
                rm(e.path, "cache" + (" (over budget)" if over else ""))

    for name, args, rel in NATIVE_PRUNERS:
        d = os.path.join(home, rel)
        exe, path = _tool(home, name)
        if dry or not exe or not _is_real_dir(d) or _tool_running(name):
            continue
        before = tree_size(d)
        try:
            subprocess.run([exe, *args], cwd=home, env={**os.environ, "PATH": path, "UV_LOCK_TIMEOUT": "10"}, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            deleted.append({"path": d, "bytes": 0, "reason": f"{name} prune", "error": str(e)[:200]})
            continue
        freed = max(0, before - tree_size(d))
        if freed:
            deleted.append({"path": d, "bytes": freed, "reason": f"{name} prune (entries nothing uses)"})

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

    return deleted


def run_cache_pass(plan):
    """Owner accounts: caches, tmp leftovers and old T3 versions only. Their projects are never archived and they
    have no enforced budget, but caches that nothing uses still go."""
    home = os.path.expanduser("~")
    deleted = clean_caches(plan, home, time.time(), my_paths(), False, plan.get("dry_run", False))
    return {"deleted": deleted, "freed": sum(d["bytes"] for d in deleted), "dry_run": plan.get("dry_run", False)}


def run_pass(plan):
    home = os.path.expanduser("~")
    now = time.time()
    dry = plan.get("dry_run", False)
    ttl = plan["project_ttl_days"] * DAY
    art_ttl = plan["artifact_ttl_days"] * DAY
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

    deleted += clean_caches(plan, home, now, cwds, over, dry)

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

    # 3b. archives past ARCHIVE_DAYS are deleted
    archive_days = plan.get("archive_days", 14)
    archives = list_archives(home, archive_days)
    for a in list(archives):
        if now > a["until"]:
            rm(a["dir"], f"archived project {a['name']} expired ({archive_days}d)")
            archives.remove(a)

    freed = sum(d["bytes"] for d in deleted)
    archived = []
    for p in projects:
        if p["active"]:
            continue
        if p["idle_days"] * DAY > ttl:
            # moved, not deleted: `box restore NAME` for ARCHIVE_DAYS. Build outputs go now (regenerable).
            for apath, _ in p["artifacts"]:
                freed += rm(apath, "build artifacts of archived project")
            try:
                dest, n = archive_project(home, p["path"], p["name"], now, p["idle_days"], archive_days, dry)
            except OSError as e:
                deleted.append({"path": p["path"], "bytes": 0, "reason": "archive failed", "error": str(e)})
                continue
            p["deleted"] = True
            archived.append({"name": p["name"], "path": dest, "bytes": n})
            archives.insert(0, {"name": p["name"], "dir": dest, "path": os.path.join(dest, p["name"]),
                                "archived_at": now, "until": int(now + archive_days * DAY)})
            continue
        if over or p["idle_days"] * DAY > art_ttl:
            for apath, _ in p["artifacts"]:
                freed += rm(apath, "build artifacts of idle project")

    # 4. still over budget after the grace period: archives go first (oldest first), then the oldest idle projects
    if over and plan.get("over_budget_delete"):
        est = usage - freed
        for a in sorted(archives, key=lambda a: a["archived_at"]):
            if est <= budget:
                break
            est -= rm(a["dir"], f"over disk budget, archived project {a['name']}")
            archives.remove(a)
        for p in sorted(projects, key=lambda p: -p["idle_days"]):
            if est <= budget:
                break
            # only projects already past the TTL (they'd be archived anyway): over budget never takes recent work
            if p.get("deleted") or p["active"] or p["idle_days"] * DAY < ttl:
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

    return {"projects": report_projects, "deleted": deleted, "archived": archived,
            "archives": [{"name": a["name"], "until": int(a["until"])} for a in archives],
            "freed": sum(d["bytes"] for d in deleted), "dry_run": dry}
