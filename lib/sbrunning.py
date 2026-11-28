"""What is running for one account: containers, databases, servers, agents, terminals, with memory and age.

Everything is read from the account's cgroup tree (/sys/fs/cgroup/user.slice/user-UID.slice), which anyone may
read, plus /proc for process names and start times. Container names come from rootless Docker's own files in the
account's home (~/.local/share/docker/containers/ID/config.v2.json), so only the account itself or root sees them;
anyone else gets the container's process names instead.

Used by `box running` and `box status --agent` (as the user), the dashboard's "Running" card (through
`box _portal status`, as the user) and the owner's "Running" tab (root, every account).
Stdlib only, no subprocesses: it runs at every agent session start.
"""

import json
import os
import re
import stat
import time

CGROUP_ROOT = "/sys/fs/cgroup"
DOCKER_SCOPE_RE = re.compile(r"^docker-([0-9a-f]{64})\.scope$")
DB_RE = re.compile(r"mongo|postgres|supabase|mysql|mariadb|redis|valkey|clickhouse|elastic|opensearch|qdrant|neo4j|"
                   r"cassandra|couchdb|influx|meilisearch|typesense|surreal|dolt|cockroach|etcd|minio|rabbitmq|kafka|"
                   r"nats-server|memcached|chroma|weaviate|milvus|timescale|pocketbase", re.I)
AGENT_RE = re.compile(r"claude|codex|opencode|gemini|antigravity|aider|cursor-agent", re.I)
# the account's own plumbing: shown as one "background" line, never stoppable from here
SYSTEM_UNITS = re.compile(r"^(init\.scope|docker\.service|dbus\.service|dbus-broker\.service|pipewire.*|wireplumber.*|"
                          r"xdg-.*|at-spi.*|gpg-agent.*|ssh-agent.*|gnome-keyring.*|gvfs.*|evolution.*|tracker.*|"
                          r"systemd-.*|containerd\.service|snap\..*|filter-chain.*|sharedbox-.*|"
                          r"sb-t3-proxy\.service)$")
MAX_PIDS = 200          # per cgroup, for names and start times
KEY_RE = re.compile(r"^[cu]:[A-Za-z0-9][A-Za-z0-9_.@:-]{0,160}$")
COMPOSE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")      # what compose itself allows as a project name
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")   # docker container names
META_MAX = 256 << 10


def clean(s, n=80):
    """Friend-controlled text (names, process names) for pages, terminals and agents' instructions: one line of
    printable characters, no backticks."""
    return "".join(c if c.isprintable() and c != "`" else " " for c in str(s or ""))[:n].strip()


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _int(path):
    v = (_read(path) or "").split()
    try:
        return int(v[0]) if v and v[0] != "max" else None
    except ValueError:
        return None


def mem_split(path):
    """(in use, reclaimable cache) bytes for one cgroup, from memory.stat; None if it can't be read.
    In use = what programs really hold: anonymous memory + shmem (tmpfs/shared memory can't just be dropped) +
    kernel memory that isn't a cache (kernel - slab_reclaimable). Cache = file pages - shmem + slab_reclaimable:
    page cache and dentry/inode caches the kernel frees by itself when anyone needs the memory. memory.current
    counts both, so a slice whose programs exited long ago can look like it "uses" gigabytes (system.slice on a
    box that ran a few big apt/docker jobs: anon 190M, kernel 390M, file 4.6G)."""
    st = {}
    for line in (_read(os.path.join(path, "memory.stat")) or "").splitlines():
        k, _, v = line.partition(" ")
        try:
            st[k] = int(v)
        except ValueError:
            pass
    return split_stat(st)


def split_stat(st):
    """mem_split from an already-parsed memory.stat ({key: bytes}); None without "anon". lib/sbtelemetry.py uses
    this too, so telemetry and the dashboard count memory the same way."""
    if "anon" not in st:
        return None
    shmem, slab_r = st.get("shmem", 0), st.get("slab_reclaimable", 0)
    if "kernel" in st:
        kernel = max(0, st["kernel"] - slab_r)
    else:  # kernels before 5.18 have no "kernel" line: add up the parts that aren't caches
        kernel = sum(st.get(k, 0) for k in ("kernel_stack", "pagetables", "percpu", "sock", "slab_unreclaimable",
                                            "vmalloc"))
    return st["anon"] + shmem + kernel, max(0, st.get("file", 0) - shmem) + slab_r


def mem_used(path):
    """Memory in use (see mem_split), falling back to memory.current when memory.stat can't be read."""
    m = mem_split(path)
    return m[0] if m else (_int(os.path.join(path, "memory.current")) or 0)


