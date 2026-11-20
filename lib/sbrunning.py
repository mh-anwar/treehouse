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


def _boot_time():
    for line in (_read("/proc/stat") or "").splitlines():
        if line.startswith("btime "):
            return int(line.split()[1])
    return 0


_HZ = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _proc(pid, boot):
    """(comm, start epoch, rss bytes) or None if it's gone."""
    stat = _read(f"/proc/{pid}/stat")
    if not stat or ")" not in stat:
        return None
    comm = stat[stat.index("(") + 1:stat.rindex(")")]
    rest = stat[stat.rindex(")") + 2:].split()
    try:
        start = boot + int(rest[19]) / _HZ
        rss = int(rest[21]) * _PAGE
    except (IndexError, ValueError):
        return None
    return comm, start, rss


def _leaves(root):
    """Every cgroup under root that has processes in it (nested slices are walked through)."""
    out = []
    for dirpath, dirnames, _ in os.walk(root):
        procs = (_read(os.path.join(dirpath, "cgroup.procs")) or "").split()
        if procs:
            out.append((dirpath, procs))
    return out


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
    return {"mem": _int(os.path.join(path, "memory.current")) or 0, "cpu_usec": usage,
            "since": int(start) if start else None, "pids": len(procs),
            "procs": [n for n, _ in sorted(names.items(), key=lambda kv: -kv[1])][:4]}


def _docker_meta(home, cid):
    """{name, image, project} from rootless Docker's files, or {} when they can't be read (not our home)."""
    if not home:
        return {}
    for root in (os.path.join(home, ".local", "share", "docker"),):
        raw = _read(os.path.join(root, "containers", cid, "config.v2.json"))
        if raw:
            try:
                c = json.loads(raw)
            except ValueError:
                return {}
            labels = (c.get("Config") or {}).get("Labels") or {}
            return {"name": str(c.get("Name") or "").lstrip("/")[:80],
                    "image": str((c.get("Config") or {}).get("Image") or "")[:120],
                    "project": str(labels.get("com.docker.compose.project") or "")[:80]}
    return {}


def _short_image(image):
    tail = image.rsplit("/", 1)[-1]
    return tail[:60]


def _unit_kind(unit, procs):
    """(kind, display name, how to stop it or None)."""
    joined = " ".join(procs)
    if unit in ("t3code.service",) or unit.startswith("sb-t3"):
        return "t3", "T3 Code", None
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
            meta = _docker_meta(home, cid)
            proj = meta.get("project")
            gkey = "c:" + (proj if proj and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", proj) else cid[:12])
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