# ---------- what a `box serve` server is for (its unit file's X-Treehouse-* lines; systemd ignores X- keys) ----------
# Written by `box serve add/describe/keep/hackathon`, read by `box serve ls`, the dashboard, the owner's All running
# tab, the janitor (never stops a keep-running server) and hackathon mode (pauses only servers that allow it).
META_KEYS = {"About": "about", "Project": "project", "By": "by", "Agent": "agent", "Thread": "thread",
             "Created": "created", "Importance": "importance", "Url": "url", "KeepRunning": "keep",
             "Hackathon": "hackathon"}


def meta_value(s, n=200):
    """One line, no control characters, safe for a unit file (systemd's % and $ specifiers are doubled on write)."""
    return clean(s, n)


def parse_unit_meta(text):
    """{about, project, by, agent, thread, created, importance, url, keep, hackathon, exec, port, workdir} from a
    `box serve` unit file's text. keep: bool. hackathon: "pause" (may be stopped while hackathon mode is on) or ""."""
    out = {}
    for line in (text or "").splitlines():
        k, _, v = line.partition("=")
        v = v.strip().replace("%%", "%").replace("$$", "$")
        if k.startswith("X-Treehouse-") and k[12:] in META_KEYS:
            out[META_KEYS[k[12:]]] = clean(v, 300)
        elif k == "ExecStart":
            out["exec"] = clean(v, 300)
        elif k == "WorkingDirectory":
            out["workdir"] = clean(v, 300)
        elif k == "Environment" and v.startswith("PORT="):
            out["port"] = clean(v[5:], 8)
    out["keep"] = out.get("keep", "").lower() in ("yes", "1", "true")
    out["hackathon"] = "pause" if out.get("hackathon", "") == "pause" and not out["keep"] else ""
    if out.get("importance") not in ("important", "safe-to-stop"):
        out["importance"] = "important" if out["keep"] else ""
    return out


def server_meta(home, name, uid=None):
    """parse_unit_meta of ~/.config/systemd/user/sb-NAME.service; {} when unreadable. With uid (root reading someone
    else's home) every step must be owned by that account (see _read_owned)."""
    if not NAME_RE.match(name or ""):
        return {}
    parts = [".config", "systemd", "user", f"sb-{name}.service"]
    text = _read_owned(home, parts, uid) if uid is not None else _read(os.path.join(home or "", *parts))
    return parse_unit_meta(text) if text else {}


def _boot_time():
    for line in (_read("/proc/stat") or "").splitlines():
        if line.startswith("btime "):
            return int(line.split()[1])
    return 0


_HZ = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _proc(pid, boot):
    """(comm, start epoch, rss bytes) or None if it's gone."""
    st = _read(f"/proc/{pid}/stat")
    if not st or ")" not in st:
        return None
    stat = st
    comm = clean(stat[stat.index("(") + 1:stat.rindex(")")], 16)
    rest = stat[stat.rindex(")") + 2:].split()
    try:
        start = boot + int(rest[19]) / _HZ
        rss = int(rest[21]) * _PAGE
    except (IndexError, ValueError):
        return None
    return comm, start, rss


USER_MANAGER_RE = re.compile(r"^user@\d+\.service$")


def _unit_of(root, path):
    """The unit a cgroup belongs to: its first .service/.scope below the account's slice (user@UID.service and
    slices are walked through). Cgroups a container or a service made inside itself (systemd in a container,
    kind/k3d, docker-in-docker) belong to that container or service, never items of their own."""
    acc = root
    for part in os.path.relpath(path, root).split(os.sep):
        acc = os.path.join(acc, part)
        if part.endswith((".service", ".scope")) and not USER_MANAGER_RE.match(part):
            return acc
    return path


def _leaves(root):
    """[(unit cgroup path, pids)] for every unit under root with processes anywhere inside it."""
    units = {}
    for dirpath, dirnames, _ in os.walk(root):
        procs = (_read(os.path.join(dirpath, "cgroup.procs")) or "").split()
        if procs:
            units.setdefault(_unit_of(root, dirpath), []).extend(procs)
    return list(units.items())


def _cg_stats(path, procs, boot):
    usage = 0
    for line in (_read(os.path.join(path, "cpu.stat")) or "").splitlines():
        if line.startswith("usage_usec "):
            usage = int(line.split()[1])
    names, start = {}, None
    for pid in procs[:MAX_PIDS]:
        p = _proc(pid, boot)
        if not p:
            continue
        comm, st, rss = p
        names[comm] = names.get(comm, 0) + rss
        start = st if start is None else min(start, st)
    return {"mem": mem_used(path), "cpu_usec": usage,
            "since": int(start) if start else None, "pids": len(procs),
            "procs": [n for n, _ in sorted(names.items(), key=lambda kv: -kv[1])][:4]}


def _read_owned(home, parts, uid):
    """Text of home/parts... only if every step is a real directory/file owned by uid (or root's home dirs above
    it), never a link, FIFO or device, and the file is at most META_MAX. Root reads friends' files with this:
    a friend controls everything under their home."""
    try:
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | (0 if last else os.O_DIRECTORY)
            nfd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nfd
            st = os.fstat(fd)
            if uid is not None and st.st_uid != uid:
                return None
            if last:
                if not stat.S_ISREG(st.st_mode) or st.st_size > META_MAX or st.st_nlink != 1:
                    return None
                return os.read(fd, META_MAX + 1)[:META_MAX].decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)
    return None


def _docker_meta(home, cid, uid=None):
    """{name, image, project} from rootless Docker's files, or {} when they can't be read (not our home) or
    look wrong. Everything in them is the account's own (untrusted) text."""
    if not home or not re.fullmatch(r"[0-9a-f]{64}", cid or ""):
        return {}
    raw = _read_owned(home, [".local", "share", "docker", "containers", cid, "config.v2.json"], uid)
    if not raw:
        return {}
    try:
        c = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(c, dict):
        return {}
    conf = c.get("Config") if isinstance(c.get("Config"), dict) else {}
    labels = conf.get("Labels") if isinstance(conf.get("Labels"), dict) else {}
    name = str(c.get("Name") or "").lstrip("/")
    project = str(labels.get("com.docker.compose.project") or "")
    return {"name": name if NAME_RE.match(name) else "",
            "image": clean(conf.get("Image"), 120),
            "project": project if COMPOSE_RE.match(project) else ""}


def _short_image(image):
    tail = image.rsplit("/", 1)[-1]
    return tail[:60]


T3_SCOPE_RE = re.compile(r"^t3code-([a-z0-9]+(?:-[a-z]+)*)-[0-9a-f]{6,}\.scope$")


def _unit_kind(unit, procs):
    """(kind, display name, how to stop it or None)."""
    joined = " ".join(procs)
    if unit in ("t3code.service", "sb-t3.service"):  # stoppable only from the owner's All running tab
        return "t3", "T3 Code", "t3"
    if unit == "sb-t3.socket":
        return "t3", "T3 Code", None
    t3 = T3_SCOPE_RE.match(unit)  # what T3 Code starts: agents (claude, codex, opencode, acp, muse, pi, ...),
    if t3:                        # its built-in terminals and preview browsers
        what = t3.group(1)
        if what == "terminal":
            return "terminal", "T3 terminal", "unit"
        if what.startswith("preview"):
            return "app", "T3 preview browser", "unit"
        return "agent", {"claude": "Claude", "codex": "Codex", "opencode": "OpenCode", "acp": "Agent",
                         "gemini": "Gemini"}.get(what, "Agent") + " session", None
    m = re.match(r"^sb-([A-Za-z0-9_-]+)\.service$", unit)
    if m:
        return ("database" if DB_RE.search(joined) else "server"), m.group(1), "server"
    if AGENT_RE.search(unit) or (unit.startswith("t3code-") and AGENT_RE.search(joined)):
        agent = next((a for a in ("claude", "codex", "opencode", "gemini", "antigravity") if a in unit.lower()
                      or a in joined.lower()), "agent")
        return "agent", {"claude": "Claude", "codex": "Codex", "opencode": "OpenCode", "gemini": "Gemini",
                         "antigravity": "Antigravity"}.get(agent, "Agent") + " session", None
    if unit.startswith("tmux") or "tmux" in procs:
        return "terminal", "tmux terminal", "unit"
    if re.match(r"^session-\d+\.scope$", unit):
        return "login", "SSH login", None
    if re.search(r"chrom|firefox|playwright", unit + " " + joined, re.I):
        return "app", "Browser", "unit"
    if DB_RE.search(joined):
        return "database", unit.rsplit(".", 1)[0], "unit"
    if unit.endswith(".service"):
        return "service", unit[:-8], "unit"
    return "app", re.sub(r"^(app-|run-)", "", unit.rsplit(".", 1)[0])[:60], "unit"


def running(uid, home=None, now=None):
    """Everything running for this account, biggest first:
    [{key, kind, name, detail, mem, cpu_usec, since, pids, stop, procs, [ids]}]
    kind: database | container | server | agent | t3 | terminal | app | service | login | background.
    stop: "containers" | "server" | "unit" | None. key identifies the item for `box _portal running-stop`."""
    now = now or time.time()
    root = os.path.join(CGROUP_ROOT, "user.slice", f"user-{uid}.slice")
    if not os.path.isdir(root):
        return []
    boot = _boot_time()
    items, groups, background = [], {}, None
    for path, procs in _leaves(root):
        unit = os.path.basename(path)
        st = _cg_stats(path, procs, boot)
        m = DOCKER_SCOPE_RE.match(unit)
        if m:
            cid = m.group(1)
            meta = _docker_meta(home, cid, uid)
            proj = meta.get("project")
            gkey = "c:" + (proj or cid[:12])
            g = groups.get(gkey)
            if not g:
                g = groups[gkey] = {"key": gkey, "kind": "container", "name": proj or meta.get("name") or cid[:12],
                                    "detail": "", "mem": 0, "cpu_usec": 0, "since": None, "pids": 0, "stop": "containers",
                                    "procs": [], "ids": [], "names": [], "images": []}
            g["ids"].append(cid)
            g["names"].append(meta.get("name") or cid[:12])
            if meta.get("image"):
                g["images"].append(meta["image"])
            for k in ("mem", "cpu_usec", "pids"):
                g[k] += st[k]
            if st["since"]:
                g["since"] = st["since"] if g["since"] is None else min(g["since"], st["since"])
            g["procs"] += [p for p in st["procs"] if p not in g["procs"]]
            continue
        if SYSTEM_UNITS.match(unit) or (unit.endswith(".slice") and not procs):
            if background is None:
                background = {"key": "", "kind": "background", "name": "Background", "detail": "", "mem": 0,
                              "cpu_usec": 0, "since": None, "pids": 0, "stop": None, "procs": [], "units": []}
            for k in ("mem", "cpu_usec", "pids"):
                background[k] += st[k]
            background["units"].append(unit)
            continue
        kind, name, stop = _unit_kind(unit, st["procs"])
        items.append({"key": "u:" + unit if stop and KEY_RE.match("u:" + unit) else "", "kind": kind, "name": name,
                      "detail": ", ".join(st["procs"]), "unit": unit, **st, "stop": stop if stop and
                      KEY_RE.match("u:" + unit) else None})
    for g in groups.values():
        is_db = any(DB_RE.search(x) for x in g["images"] + g["procs"] + g["names"])
        g["kind"] = "database" if is_db else "container"
        n = len(g["ids"])
        if "supabase" in " ".join(g["images"] + g["names"]).lower():
            what = "Supabase"
        elif g["images"]:
            what = _short_image(g["images"][0])
        else:
            what = ", ".join(g["procs"][:3]) or "container"
        g["detail"] = what + (f" · {n} containers" if n > 1 else "")
        items.append(g)
    if background:
        names = [u.rsplit(".", 1)[0] for u in background["units"]]
        background["detail"] = ", ".join(sorted(set("Docker engine" if n == "docker" else n for n in names)))[:200]
        items.append(background)
    for it in items:
        it["age"] = int(now - it["since"]) if it.get("since") else None
    items.sort(key=lambda x: (x["kind"] == "background", -x["mem"]))
    return items


def with_meta(items, home, uid=None):
    """Add what each `box serve` server is for (about, keep, importance, exec) from its unit file."""
    for it in items:
        unit = it.get("unit") or ""
        if unit.startswith("sb-") and unit.endswith(".service") and unit not in ("sb-t3.service", "sb-t3-proxy.service"):
            m = server_meta(home, unit[3:-8], uid)
            for k in ("about", "keep", "importance", "exec", "project", "by", "agent"):
                if m.get(k):
                    it[k] = m[k]
    return items


def find(items, key):
    """The item a dashboard button names, or None (keys are re-derived from the live tree every time)."""
    if not isinstance(key, str) or not KEY_RE.match(key):
        return None
    return next((it for it in items if it.get("key") == key and it.get("stop")), None)


def ago(seconds):
    """'3d 4h', '5h 12m', '7m' for an age in seconds."""
    if seconds is None:
        return "?"
    s = int(seconds)
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    if d:
        return f"{d}d {h}h" if h else f"{d}d"
    if h:
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{max(m, 1)}m"


KIND_LABEL = {"database": "Database", "container": "Container", "server": "Server", "agent": "Agent",
              "t3": "T3 Code", "terminal": "Terminal", "app": "App", "service": "Service", "login": "Login",
              "background": "Background"}


def cpu_avg(it):
    """Average CPU since it started, in % of one core."""
    if not it.get("age"):
        return 0.0
    return round(it.get("cpu_usec", 0) / 1e6 / max(it["age"], 1) * 100, 1)


def worth_mentioning(items, min_mem=300 << 20, min_age=6 * 3600):
    """What an agent should know is running at session start: databases and containers (any size) and anything
    else big that has been up for hours. Never agents, T3 or the background line."""
    out = []
    for it in items:
        if it["kind"] in ("agent", "t3", "background", "login"):
            continue
        if it["kind"] in ("database", "container") or (it["mem"] >= min_mem and (it.get("age") or 0) >= min_age):
            out.append(it)
    return out
