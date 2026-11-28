"""Box telemetry: what each treehouse box was doing, minute by minute, kept for a long time, so the owner (and any
agent they ask) can find bad patterns later: what froze a box, who ran out of memory, which agents loop, disk filling
up, services crash-looping.

Collection (root, `sharedbox-health sample`, every minute, nice 10 + idle IO): plain /proc, /sys and cgroup reads,
one `journalctl` call that starts where the last one stopped, and nothing else. No agent or LLM is ever involved;
the detectors are SQL plus a little arithmetic. Agents come in only when someone asks one to read the data.

Storage: one SQLite file per box, STATE_DIR/telemetry/telemetry.db (WAL while the collector writes). Raw rows are
kept TELEMETRY_RAW_DAYS (14), hourly roll-ups and events TELEMETRY_KEEP_DAYS (400); the file is capped at
TELEMETRY_MAX_MB (oldest raw days go first). Privacy is plain file permissions:
  telemetry.db          everything; root:<owner's private group> 0640 (the owner reads it, nobody else)
  users/<name>.db       the same schema holding only that account's rows plus the box-wide ones;
                        root:<their private group> 0640
Readers open these read-only and immutable (the directory isn't theirs, so they can't create the -shm file a WAL
reader needs); the collector checkpoints and closes within a second each minute, and readers retry if the file
changed under them.

What is stored about processes: the kernel's short name (comm) and, for interpreters (node, python, ...), the name
of a known tool from an allowlist (`node vite`, `python3 pytest`). Never arguments, paths, prompts or environment.

Fleet: `box telemetry pull` copies what the account can read on every box (all of it for the owner, their own rows
for anyone else) into ~/me/telemetry.db, over the account's own federation SSH login, like `box me pull`.

Plan usage (optional, `box telemetry usage on`): the account's own janitor tick reads the account's own Claude
login (~/.claude/.credentials.json, never anyone else's) every ~10 minutes, records the plan windows into
~/me/telemetry.db and sends the account a `box msg` (which Telegram relays) at 80% and 90%.

Stdlib only.
"""

import functools
import glob
import json
import os
import pwd
import re
import socket
import sqlite3
import subprocess
import sys
import time

import sbrunning

HZ = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
MB = 1 << 20
SCHEMA_VERSION = 1
GAP_S = 180               # two samples further apart than this (same boot) = the box stalled or sampling stopped
MAX_INTERVAL_S = 900      # deltas over longer gaps are meaningless
TOP_N = 12                # processes kept per sample, by CPU and by memory (each)
NICE = ("nice", "-n", "15", "ionice", "-c", "3")
JOURNAL_FIRST_LOOKBACK = "-24h"
JOURNAL_PATTERN = ("Out of memory|oom-kill:|Killed process|sending SIG|Failed with result|Scheduled restart job|"
                   "killed by the OOM|due to memory|blocked for more than|I/O error|Start request repeated|"
                   "EXT4-fs error|critical medium error")
RAW_TABLES = ("samples", "disks", "diskio", "slices", "procs", "agents", "containers")
HOURLY_TABLES = tuple(t + "_hourly" for t in RAW_TABLES)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS samples(
  ts INTEGER NOT NULL, box TEXT NOT NULL, interval_s REAL,
  load1 REAL, load5 REAL, load15 REAL, ncpu INTEGER, cpu_pct REAL, iowait_pct REAL, steal_pct REAL,
  procs INTEGER, procs_running INTEGER, procs_blocked INTEGER,
  psi_cpu REAL, psi_mem REAL, psi_mem_full REAL, psi_io REAL, psi_io_full REAL,
  mem_total_mb INTEGER, mem_used_mb INTEGER, mem_avail_mb INTEGER, mem_anon_mb INTEGER, mem_cache_mb INTEGER,
  mem_shmem_mb INTEGER, swap_total_mb INTEGER, swap_used_mb INTEGER, swap_in_mb REAL, swap_out_mb REAL,
  majfaults INTEGER, oom_kills INTEGER, disk_total_gb REAL, disk_free_gb REAL, temp_max_c REAL, cpu_mhz INTEGER,
  throttles INTEGER, collect_ms INTEGER,
  PRIMARY KEY(ts, box)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS disks(ts INTEGER NOT NULL, box TEXT NOT NULL, mount TEXT NOT NULL, dev TEXT,
  total_gb REAL, free_gb REAL, PRIMARY KEY(ts, box, mount)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS diskio(ts INTEGER NOT NULL, box TEXT NOT NULL, dev TEXT NOT NULL,
  read_mb REAL, write_mb REAL, util_pct REAL, PRIMARY KEY(ts, box, dev)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS slices(ts INTEGER NOT NULL, box TEXT NOT NULL, slice TEXT NOT NULL, user TEXT NOT NULL,
  cpu_pct REAL, mem_mb INTEGER, cache_mb INTEGER, swap_mb INTEGER, pids INTEGER, psi_mem REAL, psi_mem_full REAL,
  oom_kills INTEGER, mem_high_events INTEGER, mem_max_events INTEGER,
  PRIMARY KEY(ts, box, slice)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS slices_user ON slices(user, ts);
CREATE TABLE IF NOT EXISTS procs(ts INTEGER NOT NULL, box TEXT NOT NULL, user TEXT NOT NULL, pid INTEGER NOT NULL,
  name TEXT, cpu_pct REAL, rss_mb INTEGER, age_s INTEGER, PRIMARY KEY(ts, box, pid)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS procs_user ON procs(user, ts);
CREATE TABLE IF NOT EXISTS agents(ts INTEGER NOT NULL, box TEXT NOT NULL, user TEXT NOT NULL, agent TEXT,
  pid INTEGER NOT NULL, started INTEGER, cpu_pct REAL, rss_mb INTEGER, tree_cpu_pct REAL, tree_rss_mb INTEGER,
  tree_procs INTEGER, PRIMARY KEY(ts, box, pid)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS agents_user ON agents(user, ts);
CREATE TABLE IF NOT EXISTS containers(ts INTEGER NOT NULL, box TEXT NOT NULL, user TEXT NOT NULL, id TEXT NOT NULL,
  name TEXT, cpu_pct REAL, mem_mb INTEGER, peak_mb INTEGER, PRIMARY KEY(ts, box, id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS events(ts REAL NOT NULL, box TEXT NOT NULL, kind TEXT NOT NULL,
  user TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', value REAL,
  UNIQUE(box, ts, kind, user, unit, detail));
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, ts);

CREATE TABLE IF NOT EXISTS samples_hourly(ts INTEGER NOT NULL, box TEXT NOT NULL, n INTEGER,
  load1 REAL, load1_max REAL, cpu_pct REAL, cpu_pct_max REAL, iowait_pct REAL, iowait_pct_max REAL,
  psi_cpu REAL, psi_cpu_max REAL, psi_mem REAL, psi_mem_max REAL, psi_mem_full REAL, psi_mem_full_max REAL,
  psi_io REAL, psi_io_max REAL, psi_io_full REAL, psi_io_full_max REAL, mem_used_mb REAL, mem_used_mb_max REAL,
  mem_cache_mb REAL, mem_cache_mb_max REAL, swap_used_mb REAL, swap_used_mb_max REAL, temp_max_c REAL,
  temp_max_c_max REAL, procs REAL, procs_max REAL, procs_blocked REAL, procs_blocked_max REAL,
  swap_in_mb REAL, swap_out_mb REAL, majfaults INTEGER, oom_kills INTEGER, throttles INTEGER,
  disk_free_gb REAL, disk_total_gb REAL, mem_total_mb INTEGER, ncpu INTEGER,
  PRIMARY KEY(ts, box)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS disks_hourly(ts INTEGER NOT NULL, box TEXT NOT NULL, mount TEXT NOT NULL, dev TEXT,
  total_gb REAL, free_gb REAL, PRIMARY KEY(ts, box, mount)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS diskio_hourly(ts INTEGER NOT NULL, box TEXT NOT NULL, dev TEXT NOT NULL,
  read_mb REAL, write_mb REAL, util_pct REAL, util_pct_max REAL, PRIMARY KEY(ts, box, dev)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS slices_hourly(ts INTEGER NOT NULL, box TEXT NOT NULL, slice TEXT NOT NULL,
  user TEXT NOT NULL, n INTEGER, cpu_pct REAL, cpu_pct_max REAL, mem_mb REAL, mem_mb_max REAL, cache_mb REAL,
  swap_mb_max REAL, pids_max INTEGER, psi_mem REAL, psi_mem_max REAL, psi_mem_full_max REAL, oom_kills INTEGER,
  mem_high_events INTEGER, mem_max_events INTEGER, PRIMARY KEY(ts, box, slice)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS slices_hourly_user ON slices_hourly(user, ts);
CREATE TABLE IF NOT EXISTS procs_hourly(ts INTEGER NOT NULL, box TEXT NOT NULL, user TEXT NOT NULL,
  name TEXT NOT NULL, n INTEGER, cpu_s REAL, cpu_pct_max REAL, rss_mb_max INTEGER,
  PRIMARY KEY(ts, box, user, name)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS agents_hourly(ts INTEGER NOT NULL, box TEXT NOT NULL, user TEXT NOT NULL,
  agent TEXT NOT NULL, sessions REAL, sessions_max INTEGER, cpu_s REAL, tree_cpu_s REAL, rss_mb_max INTEGER,
  tree_rss_mb_max INTEGER, PRIMARY KEY(ts, box, user, agent)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS containers_hourly(ts INTEGER NOT NULL, box TEXT NOT NULL, user TEXT NOT NULL,
  name TEXT NOT NULL, n INTEGER, cpu_s REAL, mem_mb_max INTEGER, peak_mb_max INTEGER,
  PRIMARY KEY(ts, box, user, name)) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS plan_usage(ts INTEGER NOT NULL, box TEXT NOT NULL, user TEXT NOT NULL,
  window TEXT NOT NULL, utilization REAL, resets_at TEXT, PRIMARY KEY(ts, user, window)) WITHOUT ROWID;
"""

# Primary-key columns per table (imports from other boxes replace rows by these); events dedupe by UNIQUE.
KEYS = {"samples": ("ts", "box"), "disks": ("ts", "box", "mount"), "diskio": ("ts", "box", "dev"),
        "slices": ("ts", "box", "slice"), "procs": ("ts", "box", "pid"), "agents": ("ts", "box", "pid"),
        "containers": ("ts", "box", "id"), "samples_hourly": ("ts", "box"), "disks_hourly": ("ts", "box", "mount"),
        "diskio_hourly": ("ts", "box", "dev"), "slices_hourly": ("ts", "box", "slice"),
        "procs_hourly": ("ts", "box", "user", "name"), "agents_hourly": ("ts", "box", "user", "agent"),
        "containers_hourly": ("ts", "box", "user", "name"), "plan_usage": ("ts", "user", "window"),
        "events": ("box", "ts", "kind", "user", "unit", "detail")}
EXPORT_TABLES = RAW_TABLES + HOURLY_TABLES + ("events", "plan_usage")

AGENT_COMMS = {"claude": "claude", "codex": "codex", "opencode": "opencode", "gemini": "gemini",
               "antigravity": "antigravity", "agy": "antigravity", "aider": "aider", "cursor-agent": "cursor",
               "goose": "goose", "amp": "amp"}
# node/bun-run agents show up as `node`: their script path says which one (only the verdict is stored)
AGENT_ARGV_RE = re.compile(r"(?:^|/)(claude|codex|opencode|gemini|antigravity)(?:\.m?js)?$|/(claude)-code/|"
                           r"/@openai/(codex)/|/@(opencode)/|/(opencode)-ai/|/@google/(gemini)-cli")
INTERPRETERS = re.compile(r"^(node|node-MainThread|nodejs|bun|deno|python\d?(\.\d+)?|ruby|perl|java|php|sh|bash|"
                          r"dash|zsh|uv|npx|npm|pnpm|yarn)$")
# tool names that may be shown next to an interpreter (`python3 pytest`): an allowlist, so a script's own name,
# which could say something private, is never recorded
TOOL_ALLOW = frozenset((
    "pytest", "py.test", "jest", "vitest", "mocha", "vite", "next", "nuxt", "webpack", "rollup", "esbuild", "tsc",
    "tsserver", "eslint", "prettier", "playwright", "puppeteer", "uvicorn", "gunicorn", "celery", "flask", "django",
    "manage", "jupyter", "jupyter-lab", "ipykernel_launcher", "ipykernel", "pip", "pip3", "npm", "npm-cli", "pnpm",
    "yarn", "npx", "bun", "turbo", "nx", "storybook", "astro", "remix", "expo", "metro", "tailwindcss", "prisma",
    "ruff", "mypy", "pyright", "black", "isort", "poetry", "uv", "hatch", "tox", "nox", "streamlit", "gradio",
    "ollama", "vllm", "torchrun", "accelerate", "deepspeed", "huggingface-cli", "mcp", "t3", "claude", "codex",
    "opencode", "gemini", "antigravity", "http.server", "nodemon", "ts-node", "tsx", "wrangler",
    "firebase", "supabase", "docker-compose", "ansible", "ansible-playbook", "make", "cargo", "go", "gradle",
))


def num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def rnd(v, n=1):
    return None if v is None else round(v, n)


def clean(s, n=60):
    """Printable, single-line, bounded: names from /proc and the journal are other people's text."""
    s = "".join(c if c.isprintable() and c not in "\t\r\n" else "?" for c in str(s or ""))
    return s[:n]


def _read(path, limit=1 << 20):
    try:
        with open(path, errors="replace") as f:
            return f.read(limit)
    except OSError:
        return None


# ---------------------------------------------------------------- pure parsers

def parse_kv(text, sep=None):
    """'key value' lines (cpu.stat, memory.stat, memory.events, /proc/vmstat) -> {key: int}."""
    out = {}
    for line in (text or "").splitlines():
        parts = line.replace(":", " ").split() if sep == ":" else line.split()
        if len(parts) >= 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def parse_meminfo(text):
    """/proc/meminfo -> {key: bytes}."""
    out = {}
    for line in (text or "").splitlines():
        k, _, v = line.partition(":")
        try:
            out[k.strip()] = int(v.split()[0]) * (1024 if v.strip().endswith("kB") else 1)
        except (IndexError, ValueError):
            pass
    return out


def parse_psi(text):
    """/proc/pressure/X or cgroup X.pressure -> {'some': total_us, 'full': total_us, 'some_avg10': ...}."""
    out = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        kv = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
        try:
            out[parts[0]] = int(kv.get("total", 0))
            out[parts[0] + "_avg10"] = float(kv.get("avg10", 0))
        except ValueError:
            pass
    return out


def parse_cpu_line(text):
    """First line of /proc/stat -> (total ticks, idle, iowait, steal)."""
    for line in (text or "").splitlines():
        if line.startswith("cpu "):
            v = [int(x) for x in line.split()[1:]]
            v += [0] * (10 - len(v))
            return sum(v[:8]), v[3], v[4], v[7]
    return None


def parse_stat_counts(text):
    out = {}
    for line in (text or "").splitlines():
        if line.startswith(("procs_running", "procs_blocked", "btime")):
            k, v = line.split()[:2]
            out[k] = int(v)
    return out


def parse_proc_stat(text):
    """/proc/PID/stat -> (comm, state, ppid, ticks, starttime ticks, rss bytes) or None."""
    if not text or ")" not in text:
        return None
    comm = text[text.index("(") + 1:text.rindex(")")]
    rest = text[text.rindex(")") + 2:].split()
    try:
        return comm, rest[0], int(rest[1]), int(rest[11]) + int(rest[12]), int(rest[19]), int(rest[21]) * PAGE
    except (IndexError, ValueError):
        return None


def parse_diskstats(text):
    """/proc/diskstats -> {dev: (sectors read, sectors written, io ticks ms)} for whole disks only."""
    out = {}
    for line in (text or "").splitlines():
        p = line.split()
        if len(p) < 14:
            continue
        dev = p[2]
        if not re.fullmatch(r"sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|nvme\d+n\d+|mmcblk\d+", dev):
            continue
        try:
            out[dev] = (int(p[5]), int(p[9]), int(p[12]))
        except ValueError:
            pass
    return out


def parse_mounts(text):
    """/proc/mounts -> [(mount, device)] for real filesystems, one entry per device."""
    seen, out = set(), []
    for line in (text or "").splitlines():
        p = line.split()
        if len(p) < 3 or not p[0].startswith("/dev/") or p[0].startswith("/dev/loop"):
            continue
        if p[2] not in ("ext4", "ext3", "xfs", "btrfs", "f2fs", "zfs", "bcachefs") or p[1].startswith("/snap/"):
            continue
        mnt = p[1].replace("\\040", " ")
        if p[0] in seen or mnt.startswith(("/boot", "/var/snap")):
            continue
        seen.add(p[0])
        out.append((mnt, p[0]))
    return out


def agent_of(comm, argv):
    """Which coding agent a process is (claude, codex, ...) or None. argv only decides; it is never kept."""
    a = AGENT_COMMS.get(comm)
    if a:
        return a
    if INTERPRETERS.match(comm or ""):
        for arg in argv[1:3]:
            m = AGENT_ARGV_RE.search(arg)
            if m:
                return next((g for g in m.groups() if g), None) or "agent"
    return None


def proc_name(comm, argv):
    """What the process is called in telemetry: comm, plus an allowlisted tool for interpreters ('node vite')."""
    comm = clean(comm, 24)
    if not INTERPRETERS.match(comm) or not argv:
        return comm
    exe = os.path.basename(argv[0] or "")
    if exe in TOOL_ALLOW and exe != comm:   # a bundled tool whose threads are called node-MainThread (t3)
        return f"{comm} {exe}"
    for arg in argv[1:4]:
        if not arg or arg.startswith("-"):
            continue
        base = os.path.basename(arg)
        for ext in (".js", ".mjs", ".cjs", ".py", ".ts"):
            if base.endswith(ext):
                base = base[:-len(ext)]
        if base in TOOL_ALLOW:
            return f"{comm} {base}"
        if base in ("cli", "index", "main", "bin") and arg.count("/") >= 2:
            # node_modules/<tool>/bin/cli.js: the package folder names the tool
            for part in reversed(arg.split("/")[:-1]):
                if part in TOOL_ALLOW:
                    return f"{comm} {part}"
        return comm
    return comm


@functools.lru_cache(maxsize=512)
def _user_of_uid(uid):
    try:
        return pwd.getpwuid(int(uid)).pw_name
    except (KeyError, ValueError, TypeError):
        return ""


def _user_of_cgroup(path):
    m = re.search(r"user-(\d+)\.slice", path or "")
    return _user_of_uid(m.group(1)) if m else ""


KERNEL_KILL_RE = re.compile(r"(?:Memory cgroup out of memory|Out of memory)[^:]*: Killed process (\d+) \(([^)]*)\)"
                            r".*?anon-rss:(\d+)kB.*?UID:(\d+)")
OOM_KILL_INFO_RE = re.compile(r"oom-kill:.*?task_memcg=([^,]*),task=([^,]*),pid=(\d+),uid=(\d+)")
EARLYOOM_RE = re.compile(r"sending SIG(?:TERM|KILL) to process (\d+) uid (\d+) \"([^\"]*)\".*?VmRSS (\d+) MiB")
OOMD_RE = re.compile(r"Killed (/\S+) due to (memory pressure|memory used|swap)")
UNIT_RE = re.compile(r"^(\S+\.(?:service|scope|socket|timer|mount)): (Failed with result '([^']*)'|"
                     r"Scheduled restart job, restart counter is at (\d+)|A process of this unit has been killed by "
                     r"the OOM killer|Start request repeated too quickly)")
HUNG_RE = re.compile(r"INFO: task (.{1,20}):(\d+) blocked for more than (\d+) seconds")
DISK_ERR_RE = re.compile(r"(?:I/O error, dev (\w+)|EXT4-fs error \(device (\w+)\)|critical medium error, dev (\w+))")


def journal_event(entry, oom_info=None):
    """One journal entry (dict from `journalctl -o json`) -> event dict or None. Only names and numbers are kept:
    earlyoom's own line carries the victim's command line, which is dropped here."""
    msg = entry.get("MESSAGE")
    if isinstance(msg, list):  # non-UTF-8 messages come as byte arrays
        try:
            msg = bytes(msg).decode("utf-8", "replace")
        except (TypeError, ValueError):
            return None
    msg = str(msg or "")
    try:
        ts = int(entry["__REALTIME_TIMESTAMP"]) / 1e6
    except (KeyError, ValueError, TypeError):
        return None
    ident = str(entry.get("SYSLOG_IDENTIFIER") or "")
    transport = str(entry.get("_TRANSPORT") or "")
    ev = None
    if transport == "kernel" or ident == "kernel":
        m = OOM_KILL_INFO_RE.search(msg)
        if m and oom_info is not None:   # the line before "Killed process" names the cgroup
            oom_info[m.group(3)] = m.group(1)
            return None
        m = KERNEL_KILL_RE.search(msg)
        if m:
            memcg = (oom_info or {}).get(m.group(1), "")
            ev = {"kind": "oom_kill", "user": _user_of_uid(m.group(4)), "unit": clean(memcg.rsplit("/", 1)[-1], 80),
                  "detail": clean(m.group(2), 40), "value": round(int(m.group(3)) / 1024)}
            if "cgroup" in msg.split(":")[0]:
                ev["detail"] += " (limit)"
        elif HUNG_RE.search(msg):
            m = HUNG_RE.search(msg)
            ev = {"kind": "hung_task", "detail": clean(m.group(1), 40), "value": int(m.group(3))}
        elif DISK_ERR_RE.search(msg):
            m = DISK_ERR_RE.search(msg)
            ev = {"kind": "disk_error", "detail": next(g for g in m.groups() if g)}
    elif ident == "earlyoom":
        m = EARLYOOM_RE.search(msg)
        if m:
            ev = {"kind": "earlyoom_kill", "user": _user_of_uid(m.group(2)), "detail": clean(m.group(3), 40),
                  "value": int(m.group(4))}
    elif ident == "systemd-oomd" or "due to memory" in msg or "due to swap" in msg:
        m = OOMD_RE.search(msg)
        if m:
            ev = {"kind": "oomd_kill", "user": _user_of_cgroup(m.group(1)),
                  "unit": clean(m.group(1).rstrip("/").rsplit("/", 1)[-1], 80), "detail": m.group(2)}
    else:
        m = UNIT_RE.search(msg)
        if m:
            uid = entry.get("_UID")
            user = _user_of_uid(uid) if uid not in (None, "0", 0) else ""
            unit = clean(m.group(1), 80)
            if m.group(3) is not None:
                ev = {"kind": "unit_failed", "unit": unit, "user": user, "detail": clean(m.group(3), 30)}
            elif m.group(4) is not None:
                ev = {"kind": "unit_restart", "unit": unit, "user": user, "value": int(m.group(4))}
            elif "OOM killer" in m.group(2):
                ev = {"kind": "unit_oom", "unit": unit, "user": user}
            else:
                ev = {"kind": "unit_failed", "unit": unit, "user": user, "detail": "start-limit"}
    if ev:
        ev["ts"] = round(ts, 6)
        for k in ("user", "unit", "detail"):
            ev.setdefault(k, "")
        ev.setdefault("value", None)
    return ev


WDIOF_CARDRESET = 0x20


def boot_reason(bootstatus, prev_last, pstore_new):
    """How the previous boot ended: watchdog | panic | unclean | clean | unknown (same rules as health's
    last-boot check). prev_last: the previous boot's last journal entry, or None."""
    try:
        if int(bootstatus or 0) & WDIOF_CARDRESET:
            return "watchdog"
    except ValueError:
        pass
    if pstore_new:
        return "panic"
    if not prev_last:
        return "unknown"
    msg = str(prev_last.get("MESSAGE") or "")
    ident = str(prev_last.get("SYSLOG_IDENTIFIER") or "")
    if msg == "Journal stopped" or ident == "systemd-shutdown" or msg.startswith(
            ("Received SIGTERM from PID 1", "systemd-journald stopped")):
        return "clean"
    return "unclean"


# ---------------------------------------------------------------- collection (root, every minute)

class Collector:
    """One sample of the box. `state` is what the previous run left (counters for deltas); collect() returns
    ({table: [row dicts]}, new state). Paths are parameters so tests can point it at a fake tree."""

    def __init__(self, box, state=None, proc="/proc", sys_dir="/sys", cgroup="/sys/fs/cgroup", journal=True,
                 home_of=None):
        self.box = box
        self.old = state or {}
        self.proc, self.sys, self.cg = proc, sys_dir, cgroup
        self.journal = journal
        self.home_of = home_of or (lambda user: None)

    def _p(self, *parts):
        return os.path.join(self.proc, *parts)

    def collect(self, now=None):
        t0 = time.monotonic()
        now = int(now or time.time())
        old, new = self.old, {"ts": now}
        boot_id = (_read(self._p("sys", "kernel", "random", "boot_id")) or "").strip()
        new["boot_id"] = boot_id
        same_boot = old.get("boot_id") == boot_id and old.get("ts")
        dt = now - old["ts"] if same_boot else None
        if dt is not None and not 0 < dt <= MAX_INTERVAL_S:
            dt = None
        self.dt = dt
        out = {t: [] for t in RAW_TABLES}
        out["events"] = []
        s = {"ts": now, "box": self.box, "interval_s": dt}
        self._box(s, old, new, dt)
        out["disks"], out["diskio"] = self._disks(now, old, new, dt)
        root = next((d for d in out["disks"] if d["mount"] == "/"), None)
        if root:
            s["disk_total_gb"], s["disk_free_gb"] = root["total_gb"], root["free_gb"]
        out["slices"] = self._slices(now, old, new, dt)
        procs = self._procs(old, new, dt)
        s["procs"] = len(procs)
        out["procs"] = self._top(now, procs)
        out["agents"] = self._agents(now, procs)
        out["containers"], cev = self._containers(now, old, new, dt)
        out["events"] += cev
        if old.get("boot_id") != boot_id:
            out["events"] += self._boot_event(now, old)
        elif same_boot and now - old["ts"] > GAP_S:
            out["events"].append({"ts": float(old["ts"]), "kind": "sample_gap", "user": "", "unit": "",
                                  "detail": "", "value": now - old["ts"]})
        if self.journal:
            out["events"] += self._journal(old, new)
        s["collect_ms"] = int((time.monotonic() - t0) * 1000)
        out["samples"] = [s]
        for t in out:
            for r in out[t]:
                r["box"] = self.box
                r.setdefault("ts", now)
        return out, new

    # ---- box-wide
    def _box(self, s, old, new, dt):
        stat = _read(self._p("stat")) or ""
        cpu = parse_cpu_line(stat)
        if cpu:
            new["cpu"] = cpu
            o = old.get("cpu")
            if dt and o and cpu[0] > o[0]:
                d = cpu[0] - o[0]
                s["cpu_pct"] = rnd(100 * (1 - (cpu[1] + cpu[2] - o[1] - o[2]) / d))
                s["iowait_pct"] = rnd(100 * (cpu[2] - o[2]) / d)
                s["steal_pct"] = rnd(100 * (cpu[3] - o[3]) / d)
        cnt = parse_stat_counts(stat)
        s["procs_running"], s["procs_blocked"] = cnt.get("procs_running"), cnt.get("procs_blocked")
        la = (_read(self._p("loadavg")) or "0 0 0").split()
        s["load1"], s["load5"], s["load15"] = (num(x) for x in la[:3])
        s["ncpu"] = os.cpu_count()
        psi = {k: parse_psi(_read(self._p("pressure", f)))
               for k, f in (("cpu", "cpu"), ("mem", "memory"), ("io", "io"))}
        new["psi"] = {k: {"some": v.get("some"), "full": v.get("full")} for k, v in psi.items()}
        for k in ("cpu", "mem", "io"):
            for kind, col in (("some", f"psi_{k}"), ("full", f"psi_{k}_full")):
                if col == "psi_cpu_full":
                    continue
                cur, prev = psi[k].get(kind), ((old.get("psi") or {}).get(k) or {}).get(kind)
                if dt and cur is not None and prev is not None and cur >= prev:
                    s[col] = rnd(min(100.0, (cur - prev) / (dt * 1e6) * 100), 2)
        mi = parse_meminfo(_read(self._p("meminfo")))
        if mi.get("MemTotal"):
            total, avail = mi["MemTotal"], mi.get("MemAvailable", mi.get("MemFree", 0))
            s["mem_total_mb"], s["mem_avail_mb"] = total // MB, avail // MB
            s["mem_used_mb"] = (total - avail) // MB
            s["mem_anon_mb"] = mi.get("AnonPages", 0) // MB
            s["mem_shmem_mb"] = mi.get("Shmem", 0) // MB
            s["mem_cache_mb"] = max(0, mi.get("Cached", 0) + mi.get("Buffers", 0) + mi.get("SReclaimable", 0)
                                    - mi.get("Shmem", 0)) // MB
            s["swap_total_mb"] = mi.get("SwapTotal", 0) // MB
            s["swap_used_mb"] = (mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)) // MB
        vm = parse_kv(_read(self._p("vmstat")))
        keep = {k: vm.get(k) for k in ("pswpin", "pswpout", "pgmajfault", "oom_kill")}
        new["vm"] = keep
        ov = old.get("vm") or {}

        def dv(k):
            if not dt or keep.get(k) is None or ov.get(k) is None or keep[k] < ov[k]:
                return None
            return keep[k] - ov[k]
        sw_in, sw_out = dv("pswpin"), dv("pswpout")
        s["swap_in_mb"] = rnd(sw_in * PAGE / MB) if sw_in is not None else None
        s["swap_out_mb"] = rnd(sw_out * PAGE / MB) if sw_out is not None else None
        s["majfaults"], s["oom_kills"] = dv("pgmajfault"), dv("oom_kill")
        s["temp_max_c"] = self._temp()
        thr = 0
        for f in glob.glob(os.path.join(self.sys, "devices/system/cpu/cpu*/thermal_throttle/*_throttle_count")):
            thr += int(num(_read(f), 0))
        new["throttle"] = thr
        if dt and old.get("throttle") is not None and thr >= old["throttle"]:
            s["throttles"] = thr - old["throttle"]
        mhz = [num(ln.split(":", 1)[1]) for ln in (_read(self._p("cpuinfo")) or "").splitlines()
               if ln.startswith("cpu MHz")]
        mhz = [m for m in mhz if m]
        s["cpu_mhz"] = int(sum(mhz) / len(mhz)) if mhz else None

    def _temp(self):
        cpu, other = [], []
        for d in glob.glob(os.path.join(self.sys, "class/hwmon/hwmon*")):
            name = (_read(os.path.join(d, "name")) or "").strip()
            for f in glob.glob(os.path.join(d, "temp*_input")):
                t = num(_read(f))
                if t is None or not 0 < t / 1000 < 150:
                    continue
                (cpu if name in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz") else other).append(t / 1000)
        vals = cpu or other
        return round(max(vals), 1) if vals else None

    def _disks(self, now, old, new, dt):
        disks = []
        for mnt, dev in parse_mounts(_read(self._p("mounts"))):
            try:
                st = os.statvfs(mnt)
            except OSError:
                continue
            disks.append({"ts": now, "mount": mnt, "dev": os.path.basename(dev),
                          "total_gb": round(st.f_blocks * st.f_frsize / 1e9, 2),
                          "free_gb": round(st.f_bavail * st.f_frsize / 1e9, 2)})
        cur = parse_diskstats(_read(self._p("diskstats")))
        new["diskio"] = cur
        io = []
        for dev, (r, w, ticks) in cur.items():
            o = (old.get("diskio") or {}).get(dev)
            if not dt or not o or r < o[0] or w < o[1] or ticks < o[2]:
                continue
            io.append({"ts": now, "dev": dev, "read_mb": rnd((r - o[0]) * 512 / MB),
                       "write_mb": rnd((w - o[1]) * 512 / MB),
                       "util_pct": rnd(min(100.0, (ticks - o[2]) / (dt * 10)))})
        return disks, io

    # ---- per account / slice
    def _slice_paths(self):
        out = []
        for p in sorted(glob.glob(os.path.join(self.cg, "*.slice"))):
            if os.path.basename(p) != "user.slice":
                out.append((os.path.basename(p), "", p))
        for p in sorted(glob.glob(os.path.join(self.cg, "user.slice", "user-*.slice"))):
            out.append((os.path.basename(p), _user_of_cgroup(p), p))
        return out

    def _slices(self, now, old, new, dt):
        rows, st = [], {}
        for name, user, path in self._slice_paths():
            cpu = parse_kv(_read(os.path.join(path, "cpu.stat"))).get("usage_usec")
            ms = parse_kv(_read(os.path.join(path, "memory.stat")))
            ev = parse_kv(_read(os.path.join(path, "memory.events")))
            pr = parse_psi(_read(os.path.join(path, "memory.pressure")))
            cur = {"cpu": cpu, "oom": ev.get("oom_kill"), "high": ev.get("high"), "max": ev.get("max"),
                   "ps": pr.get("some"), "pf": pr.get("full")}
            st[name] = cur
            o = (old.get("slices") or {}).get(name) or {}

            def d(k):
                if not dt or cur.get(k) is None or o.get(k) is None or cur[k] < o[k]:
                    return None
                return cur[k] - o[k]
            # in use vs reclaimable cache: the same split the dashboard shows (lib/sbrunning.py split_stat); kernel
            # slab the kernel frees by itself (dentry/inode caches) counts as cache, not use
            split = sbrunning.split_stat({k: int(v) for k, v in ms.items() if isinstance(v, (int, float))}) if ms else None
            real = split[0] if split else None
            dcpu = d("cpu")
            rows.append({
                "ts": now, "slice": name, "user": user,
                "cpu_pct": rnd(dcpu / (dt * 1e4)) if dcpu is not None else None,
                "mem_mb": real // MB if real is not None else None,
                "cache_mb": split[1] // MB if split else None,
                "swap_mb": int(num(_read(os.path.join(path, "memory.swap.current")), 0)) // MB,
                "pids": int(num(_read(os.path.join(path, "pids.current")), 0)),
                "psi_mem": rnd(d("ps") / (dt * 1e4), 2) if d("ps") is not None else None,
                "psi_mem_full": rnd(d("pf") / (dt * 1e4), 2) if d("pf") is not None else None,
                "oom_kills": d("oom"), "mem_high_events": d("high"), "mem_max_events": d("max")})
        new["slices"] = st
        return rows

    # ---- processes
    def _procs(self, old, new, dt):
        procs, prev, cur = {}, old.get("procs") or {}, {}
        try:
            pids = [p for p in os.listdir(self.proc) if p.isdigit()]
        except OSError:
            pids = []
        btime = parse_stat_counts(_read(self._p("stat"))).get("btime", 0)
        now = time.time()
        for pid in pids:
            st = parse_proc_stat(_read(self._p(pid, "stat"), 4096))
            if not st:
                continue
            comm, state, ppid, ticks, start, rss = st
            if ppid == 2 or pid == "2":   # kernel threads
                continue
            try:
                uid = os.stat(self._p(pid)).st_uid
            except OSError:
                continue
            cur[pid] = [start, ticks]
            o = prev.get(pid)
            cpu = None
            if dt and o and o[0] == start and ticks >= o[1]:
                cpu = (ticks - o[1]) / HZ / dt * 100
            procs[int(pid)] = {"pid": int(pid), "uid": uid, "comm": comm, "ppid": ppid, "cpu": cpu, "rss": rss,
                               "start": int(btime + start / HZ), "age": int(now - btime - start / HZ)}
        new["procs"] = cur
        return procs

    def _argv(self, pid):
        raw = _read(self._p(str(pid), "cmdline"), 4096) or ""
        return raw.split("\0")

    def _top(self, now, procs):
        by_cpu = sorted((p for p in procs.values() if (p["cpu"] or 0) >= 1), key=lambda p: -(p["cpu"] or 0))[:TOP_N]
        by_mem = sorted(procs.values(), key=lambda p: -p["rss"])[:TOP_N]
        rows, seen = [], set()
        for p in by_cpu + by_mem:
            if p["pid"] in seen:
                continue
            seen.add(p["pid"])
            rows.append({"ts": now, "user": _user_of_uid(p["uid"]), "pid": p["pid"],
                         "name": proc_name(p["comm"], self._argv(p["pid"])), "cpu_pct": rnd(p["cpu"]),
                         "rss_mb": p["rss"] // MB, "age_s": p["age"]})
        return rows

    def _agents(self, now, procs):
        agents = {}
        for pid, p in procs.items():
            a = AGENT_COMMS.get(p["comm"])
            if not a and re.match(r"^(node|node-MainThread|nodejs|bun|deno)$", p["comm"]):
                a = agent_of(p["comm"], self._argv(pid))
            if a:
                agents[pid] = a
        kids = {}
        for pid, p in procs.items():
            kids.setdefault(p["ppid"], []).append(pid)
        rows = []
        for pid, a in agents.items():
            p = procs[pid]
            if agents.get(p["ppid"]) == a:   # a helper process of the same agent session
                continue
            cpu = rss = n = 0
            todo, seen = [pid], set()
            while todo:
                q = todo.pop()
                if q in seen or q not in procs:
                    continue
                seen.add(q)
                cpu += procs[q]["cpu"] or 0
                rss += procs[q]["rss"]
                n += 1
                todo += kids.get(q, [])
            rows.append({"ts": now, "user": _user_of_uid(p["uid"]), "agent": a, "pid": pid, "started": p["start"],
                         "cpu_pct": rnd(p["cpu"]), "rss_mb": p["rss"] // MB,
                         "tree_cpu_pct": rnd(cpu) if self.dt else None, "tree_rss_mb": rss // MB, "tree_procs": n})
        return rows

    # ---- docker containers (any account's rootless Docker, or the system one)
    def _containers(self, now, old, new, dt):
        found = {}
        for dirpath, dirnames, _ in os.walk(self.cg):
            keep = []
            for d in dirnames:
                m = re.fullmatch(r"docker-([0-9a-f]{64})\.scope", d)
                if m:
                    found[m.group(1)] = os.path.join(dirpath, d)
                elif d != "init.scope":
                    keep.append(d)
            dirnames[:] = keep
        prev = old.get("containers") or {}
        st, rows, events = {}, [], []
        for cid, path in found.items():
            user = _user_of_cgroup(path)
            o = prev.get(cid) or {}
            name = o.get("name")
            if name is None:
                name = self._container_name(cid, user)
                if "containers" in old:
                    events.append({"ts": float(now), "kind": "container_start", "user": user, "unit": cid[:12],
                                   "detail": name, "value": None})
            mem = int(num(_read(os.path.join(path, "memory.current")), 0))
            peak = int(num(_read(os.path.join(path, "memory.peak")), 0)) or mem
            cpu = parse_kv(_read(os.path.join(path, "cpu.stat"))).get("usage_usec")
            peak = max(peak, o.get("peak") or 0)
            st[cid] = {"name": name, "user": user, "peak": peak, "cpu": cpu}
            dcpu = cpu - o["cpu"] if dt and cpu is not None and o.get("cpu") is not None and cpu >= o["cpu"] else None
            rows.append({"ts": now, "user": user, "id": cid[:12], "name": name,
                         "cpu_pct": rnd(dcpu / (dt * 1e4)) if dcpu is not None else None,
                         "mem_mb": mem // MB, "peak_mb": peak // MB})
        for cid, o in prev.items():
            if cid not in found:
                events.append({"ts": float(now), "kind": "container_stop", "user": o.get("user") or "",
                               "unit": cid[:12], "detail": o.get("name") or "", "value": (o.get("peak") or 0) // MB})
        new["containers"] = st
        return rows, events

    def _container_name(self, cid, user):
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import sbrunning
            if user:
                pw = pwd.getpwnam(user)
                meta = sbrunning._docker_meta(pw.pw_dir, cid, pw.pw_uid)
            else:
                raw = _read(f"/var/lib/docker/containers/{cid}/config.v2.json", 256 << 10) or "{}"
                meta = {"name": str((json.loads(raw) or {}).get("Name") or "").lstrip("/")}
        except Exception:  # noqa: BLE001 - a name is a nicety
            meta = {}
        name = meta.get("name") or ""
        return clean(name, 80) if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name) else cid[:12]

    # ---- events from the journal (one call, starting where the last one stopped)
    def _journal(self, old, new):
        j = dict(old.get("journal") or {})
        cmd = ["journalctl", "-o", "json", "--no-pager", "-q",
               "--output-fields=MESSAGE,SYSLOG_IDENTIFIER,_TRANSPORT,_UID,_COMM"]
        if j.get("cursor"):
            cmd.append(f"--after-cursor={j['cursor']}")
        elif j.get("since"):
            cmd.append(f"--since=@{int(j['since']) - 2}")
        else:
            cmd.append(f"--since={JOURNAL_FIRST_LOOKBACK}")
        if not j.get("no_grep"):
            cmd += ["-g", JOURNAL_PATTERN]
        cmd += ["_TRANSPORT=kernel", "+", "SYSLOG_IDENTIFIER=earlyoom", "+", "SYSLOG_IDENTIFIER=systemd-oomd", "+",
                "_COMM=systemd"]
        started = time.time()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            new["journal"] = j
            print(f"telemetry journal: {e!r}", file=sys.stderr)
            return []
        if r.returncode not in (0, 1) and "pattern" in (r.stderr or "").lower() and not j.get("no_grep"):
            j["no_grep"] = True   # journalctl built without PCRE: filter here instead
            self.old = dict(old, journal=j)
            return self._journal(self.old, new)
        events, info, last = [], {}, None
        for line in r.stdout.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            last = e.get("__CURSOR") or last
            ev = journal_event(e, info)
            if ev:
                events.append(ev)
        if last:
            j["cursor"] = last
        elif r.returncode in (0, 1) and not r.stdout.strip():
            # nothing matched: next time start from now, not from the old cursor (no re-scan of the quiet stretch)
            j.pop("cursor", None)
            j["since"] = started
        new["journal"] = j
        return events

    def _boot_event(self, now, old):
        """The box (re)started since the last sample: when, and how the previous boot ended."""
        up = num((_read(self._p("uptime")) or "0").split()[0], 0)
        boot_ts = now - up
        prev = None
        try:
            if not self.journal:
                raise OSError("journal reads are off")
            r = subprocess.run(["journalctl", "-b", "-1", "-n", "1", "-o", "json", "--no-pager", "-q",
                                "--output-fields=MESSAGE,SYSLOG_IDENTIFIER"],
                               capture_output=True, text=True, timeout=60)
            prev = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else None
        except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
            prev = None
        prev_end = int(prev["__REALTIME_TIMESTAMP"]) / 1e6 if prev and prev.get("__REALTIME_TIMESTAMP") else None
        prev_end = prev_end or old.get("ts")
        pstore = []
        for d in ("/var/lib/systemd/pstore", "/sys/fs/pstore"):
            for fn in glob.glob(os.path.join(d, "**", "*"), recursive=True):
                try:
                    if os.path.isfile(fn) and os.path.getmtime(fn) >= (prev_end or boot_ts) - 600:
                        pstore.append(fn)
                except OSError:
                    pass
        bs = (_read(os.path.join(self.sys, "class/watchdog/watchdog0/bootstatus")) or "").strip()
        reason = boot_reason(bs, prev, pstore)
        down = boot_ts - prev_end if prev_end else None
        # a clock that was wrong before NTP fixed it (a dead RTC battery) makes nonsense gaps: leave those out
        return [{"ts": round(boot_ts, 1), "kind": "boot", "user": "", "unit": "", "detail": reason,
                 "value": round(down) if down is not None and 0 < down < 30 * 86400 else None}]


def for_user(data, user):
    """The rows one account may read: its own plus the box-wide ones (box metrics, disks, system slices, events
    that aren't about another account)."""
    out = {}
    for t, rows in data.items():
        if t in ("samples", "disks", "diskio"):
            out[t] = rows
        elif t in ("slices", "events"):
            out[t] = [r for r in rows if r.get("user", "") in ("", user)]
        else:
            out[t] = [r for r in rows if r.get("user") == user]
    return out


# ---------------------------------------------------------------- storage

def connect(path, gid=None, mode=0o640):
    """Open (creating) a telemetry database for writing. Root's files are root:gid with `mode`; never follows a
    link (the collector writes as root)."""
    if os.path.islink(path):
        raise OSError(f"{path} is a symlink; refusing")
    new = not os.path.exists(path)
    con = sqlite3.connect(path, timeout=30)
    if new:
        con.execute("PRAGMA auto_vacuum=INCREMENTAL")   # space from retention can be given back cheaply
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    con.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (str(SCHEMA_VERSION),))
    if os.geteuid() == 0:
        os.chown(path, 0, gid if gid is not None else 0)
        os.chmod(path, mode if gid is not None else 0o600)
    elif new:
        os.chmod(path, 0o600)
    return con


def close(con):
    """Fold the WAL into the main file and close, so read-only readers (who can't use the WAL) see everything."""
    try:
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()


_COLS = {}


def columns(con, table):
    key = (id(con), table)
    if key not in _COLS:
        _COLS[key] = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    return _COLS[key]


def store(con, data):
    """Write {table: [row dicts]}; rows replace rows with the same key (events: kept once)."""
    n = 0
    with con:
        for table, rows in data.items():
            if not rows or table not in KEYS:
                continue
            cols = columns(con, table)
            verb = "INSERT OR IGNORE" if table == "events" else "INSERT OR REPLACE"
            if table == "events":   # NOT NULL '' columns: a missing one would make OR IGNORE drop the row silently
                rows = [dict(r, user=r.get("user") or "", unit=r.get("unit") or "", detail=r.get("detail") or "")
                        for r in rows]
            con.executemany(f"{verb} INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                            [tuple(r.get(c) for c in cols) for r in rows])
            n += len(rows)
    return n


def meta_get(con, k, default=None):
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def meta_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))


_PAIRS = ("load1", "cpu_pct", "iowait_pct", "psi_cpu", "psi_mem", "psi_mem_full", "psi_io", "psi_io_full",
          "mem_used_mb", "mem_cache_mb", "swap_used_mb", "temp_max_c", "procs", "procs_blocked")
_SUMS = ("swap_in_mb", "swap_out_mb", "majfaults", "oom_kills", "throttles")
_INTERVAL = "coalesce((SELECT s.interval_s FROM samples s WHERE s.ts=x.ts AND s.box=x.box), 60)"

ROLLUPS = {
    "samples_hourly": (
        ["ts", "box", "n"] + [c for p in _PAIRS for c in (p, p + "_max")] + list(_SUMS)
        + ["disk_free_gb", "disk_total_gb", "mem_total_mb", "ncpu"],
        "SELECT (ts/3600)*3600 AS h, box, count(*), "
        + ", ".join(f"avg({p}), max({p})" for p in _PAIRS) + ", "
        + ", ".join(f"sum({c})" for c in _SUMS)
        + ", min(disk_free_gb), max(disk_total_gb), max(mem_total_mb), max(ncpu) "
        "FROM samples WHERE ts>=? AND ts<? GROUP BY h, box"),
    "disks_hourly": (
        ["ts", "box", "mount", "dev", "total_gb", "free_gb"],
        "SELECT (ts/3600)*3600 AS h, box, mount, max(dev), max(total_gb), min(free_gb) FROM disks "
        "WHERE ts>=? AND ts<? GROUP BY h, box, mount"),
    "diskio_hourly": (
        ["ts", "box", "dev", "read_mb", "write_mb", "util_pct", "util_pct_max"],
        "SELECT (ts/3600)*3600 AS h, box, dev, sum(read_mb), sum(write_mb), avg(util_pct), max(util_pct) "
        "FROM diskio WHERE ts>=? AND ts<? GROUP BY h, box, dev"),
    "slices_hourly": (
        ["ts", "box", "slice", "user", "n", "cpu_pct", "cpu_pct_max", "mem_mb", "mem_mb_max", "cache_mb",
         "swap_mb_max", "pids_max", "psi_mem", "psi_mem_max", "psi_mem_full_max", "oom_kills", "mem_high_events",
         "mem_max_events"],
        "SELECT (ts/3600)*3600 AS h, box, slice, max(user), count(*), avg(cpu_pct), max(cpu_pct), avg(mem_mb), "
        "max(mem_mb), avg(cache_mb), max(swap_mb), max(pids), avg(psi_mem), max(psi_mem), max(psi_mem_full), "
        "sum(oom_kills), sum(mem_high_events), sum(mem_max_events) FROM slices WHERE ts>=? AND ts<? "
        "GROUP BY h, box, slice"),
    "procs_hourly": (
        ["ts", "box", "user", "name", "n", "cpu_s", "cpu_pct_max", "rss_mb_max"],
        f"SELECT (ts/3600)*3600 AS h, box, user, name, count(*), sum(coalesce(cpu_pct, 0) * {_INTERVAL} / 100.0), "
        "max(cpu_pct), max(rss_mb) FROM procs x WHERE ts>=? AND ts<? GROUP BY h, box, user, name"),
    "agents_hourly": (
        ["ts", "box", "user", "agent", "sessions", "sessions_max", "cpu_s", "tree_cpu_s", "rss_mb_max",
         "tree_rss_mb_max"],
        "SELECT h, box, user, agent, sum(n) * 1.0 / max(1, (SELECT count(*) FROM samples s WHERE s.box=y.box "
        "AND s.ts>=y.h AND s.ts<y.h+3600)), max(n), sum(cpu_s), sum(tree_cpu_s), max(rss), max(trss) FROM ("
        f"SELECT (ts/3600)*3600 AS h, ts, box, user, agent, count(*) AS n, "
        f"sum(coalesce(cpu_pct, 0) * {_INTERVAL} / 100.0) AS cpu_s, "
        f"sum(coalesce(tree_cpu_pct, 0) * {_INTERVAL} / 100.0) AS tree_cpu_s, max(rss_mb) AS rss, "
        "max(tree_rss_mb) AS trss FROM agents x WHERE ts>=? AND ts<? GROUP BY ts, box, user, agent) y "
        "GROUP BY h, box, user, agent"),
    "containers_hourly": (
        ["ts", "box", "user", "name", "n", "cpu_s", "mem_mb_max", "peak_mb_max"],
        f"SELECT (ts/3600)*3600 AS h, box, user, coalesce(name, id), count(*), "
        f"sum(coalesce(cpu_pct, 0) * {_INTERVAL} / 100.0), max(mem_mb), max(peak_mb) FROM containers x "
        "WHERE ts>=? AND ts<? GROUP BY h, box, user, coalesce(name, id)"),
}


def rollup(con, now=None):
    """Hourly roll-ups for every finished hour since the last run. Returns the number of hours rolled."""
    now = int(now or time.time())
    end = now // 3600 * 3600
    start = meta_get(con, "rolled_until")
    if start is None:
        first = con.execute("SELECT min(ts) FROM samples").fetchone()[0]
        if first is None:
            return 0
        start = int(first) // 3600 * 3600
    start = int(start)
    if end <= start:
        return 0
    with con:
        for table, (cols, select) in ROLLUPS.items():
            con.execute(f"INSERT OR REPLACE INTO {table}({','.join(cols)}) {select}", (start, end))
        meta_set(con, "rolled_until", end)
    return (end - start) // 3600


def db_bytes(con):
    pages = con.execute("PRAGMA page_count").fetchone()[0] - con.execute("PRAGMA freelist_count").fetchone()[0]
    return pages * con.execute("PRAGMA page_size").fetchone()[0]


def retention(con, raw_days=14, keep_days=400, max_mb=1024, now=None):
    """Drop raw rows after raw_days, roll-ups/events after keep_days, then the oldest data until the file fits
    max_mb (raw days first, down to one; then roll-ups a month at a time)."""
    now = int(now or time.time())
    with con:
        for t in RAW_TABLES:
            con.execute(f"DELETE FROM {t} WHERE ts < ?", (now - raw_days * 86400,))
        for t in HOURLY_TABLES + ("events", "plan_usage"):
            con.execute(f"DELETE FROM {t} WHERE ts < ?", (now - keep_days * 86400,))
    cap = max_mb * MB
    for _ in range(500):
        if db_bytes(con) <= cap:
            break
        oldest = con.execute("SELECT min(ts) FROM samples").fetchone()[0]
        with con:
            if oldest is not None and oldest < now - 86400:
                for t in RAW_TABLES:
                    con.execute(f"DELETE FROM {t} WHERE ts < ?", (oldest + 86400,))
                continue
            oldest = con.execute("SELECT min(ts) FROM samples_hourly").fetchone()[0]
            if oldest is None or oldest >= now - 30 * 86400:
                break
            for t in HOURLY_TABLES + ("events", "plan_usage"):
                con.execute(f"DELETE FROM {t} WHERE ts < ?", (oldest + 30 * 86400,))
    con.execute("PRAGMA incremental_vacuum")
    con.commit()


# ---------------------------------------------------------------- the root job (sharedbox-health sample)

def telemetry_dir(state_dir):
    return os.path.join(state_dir, "telemetry")


def cfg_num(cfg, key, default):
    try:
        return int(str(cfg.get(key, default)).strip())
    except (TypeError, ValueError):
        return default


def private_gid(user):
    """The gid only `user` is in (their own primary group), or None."""
    import grp
    try:
        pw = pwd.getpwnam(user)
        gr = grp.getgrgid(pw.pw_gid)
    except KeyError:
        return None
    if gr.gr_name != user or gr.gr_mem:
        return None
    if any(p.pw_gid == pw.pw_gid and p.pw_name != user for p in pwd.getpwall()):
        return None
    return pw.pw_gid


def run_root(cfg, sb, now=None, verbose=False):
    """The minute job: one sample into telemetry.db and into each account's own file; once an hour roll-ups and
    retention. Never raises for one account's trouble."""
    now = int(now or time.time())
    if str(cfg.get("TELEMETRY", "1")).strip().lower() in ("0", "no", "off", "false"):
        return None
    tdir = telemetry_dir(sb.STATE_DIR)
    udir = os.path.join(tdir, "users")
    for d in (tdir, udir):
        os.makedirs(d, mode=0o755, exist_ok=True)
        if os.path.islink(d):
            raise OSError(f"{d} is a symlink")
        os.chmod(d, 0o755)
    state_path = os.path.join(tdir, "state.json")
    state = sb.read_json(state_path, {}) or {}
    box = state.get("box")
    if not box or now - state.get("box_ts", 0) > 6 * 3600:
        try:
            import sbfed
            box = sbfed.self_name(sb.boxes(cfg), sb.tailscale_ip4(), socket.gethostname())
        except Exception:  # noqa: BLE001
            box = socket.gethostname().split(".")[0].lower()
        state["box_ts"] = now
    data, new = Collector(box, state).collect(now)
    new["box"], new["box_ts"] = box, state.get("box_ts", now)
    owner = sb.owner_account(cfg)
    owner_gid = private_gid(owner.pw_name) if owner else None
    hourly = now // 3600 != state.get("ts", 0) // 3600
    raw_days = cfg_num(cfg, "TELEMETRY_RAW_DAYS", 14)
    keep_days = cfg_num(cfg, "TELEMETRY_KEEP_DAYS", 400)
    con = connect(os.path.join(tdir, "telemetry.db"), owner_gid)
    try:
        if meta_get(con, "backfilled") is None:
            try:
                backfill(con, box, os.path.join(sb.STATE_DIR, "health"))
            except Exception as e:  # noqa: BLE001 - history is a bonus
                print(f"telemetry backfill: {e!r}", file=sys.stderr)
            meta_set(con, "backfilled", now)
        store(con, data)
        if hourly:
            rollup(con, now)
            retention(con, raw_days, keep_days, cfg_num(cfg, "TELEMETRY_MAX_MB", 1024), now)
    finally:
        close(con)
    names = set(sb.friend_users()) | set(sb.admin_users(cfg))
    if owner_gid is not None:   # the owner reads telemetry.db itself (without a private group: an own file instead)
        names.discard(owner.pw_name)
    if hourly:   # accounts that are gone: their own copy goes (their rows stay in telemetry.db)
        for f in glob.glob(os.path.join(udir, "*.db")):
            if os.path.basename(f)[:-3] not in names:
                for ext in ("", "-wal", "-shm"):
                    try:
                        os.unlink(f + ext)
                    except FileNotFoundError:
                        pass
    for name in sorted(names):
        try:
            gid = private_gid(name)
            if gid is None:
                continue
            ucon = connect(os.path.join(udir, f"{name}.db"), gid)
            try:
                store(ucon, for_user(data, name))
                if hourly:
                    rollup(ucon, now)
                    retention(ucon, raw_days, keep_days, cfg_num(cfg, "TELEMETRY_USER_MAX_MB", 128), now)
            finally:
                close(ucon)
        except Exception as e:  # noqa: BLE001 - one account's file never stops the rest
            print(f"telemetry {name}: {e!r}", file=sys.stderr)
    sb.write_json(state_path, new, mode=0o600)
    if verbose:
        print(json.dumps({t: len(r) for t, r in data.items()}))
    return data


def backfill(con, box, health_dir):
    """Once, on the first run: the older health samples (metrics.jsonl, ~9 days at one a minute) and hourly lines
    (metrics-hourly.jsonl, since health sampling started) become rows, so the history isn't lost. Those files hold
    less: memory as a % (turned into MB with today's RAM size), per-account memory including file cache."""
    mi = parse_meminfo(_read("/proc/meminfo"))
    total_mb = mi.get("MemTotal", 0) // MB
    try:
        st = os.statvfs("/")
        disk_gb = st.f_blocks * st.f_frsize / 1e9
    except OSError:
        disk_gb = None
    users = {}

    def slice_of(name):
        if name not in users:
            try:
                users[name] = f"user-{pwd.getpwnam(name).pw_uid}.slice"
            except KeyError:
                users[name] = f"user-{name}.slice"
        return users[name]

    def pair(v, i):
        return v[i] if isinstance(v, list) and len(v) > i else None

    rows = {"samples": [], "slices": []}
    prev = None
    for line in open_lines(os.path.join(health_dir, "metrics.jsonl")):
        ts = int(line.get("ts") or 0)
        if not ts:
            continue
        psi = line.get("psi") or [None] * 3
        dfree = line.get("disk_free_b")
        rows["samples"].append({
            "ts": ts, "box": box, "cpu_pct": line.get("cpu"), "load1": line.get("load1"),
            "mem_total_mb": total_mb or None,
            "mem_used_mb": int(line["mem"] * total_mb / 100) if line.get("mem") is not None and total_mb else None,
            "swap_used_mb": int(line.get("swap", 0) / MB), "disk_free_gb": round(dfree / 1e9, 2) if dfree else None,
            "disk_total_gb": round(disk_gb, 2) if disk_gb else None,
            "psi_cpu": psi[0], "psi_mem": psi[1], "psi_io": psi[2], "temp_max_c": line.get("temp"),
            "interval_s": ts - prev["ts"] if prev and 0 < ts - prev["ts"] <= MAX_INTERVAL_S else None})
        dt = rows["samples"][-1]["interval_s"]
        for name, v in (line.get("users") or {}).items():
            cpu = None
            pv = ((prev or {}).get("users") or {}).get(name)
            if dt and pv and isinstance(v, list) and v[0] >= pv[0]:
                cpu = round((v[0] - pv[0]) / (dt * 1e4), 1)
            rows["slices"].append({"ts": ts, "box": box, "slice": slice_of(name), "user": name, "cpu_pct": cpu,
                                   "mem_mb": int(pair(v, 1) / MB) if pair(v, 1) is not None else None})
        prev = line
    store(con, rows)
    hourly = {"samples_hourly": [], "slices_hourly": []}
    for line in open_lines(os.path.join(health_dir, "metrics-hourly.jsonl")):
        ts = int(line.get("ts") or 0)
        if not ts:
            continue
        psi = line.get("psi") or [None] * 3
        mem = line.get("mem")
        r = {"ts": ts, "box": box, "n": line.get("n"), "cpu_pct": pair(line.get("cpu"), 0),
             "cpu_pct_max": pair(line.get("cpu"), 1), "load1": pair(line.get("load1"), 0),
             "load1_max": pair(line.get("load1"), 1), "temp_max_c": pair(line.get("temp"), 0),
             "temp_max_c_max": pair(line.get("temp"), 1), "mem_total_mb": total_mb or None,
             "disk_total_gb": round(disk_gb, 2) if disk_gb else None,
             "disk_free_gb": round(line["disk_free"] * disk_gb / 100, 2)
             if line.get("disk_free") is not None and disk_gb else None}
        for i, k in enumerate(("psi_cpu", "psi_mem", "psi_io")):
            r[k], r[k + "_max"] = pair(psi[i] if i < len(psi) else None, 0), pair(psi[i] if i < len(psi) else None, 1)
        if mem and total_mb:
            r["mem_used_mb"] = pair(mem, 0) * total_mb / 100 if pair(mem, 0) is not None else None
            r["mem_used_mb_max"] = pair(mem, 1) * total_mb / 100 if pair(mem, 1) is not None else None
        hourly["samples_hourly"].append(r)
        for name, u in (line.get("users") or {}).items():
            if not isinstance(u, dict):
                continue
            hourly["slices_hourly"].append({
                "ts": ts, "box": box, "slice": slice_of(name), "user": name, "n": line.get("n"),
                "cpu_pct": round(u.get("cpu_s", 0) / 36, 1), "mem_mb": pair(u.get("mem_mb"), 0),
                "mem_mb_max": pair(u.get("mem_mb"), 1)})
    # INSERT OR IGNORE: hours that also have minute rows get rolled up from those instead
    with con:
        for table, rows_ in hourly.items():
            cols = columns(con, table)
            con.executemany(f"INSERT OR IGNORE INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                            [tuple(r.get(c) for c in cols) for r in rows_])
    meta_set(con, "backfill_note", "rows before telemetry started come from health/metrics*.jsonl: mem_used_mb "
                                   "estimated from a %, slices.mem_mb includes file cache")
    return len(rows["samples"]), len(hourly["samples_hourly"])


def open_lines(path):
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    yield r
    except OSError:
        return


# ---------------------------------------------------------------- reading

def readable_db(state_dir, user):
    """(path, scope) of the telemetry file this account can read here: everything for the owner, its own file
    for anyone else; (None, why) when there is none yet."""
    tdir = telemetry_dir(state_dir)
    full = os.path.join(tdir, "telemetry.db")
    if os.access(full, os.R_OK):
        return full, "all"
    own = os.path.join(tdir, "users", f"{user}.db")
    if os.access(own, os.R_OK):
        return own, "yours"
    if not os.path.exists(full):
        return None, "no telemetry on this box yet (it starts with the next treehouse update)"
    return None, "no telemetry file for your account yet (it appears within a minute of the next sample)"


def open_ro(path):
    """Read-only connection. Root's files are opened immutable: the folder isn't the reader's, so SQLite can't
    make the -shm file a WAL reader needs; the collector folds its WAL in before closing each minute."""
    from urllib.parse import quote
    mine = os.stat(path).st_uid == os.geteuid()
    uri = f"file:{quote(os.path.abspath(path))}?mode=ro" + ("" if mine else "&immutable=1")
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.execute("PRAGMA query_only=1")
    deadline = time.monotonic() + 120

    def guard():   # a runaway query (a recursive CTE) stops after two minutes
        return 1 if time.monotonic() > deadline else 0
    con.set_progress_handler(guard, 100000)
    return con


def _fingerprint(path):
    try:
        st = os.stat(path)
        return st.st_size, st.st_mtime_ns, os.path.exists(path + "-wal")
    except OSError:
        return None


def read(path, fn, tries=5):
    """fn(con) on a read-only connection; again if the file changed under it (the collector's checkpoint)."""
    err = None
    for i in range(tries):
        before = _fingerprint(path)
        try:
            con = open_ro(path)
            try:
                out = fn(con)
            finally:
                con.close()
        except sqlite3.DatabaseError as e:
            # only a file caught mid-checkpoint is worth another try; a bad query or a write attempt is not
            if not re.search(r"malformed|disk image|not a database|locked|disk I/O", str(e)):
                raise
            err = e
            time.sleep(0.3 * (i + 1))
            continue
        if _fingerprint(path) == before:
            return out
        time.sleep(0.3 * (i + 1))
    if err:
        raise err
    return out


def sql(path, query, params=()):
    def run(con):
        cur = con.execute(query, params)
        return [d[0] for d in cur.description or []], cur.fetchmany(100000)
    return read(path, run)


def _rows(con, q, params=()):
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(q, params)]


def _when(ts):
    return time.strftime("%a %d %b %H:%M", time.localtime(ts))


def _gb(mb):
    return "?" if mb is None else f"{mb / 1024:.1f} GB"


def boxes_in(con):
    return [r[0] for r in con.execute("SELECT DISTINCT box FROM samples_hourly UNION SELECT DISTINCT box FROM samples")]


def badness(s, ncpu=None):
    """How bad one sample was (0 = fine). Memory stalls weigh most: they are what freezes a box."""
    total = s.get("mem_total_mb") or 0
    used = 100 * (s.get("mem_used_mb") or 0) / total if total else 0
    ncpu = s.get("ncpu") or ncpu or 1
    score = ((s.get("psi_mem") or 0) + 2 * (s.get("psi_mem_full") or 0) + 1.5 * (s.get("psi_io_full") or 0)
             + 0.5 * (s.get("psi_cpu") or 0) + max(0, used - 85) * 4 + min(40, max(0, (s.get("load1") or 0) / ncpu - 2) * 10))
    if (s.get("swap_in_mb") or 0) > 50:
        score += 20
    return round(score, 1)


def why_at(con, box, ts, user=None):
    """Who and what was using the box at one moment: biggest accounts by memory, busiest processes, events nearby."""
    slices = _rows(con, "SELECT user, slice, mem_mb, cpu_pct FROM slices WHERE box=? AND ts=? "
                        "ORDER BY coalesce(mem_mb, 0) DESC LIMIT 3", (box, ts))
    procs = _rows(con, "SELECT user, name, cpu_pct, rss_mb FROM procs WHERE box=? AND ts=? "
                       "ORDER BY coalesce(cpu_pct, 0) + rss_mb / 100.0 DESC LIMIT 3", (box, ts))
    events = _rows(con, "SELECT ts, kind, user, unit, detail, value FROM events WHERE box=? AND ts BETWEEN ? AND ? "
                        "AND kind NOT IN ('container_start') ORDER BY ts LIMIT 8", (box, ts - 600, ts + 600))
    return {"slices": slices, "procs": procs, "events": events}


def _dur(sec):
    sec = int(sec)
    return f"{sec} s" if sec < 120 else f"{sec // 60} min" if sec < 7200 else f"{sec / 3600:.1f} h"


def event_text(e):
    k, who = e["kind"], (f"{e['user']}'s " if e.get("user") else "")
    if k in ("oom_kill", "earlyoom_kill", "oomd_kill"):
        by = {"oom_kill": "the kernel", "earlyoom_kill": "earlyoom", "oomd_kill": "systemd-oomd"}[k]
        what = e.get("detail") if k != "oomd_kill" else e.get("unit")
        size = f" ({_gb(e['value'])})" if e.get("value") and k != "oomd_kill" else ""
        return f"{by} killed {who}{what}{size} for memory"
    if k == "unit_failed":
        return f"{who}{e['unit']} failed ({e['detail']})"
    if k == "unit_restart":
        return f"{who}{e['unit']} restarted (#{int(e['value'] or 0)})"
    if k == "unit_oom":
        return f"{who}{e['unit']} lost a process to the OOM killer"
    if k == "boot":
        down = e.get("value") or 0
        return f"box started ({e['detail']} end of the previous boot" + (
            f", down {_dur(down)})" if down else ")")
    if k == "sample_gap":
        return f"no samples for {_dur(e['value'] or 0)} (box stalled or sampling stopped)"
    if k == "container_stop":
        return f"{who}container {e['detail']} stopped (peak {_gb(e['value'])})"
    if k == "container_start":
        return f"{who}container {e['detail']} started"
    if k == "hung_task":
        return f"kernel: {e['detail']} blocked for {int(e['value'] or 0)} s"
    if k == "disk_error":
        return f"disk error on {e['detail']}"
    return f"{k} {e.get('unit') or ''} {e.get('detail') or ''}".strip()


def summary(con, hours=24, box=None, now=None):
    """The last `hours`: headline numbers, the worst moments and why, events, top accounts and agents."""
    now = int(now or time.time())
    since = now - hours * 3600
    out = {"since": since, "now": now, "boxes": []}
    for b in ([box] if box else sorted(boxes_in(con))):
        S = _rows(con, "SELECT * FROM samples WHERE box=? AND ts>=? ORDER BY ts", (b, since))
        if not S:
            continue
        d = {"box": b, "samples": len(S), "first": S[0]["ts"], "last": S[-1]["ts"]}

        def mx(k):
            v = [s[k] for s in S if s.get(k) is not None]
            return max(v) if v else None
        last = S[-1]
        d["ncpu"], d["mem_total_mb"] = mx("ncpu") or os.cpu_count(), mx("mem_total_mb")
        d["peak"] = {k: mx(k) for k in ("load1", "cpu_pct", "mem_used_mb", "mem_cache_mb", "swap_used_mb",
                                        "psi_cpu", "psi_mem", "psi_mem_full", "psi_io", "psi_io_full", "temp_max_c")}
        d["disk"] = {"free_gb": last.get("disk_free_gb"), "total_gb": last.get("disk_total_gb"),
                     "change_gb": (round(last["disk_free_gb"] - S[0]["disk_free_gb"], 1)
                                   if last.get("disk_free_gb") is not None and S[0].get("disk_free_gb") is not None
                                   else None)}
        scored = sorted(((badness(s, d["ncpu"]), s) for s in S), key=lambda x: -x[0])
        worst = []
        for score, s in scored:
            if len(worst) >= 5 or (score < 15 and worst):
                break
            if any(abs(s["ts"] - w["ts"]) < 1800 for w in worst):
                continue
            worst.append({"ts": s["ts"], "score": score, "bad": score >= 15, "mem_used_mb": s.get("mem_used_mb"),
                          "psi_mem": s.get("psi_mem"), "psi_mem_full": s.get("psi_mem_full"),
                          "psi_io_full": s.get("psi_io_full"), "psi_io": s.get("psi_io"), "psi_cpu": s.get("psi_cpu"), "load1": s.get("load1"),
                          "swap_used_mb": s.get("swap_used_mb"), "why": why_at(con, b, s["ts"])})
        d["worst"] = worst
        d["events"] = _rows(con, "SELECT kind, count(*) AS n FROM events WHERE box=? AND ts>=? GROUP BY kind "
                                 "ORDER BY n DESC", (b, since))
        d["notable"] = _rows(con, "SELECT ts, kind, user, unit, detail, value FROM events WHERE box=? AND ts>=? AND kind "
                                  "IN ('oom_kill','earlyoom_kill','oomd_kill','unit_oom','boot','sample_gap',"
                                  "'hung_task','disk_error') ORDER BY ts DESC LIMIT 12", (b, since))
        d["users"] = _rows(con, "SELECT user, round(sum(coalesce(cpu_pct, 0) * 60) / 360000.0, 2) AS cpu_h, "
                                "max(mem_mb) AS mem_peak_mb, sum(coalesce(oom_kills, 0)) AS oom_kills, "
                                "sum(coalesce(mem_max_events, 0)) AS limit_hits FROM slices WHERE box=? AND ts>=? "
                                "AND user != '' GROUP BY user ORDER BY cpu_h DESC LIMIT 8", (b, since))
        d["agents"] = _rows(con, "SELECT user, agent, pid, min(started) AS started, count(*) AS samples, "
                                 "round(avg(coalesce(tree_cpu_pct, 0))) AS avg_cpu, max(tree_rss_mb) AS peak_mb "
                                 "FROM agents WHERE box=? AND ts>=? GROUP BY user, agent, pid "
                                 "ORDER BY samples * avg_cpu DESC LIMIT 6", (b, since))
        out["boxes"].append(d)
    try:
        out["plan"] = _rows(con, "SELECT p.window, p.utilization, p.resets_at, p.ts FROM plan_usage p JOIN "
                                 "(SELECT window, max(ts) AS ts FROM plan_usage GROUP BY window) l "
                                 "ON l.window=p.window AND l.ts=p.ts ORDER BY p.window")
    except sqlite3.Error:
        out["plan"] = []
    return out


def render_summary(s):
    L = []
    hrs = round((s["now"] - s["since"]) / 3600)
    if not s["boxes"]:
        return f"No telemetry in the last {hrs} h."
    for d in s["boxes"]:
        p, total = d["peak"], d.get("mem_total_mb")
        L.append(f"{d['box']}: last {hrs} h, {d['samples']} samples ({_when(d['first'])} .. {_when(d['last'])})")
        mem = f"memory peak {_gb(p['mem_used_mb'])} of {_gb(total)} in use (+ up to {_gb(p['mem_cache_mb'])} cache)"
        L.append(f"  Peaks: load {p['load1'] or 0:.1f} on {d['ncpu']} CPUs, CPU {p['cpu_pct'] or 0:.0f}%, {mem}, "
                 f"swap {_gb(p['swap_used_mb'])}")
        L.append(f"  Stalls (share of a minute something waited): memory {p['psi_mem'] or 0:.0f}% "
                 f"(all blocked {p['psi_mem_full'] or 0:.0f}%), disk {p['psi_io'] or 0:.0f}% "
                 f"(all blocked {p['psi_io_full'] or 0:.0f}%), CPU {p['psi_cpu'] or 0:.0f}%"
                 + (f"; hottest {p['temp_max_c']:.0f}°C" if p.get("temp_max_c") else ""))
        dk = d["disk"]
        if dk["free_gb"] is not None:
            L.append(f"  Disk: {dk['free_gb']:.0f} GB free of {dk['total_gb'] or 0:.0f} GB"
                     + (f" ({'+' if dk['change_gb'] >= 0 else ''}{dk['change_gb']:.1f} GB in this window)"
                        if dk["change_gb"] is not None else ""))
        if any(w["bad"] for w in d["worst"]):
            L.append("  Worst moments:")
        else:
            L.append("  No bad moments (memory, disk and CPU never stalled for long). The busiest one:")
        for w in d["worst"]:
            bits = []
            if total and w.get("mem_used_mb"):
                bits.append(f"memory {100 * w['mem_used_mb'] / total:.0f}% used")
            if (w.get("psi_mem") or 0) >= 1:
                bits.append(f"memory stall {w['psi_mem']:.0f}%")
            io = w.get("psi_io_full") if w.get("psi_io_full") is not None else w.get("psi_io")
            if (io or 0) >= 1:
                bits.append(f"disk stall {io:.0f}%")
            if (w.get("psi_cpu") or 0) >= 20:
                bits.append(f"CPU wait {w['psi_cpu']:.0f}%")
            if w.get("load1"):
                bits.append(f"load {w['load1']:.0f}")
            L.append(f"    {_when(w['ts'])}  " + ", ".join(bits))
            y = w["why"]
            if y["slices"]:
                L.append("      biggest: " + ", ".join(f"{r['user'] or r['slice']} {_gb(r['mem_mb'])}"
                                                      for r in y["slices"] if r.get("mem_mb") is not None))
            if y["procs"]:
                L.append("      busiest: " + ", ".join(f"{r['user']}/{r['name']} {r['cpu_pct'] or 0:.0f}% CPU "
                                                      f"{_gb(r['rss_mb'])}" for r in y["procs"]))
            for e in y["events"]:
                L.append(f"      {time.strftime('%H:%M', time.localtime(e['ts']))} {event_text(e)}")
        if d["notable"]:
            L.append("  Events:")
            for e in d["notable"]:
                L.append(f"    {_when(e['ts'])}  {event_text(e)}")
        other = [f"{e['kind']} {e['n']}" for e in d["events"]]
        if other:
            L.append("  Event counts: " + ", ".join(other))
        if d["users"]:
            L.append("  Accounts: " + "; ".join(
                f"{u['user']} {u['cpu_h']:.1f} CPU-h, peak {_gb(u['mem_peak_mb'])}"
                + (f", {u['oom_kills']} OOM kills" if u["oom_kills"] else "")
                + (f", hit its memory limit {u['limit_hits']}x" if u["limit_hits"] else "") for u in d["users"]))
        if d["agents"]:
            L.append("  Busiest agent sessions: " + "; ".join(
                f"{a['user']} {a['agent']} pid {a['pid']} ({a['samples']} min, avg {a['avg_cpu'] or 0:.0f}% CPU with "
                f"its tools, peak {_gb(a['peak_mb'])})" for a in d["agents"]))
        L.append("")
    for p in s.get("plan") or []:
        L.append(f"Claude plan {p['window']}: {p['utilization']:.0f}% (resets {p['resets_at'] or '?'})")
    L.append("More: `box telemetry patterns`, `box telemetry sql \"...\"` (tables: `box help telemetry`).")
    return "\n".join(L).rstrip()


# ---------------------------------------------------------------- detectors (`box telemetry patterns`)

KILL_KINDS = ("oom_kill", "earlyoom_kill", "oomd_kill", "unit_oom")


def _slope(points):
    """Least-squares slope of [(x, y)] (y per x), or None."""
    pts = [(x, y) for x, y in points if x is not None and y is not None]
    if len(pts) < 2:
        return None
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    den = sum((x - mx) ** 2 for x, _ in pts)
    return sum((x - mx) * (y - my) for x, y in pts) / den if den else None


def detect_oom_climb(con, box, since):
    """Each memory kill, and whether memory climbed steadily before it (a leak or a job growing) or jumped."""
    out = []
    for e in _rows(con, f"SELECT * FROM events WHERE box=? AND ts>=? AND kind IN ({','.join('?' * len(KILL_KINDS))}) "
                        "ORDER BY ts", (box, since, *KILL_KINDS)):
        t = e["ts"]
        if e["user"]:
            pts = [(r["ts"], r["mem_mb"]) for r in _rows(
                con, "SELECT ts, mem_mb FROM slices WHERE box=? AND user=? AND ts BETWEEN ? AND ? ORDER BY ts",
                (box, e["user"], t - 1800, t + 30))]
            whose = f"{e['user']}'s memory"
        else:
            pts = [(r["ts"], r["mem_used_mb"]) for r in _rows(
                con, "SELECT ts, mem_used_mb FROM samples WHERE box=? AND ts BETWEEN ? AND ? ORDER BY ts",
                (box, t - 1800, t + 30))]
            whose = "the box's memory"
        pts = [(x, y) for x, y in pts if y is not None]
        what = event_text(e)
        if len(pts) >= 5:
            first, peak = pts[0][1], max(y for _, y in pts)
            mins = (pts[-1][0] - pts[0][0]) / 60
            if peak - first >= 500 and peak >= 1.3 * max(first, 1):
                out.append({"sev": "high", "ts": t, "text": f"{what} at {_when(t)}: {whose} climbed from "
                            f"{_gb(first)} to {_gb(peak)} over the {mins:.0f} min before (a job growing or a leak)"})
                continue
            out.append({"sev": "high", "ts": t, "text": f"{what} at {_when(t)}: {whose} was steady around "
                        f"{_gb(first)} and jumped right before (one big allocation or a burst of jobs)"})
        else:
            out.append({"sev": "high", "ts": t, "text": f"{what} at {_when(t)} (too few samples before it to say how "
                                                         "memory got there)"})
    return out


def detect_limit_hits(con, box, since):
    """Accounts that keep running into their memory limit (kills or memory.max hits on several days)."""
    out = []
    first_raw = con.execute("SELECT min(ts) FROM slices WHERE box=? AND ts>=?", (box, since)).fetchone()[0]
    rows = _rows(con, "SELECT user, date(ts, 'unixepoch', 'localtime') AS day, sum(coalesce(mem_max_events, 0)) AS hits, "
                      "sum(coalesce(oom_kills, 0)) AS kills, max(mem_mb) AS peak FROM slices WHERE box=? AND ts>=? "
                      "AND user != '' GROUP BY user, day UNION ALL "
                      "SELECT user, date(ts, 'unixepoch', 'localtime'), sum(coalesce(mem_max_events, 0)), "
                      "sum(coalesce(oom_kills, 0)), max(mem_mb_max) FROM slices_hourly WHERE box=? AND ts>=? AND ts<? "
                      "AND user != '' GROUP BY user, 2",
                 (box, since, box, since, (first_raw or int(time.time())) // 3600 * 3600))
    kills = _rows(con, f"SELECT user, date(ts, 'unixepoch', 'localtime') AS day, count(*) AS n FROM events WHERE box=? "
                       f"AND ts>=? AND user != '' AND kind IN ({','.join('?' * len(KILL_KINDS))}) GROUP BY user, day",
                  (box, since, *KILL_KINDS))
    per = {}

    def acct(user):
        return per.setdefault(user, {"days": set(), "hits": 0, "cg_kills": 0, "ev_kills": 0, "peak": 0})
    for r in rows:
        u = acct(r["user"])
        if r["hits"] or r["kills"]:
            u["days"].add(r["day"])
        u["hits"] += r["hits"]
        u["cg_kills"] += r["kills"]
        u["peak"] = max(u["peak"], r["peak"] or 0)
    for r in kills:
        u = acct(r["user"])
        u["days"].add(r["day"])
        u["ev_kills"] += r["n"]
    for user, u in sorted(per.items()):
        # the cgroup counter and the journal see the same kernel kills; earlyoom/oomd kills only show in the journal
        u["kills"] = max(u["cg_kills"], u["ev_kills"])
        if len(u["days"]) >= 2 or u["hits"] + u["kills"] >= 5:
            out.append({"sev": "high" if len(u["days"]) >= 3 else "medium", "ts": None,
                        "text": f"{user} ran out of memory on {len(u['days'])} day(s) ({u['kills']} kills, "
                                f"{u['hits']} limit hits; peak {_gb(u['peak'])}): days {', '.join(sorted(u['days']))}"})
    return out


def detect_crash_loops(con, box, since):
    """Services that failed or restarted 5+ times within an hour."""
    out = []
    for r in _rows(con, "SELECT user, unit, CAST(ts / 3600 AS INTEGER) AS h, count(*) AS n, min(ts) AS first, max(ts) AS last "
                        "FROM events WHERE box=? AND ts>=? AND kind IN ('unit_failed', 'unit_restart') "
                        "GROUP BY user, unit, h HAVING n >= 5 ORDER BY first", (box, since)):
        key = (r["user"], r["unit"])
        prev = next((o for o in out if o["key"] == key), None)
        if prev:
            prev["hours"] += 1
            prev["n"] += r["n"]
            prev["last"] = r["last"]
            continue
        out.append({"key": key, "hours": 1, "n": r["n"], "first": r["first"], "last": r["last"]})
    res = []
    for o in out:
        user, unit = o["key"]
        res.append({"sev": "high" if o["hours"] > 1 else "medium", "ts": o["first"],
                    "text": f"{(user + '/') if user else ''}{unit} is crash-looping: {o['n']} failures/restarts in "
                            f"{o['hours']} hour(s), {_when(o['first'])} .. {_when(o['last'])}"})
    return res


def detect_disk_growth(con, box, since, now):
    """Each filesystem's growth over the last week and the days until it is full at that rate."""
    out = []
    pts = {}
    for r in _rows(con, "SELECT mount, ts, free_gb, total_gb FROM disks_hourly WHERE box=? AND ts>=? "
                        "UNION ALL SELECT mount, (ts/3600)*3600, min(free_gb), max(total_gb) FROM disks WHERE box=? "
                        "AND ts>=? AND ts>=coalesce((SELECT max(ts) FROM disks_hourly WHERE box=?), 0) "
                        "GROUP BY mount, ts/3600 ORDER BY ts", (box, now - 7 * 86400, box, now - 7 * 86400, box)):
        pts.setdefault(r["mount"], []).append(r)
    for mount, rows in pts.items():
        if len(rows) < 12 or rows[-1]["ts"] - rows[0]["ts"] < 86400:
            continue
        slope = _slope([(r["ts"] / 86400, r["free_gb"]) for r in rows])
        free, total = rows[-1]["free_gb"], rows[-1]["total_gb"]
        if slope is None or free is None:
            continue
        rate = -slope
        if rate <= 0.2:
            continue
        days = free / rate
        if days < 120:
            out.append({"sev": "high" if days < 14 else "medium", "ts": None,
                        "text": f"{mount} fills {rate:.1f} GB/day: {free:.0f} GB of {total or 0:.0f} GB free, "
                                f"full in ~{days:.0f} days at this week's rate"})
    return out


def detect_hot_agents(con, box, since, min_hours=2.0, min_cpu=80):
    """Agent sessions that ran for hours at high CPU (loops, runaway test watchers, builds left spinning)."""
    out = []
    for r in _rows(con, "SELECT a.user, a.agent, a.pid, a.started, count(*) AS n, "
                        "sum(coalesce(s.interval_s, 60)) AS secs, avg(a.tree_cpu_pct) AS tree, avg(a.cpu_pct) AS self, "
                        "max(a.tree_rss_mb) AS peak, min(a.ts) AS first, max(a.ts) AS last FROM agents a "
                        "LEFT JOIN samples s ON s.ts=a.ts AND s.box=a.box WHERE a.box=? AND a.ts>=? "
                        "AND a.tree_cpu_pct >= ? GROUP BY a.user, a.agent, a.pid, a.started HAVING secs >= ? "
                        "ORDER BY secs DESC", (box, since, min_cpu, min_hours * 3600)):
        mostly = ("the agent process itself (a loop or a stuck stream)" if (r["self"] or 0) >= 0.6 * (r["tree"] or 1)
                  else "programs it started (builds, tests, servers)")
        out.append({"sev": "medium", "ts": r["first"],
                    "text": f"{r['user']}'s {r['agent']} (pid {r['pid']}, started {_when(r['started'])}) used "
                            f">={min_cpu}% CPU for {r['secs'] / 3600:.1f} h between {_when(r['first'])} and "
                            f"{_when(r['last'])}: avg {r['tree'] or 0:.0f}%, mostly {mostly}; peak {_gb(r['peak'])}"})
    return out


def detect_near_freeze(con, box, since):
    """Stretches where the box was close to freezing (memory or disk stalls, swap storms, RAM all but gone), and
    restarts or sampling gaps that followed them."""
    S = _rows(con, "SELECT * FROM samples WHERE box=? AND ts>=? ORDER BY ts", (box, since))
    gaps = _rows(con, "SELECT ts, kind, detail, value FROM events WHERE box=? AND ts>=? AND "
                      "(kind='sample_gap' OR (kind='boot' AND detail IN ('unclean','watchdog','panic'))) ORDER BY ts",
                 (box, since))

    ncpu = max([s["ncpu"] for s in S if s.get("ncpu")] or [4])

    def bad(s):
        total = s.get("mem_total_mb") or 0
        avail = (total - (s.get("mem_used_mb") or 0)) if total else None
        return ((s.get("psi_mem_full") or 0) >= 10 or (s.get("psi_io_full") or 0) >= 30 or (s.get("psi_mem") or 0) >= 40
                or (s.get("swap_in_mb") or 0) >= 100 or (total and avail is not None and avail < 0.03 * total)
                or (s.get("procs_blocked") or 0) >= max(4, ncpu))
    eps, cur = [], None
    for s in S:
        if bad(s):
            if cur and s["ts"] - cur["end"] <= 600:
                cur["end"] = s["ts"]
                cur["rows"].append(s)
            else:
                cur = {"start": s["ts"], "end": s["ts"], "rows": [s]}
                eps.append(cur)
    out = []
    for ep in eps:
        rows = ep["rows"]
        peak = max(rows, key=lambda r: badness(r, ncpu))
        after = next((g for g in gaps if ep["end"] - 60 <= g["ts"] <= ep["end"] + 900), None)
        y = why_at(con, box, peak["ts"])
        top = ", ".join(f"{r['user'] or r['slice']} {_gb(r['mem_mb'])}" for r in y["slices"] if r.get("mem_mb"))
        mins = max(1, round((ep["end"] - ep["start"]) / 60) + 1)
        text = (f"{_when(ep['start'])}, {mins} min: memory stall up to {max((r.get('psi_mem') or 0) for r in rows):.0f}% "
                f"(fully blocked {max((r.get('psi_mem_full') or 0) for r in rows):.0f}%), disk stall up to "
                f"{max((r.get('psi_io_full') or 0) for r in rows):.0f}%, swap-in up to "
                f"{max((r.get('swap_in_mb') or 0) for r in rows):.0f} MB/min" + (f"; biggest users {top}" if top else ""))
        if after:
            text += "; then " + event_text(after)
        out.append({"sev": "high" if after or mins >= 5 else "medium", "ts": ep["start"], "text": text})
    for g in gaps:   # restarts and stalls with no warning in the samples
        if any(ep["end"] - 60 <= g["ts"] <= ep["end"] + 900 for ep in eps):
            continue
        before = _rows(con, "SELECT * FROM samples WHERE box=? AND ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1",
                       (box, g["ts"], g["ts"] - (g.get("value") or 0) - 900))
        pre = ""
        if before:
            b = before[0]
            pre = (f"; last sample {_when(b['ts'])}: memory {_gb(b.get('mem_used_mb'))} of {_gb(b.get('mem_total_mb'))}, "
                   f"memory stall {b.get('psi_mem') or 0:.0f}%, load {b.get('load1') or 0:.0f}")
            top = ", ".join(f"{r['user'] or r['slice']} {_gb(r['mem_mb'])}" for r in why_at(con, box, b["ts"])["slices"]
                            if r.get("mem_mb"))
            pre += f"; biggest users {top}" if top else ""
        out.append({"sev": "high" if g["kind"] == "boot" else "medium", "ts": g["ts"],
                    "text": f"{_when(g['ts'])}: {event_text(g)}{pre}"})
    first_raw = S[0]["ts"] if S else None
    for h in _rows(con, "SELECT * FROM samples_hourly WHERE box=? AND ts>=? AND ts<? ORDER BY ts",
                   (box, since, (first_raw or int(time.time())) // 3600 * 3600)):
        # older than the minute rows (or a fleet copy without them): hour-level peaks only
        if (h.get("psi_mem_full_max") or 0) >= 10 or (h.get("psi_mem_max") or 0) >= 40 or (
                h.get("psi_io_full_max") or 0) >= 30:
            top = _rows(con, "SELECT user, slice, mem_mb_max FROM slices_hourly WHERE box=? AND ts=? "
                             "ORDER BY coalesce(mem_mb_max, 0) DESC LIMIT 2", (box, h["ts"]))
            who = ", ".join(f"{r['user'] or r['slice']} up to {_gb(r['mem_mb_max'])}" for r in top if r["mem_mb_max"])
            out.append({"sev": "medium", "ts": h["ts"], "text": (
                f"hour of {_when(h['ts'])} (hourly data): memory stall peaked {h.get('psi_mem_max') or 0:.0f}% "
                f"(fully blocked {h.get('psi_mem_full_max') or 0:.0f}%), disk stall {h.get('psi_io_full_max') or 0:.0f}%"
                + (f"; biggest users {who}" if who else ""))})
    return sorted(out, key=lambda o: o["ts"] or 0)


DETECTORS = (
    ("near_freeze", "Close to freezing, stalls and restarts", detect_near_freeze),
    ("oom_climb", "Memory kills and what led to them", detect_oom_climb),
    ("limit_hits", "Accounts that keep running out of memory", detect_limit_hits),
    ("crash_loops", "Crash-looping services", detect_crash_loops),
    ("disk_growth", "Disks filling up", None),
    ("hot_agents", "Agents busy for hours at high CPU", detect_hot_agents),
)


def patterns(con, days=14, box=None, now=None):
    now = int(now or time.time())
    since = now - days * 86400
    out = {"since": since, "now": now, "boxes": []}
    for b in ([box] if box else sorted(boxes_in(con))):
        res = {"box": b, "found": {}}
        for key, _, fn in DETECTORS:
            try:
                res["found"][key] = (detect_disk_growth(con, b, since, now) if key == "disk_growth"
                                     else fn(con, b, since))
            except sqlite3.Error as e:
                res["found"][key] = [{"sev": "error", "ts": None, "text": f"detector failed: {e}"}]
        out["boxes"].append(res)
    return out


def render_patterns(p):
    L = []
    days = round((p["now"] - p["since"]) / 86400)
    for b in p["boxes"]:
        n = sum(len(v) for v in b["found"].values())
        L.append(f"{b['box']}: {n} finding(s) in the last {days} days")
        for key, title, _ in DETECTORS:
            found = b["found"].get(key) or []
            if not found:
                L.append(f"  {title}: none")
                continue
            L.append(f"  {title}:")
            for f in found[:15]:
                L.append(f"    [{f['sev']}] {f['text']}")
            if len(found) > 15:
                L.append(f"    ... and {len(found) - 15} more (--json for all)")
        L.append("")
    if not p["boxes"]:
        L.append("No telemetry yet.")
    return "\n".join(L).rstrip()


# ---------------------------------------------------------------- fleet (`box telemetry pull`)

FIRST_RAW_DAYS = 3        # a first pull brings this many days of minute rows (roll-ups and events: all)
PULL_MARGIN = 7200        # roll-ups for an hour are written after it ends: re-send the last two hours


def fleet_path(home):
    return os.path.join(home, "me", "telemetry.db")


def pull_due(home, every=3600, now=None):
    """True (and noted) when the last automatic pull is older than `every` seconds."""
    now = int(now or time.time())
    os.makedirs(os.path.join(home, "me"), mode=0o700, exist_ok=True)
    con = connect(fleet_path(home))
    try:
        if now - num(meta_get(con, "auto_pull"), 0) < every:
            return False
        meta_set(con, "auto_pull", now)
        return True
    finally:
        close(con)


def export(con, since=0, raw_since=None):
    """JSON lines of rows newer than `since` (minute rows: newer than raw_since), for another box's pull."""
    for table in EXPORT_TABLES:
        s = raw_since if table in RAW_TABLES and raw_since is not None else since
        cols = columns(con, table)
        cur = con.execute(f"SELECT {','.join(cols)} FROM {table} WHERE ts > ?", (s - PULL_MARGIN,))
        while True:
            chunk = cur.fetchmany(2000)
            if not chunk:
                break
            yield json.dumps({"t": table, "c": cols, "r": chunk}, separators=(",", ":"))


def import_lines(con, lines):
    """Rows from export() into this database. Tables and columns must be ones this schema has (the other box's
    text is never trusted as SQL)."""
    counts = {}
    for line in lines:
        try:
            d = json.loads(line)
            table, cols, rows = d["t"], d["c"], d["r"]
        except (ValueError, KeyError, TypeError):
            continue
        if table not in EXPORT_TABLES or not isinstance(cols, list) or not isinstance(rows, list):
            continue
        known = set(columns(con, table))
        if not cols or any(c not in known for c in cols):
            continue
        verb = "INSERT OR IGNORE" if table == "events" else "INSERT OR REPLACE"
        good = [tuple(r) for r in rows if isinstance(r, list) and len(r) == len(cols)
                and all(v is None or isinstance(v, (int, float, str)) for v in r)]
        with con:
            con.executemany(f"{verb} INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", good)
        counts[table] = counts.get(table, 0) + len(good)
    return counts


def pull(home, local_path, peers, ssh_opts=(), now=None, max_mb=2048, raw_days=14, keep_days=400):
    """Copy this box's readable rows and every peer's into ~/me/telemetry.db. {box: rows copied | error}."""
    now = int(now or time.time())
    con = connect(fleet_path(home))
    out = {}
    try:
        def since_for(name):
            v = meta_get(con, f"pulled:{name}")
            return (float(v), None) if v else (0.0, now - FIRST_RAW_DAYS * 86400)
        if local_path:
            since, raw = since_for("local")
            try:
                src = open_ro(local_path)
                try:
                    n = import_lines(con, export(src, since, raw))
                finally:
                    src.close()
                meta_set(con, "pulled:local", now)
                con.commit()
                out["here"] = sum(n.values())
            except sqlite3.Error as e:
                out["here"] = f"error ({e})"
        for name, dest in sorted(peers.items()):
            since, raw = since_for(name)
            cmd = " ".join([*NICE, "box", "_telemetry-export", "--since", str(int(since))]
                           + (["--raw-since", str(int(raw))] if raw else []))
            try:
                p = subprocess.Popen(["ssh", *ssh_opts, "-o", "Compression=yes", dest, cmd],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True)
                n = import_lines(con, p.stdout)
                err = p.stderr.read()[-300:]
                if p.wait() == 0:
                    meta_set(con, f"pulled:{name}", now)
                    con.commit()
                    out[name] = sum(n.values())
                elif "invalid choice" in err:
                    out[name] = "needs a treehouse update first"
                else:
                    out[name] = f"error (exit {p.returncode})"
            except OSError as e:
                out[name] = f"error ({e})"
        retention(con, raw_days, keep_days, max_mb, now)
    finally:
        close(con)
    return out


# ---------------------------------------------------------------- Claude plan usage (as the account itself)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_EVERY = 300
USAGE_THRESHOLDS = (80, 90)


def _fetch_usage(token):
    import urllib.request
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20", "Content-Type": "application/json",
        "User-Agent": "treehouse-telemetry"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read(1 << 20))


def usage_windows(data):
    """{window: (utilization %, resets_at)} from the usage answer (five_hour, seven_day, seven_day_opus, ...)."""
    out = {}
    for k, w in (data or {}).items():
        if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
            out[str(k)[:40]] = (float(w["utilization"]), str(w.get("resets_at") or "")[:40] or None)
    return out


def usage_alerts(windows, alerted):
    """[(window, threshold, util, resets)] to send now (one per window: the highest threshold newly crossed), and
    the updated set of tags already sent (window:threshold:reset-hour)."""
    send, alerted = [], set(alerted)
    for k, (util, resets) in sorted(windows.items()):
        crossed = [t for t in USAGE_THRESHOLDS if util >= t and f"{k}:{t}:{(resets or '')[:13]}" not in alerted]
        if crossed:
            send.append((k, max(crossed), util, resets))
            alerted.update(f"{k}:{t}:{(resets or '')[:13]}" for t in USAGE_THRESHOLDS if util >= t)
    return send, alerted


def usage_set(home, on):
    con = connect(fleet_path(home))
    try:
        meta_set(con, "usage", "on" if on else "off")
    finally:
        close(con)


def usage_on(home):
    p = fleet_path(home)
    if not os.path.exists(p):
        return False
    try:
        return read(p, lambda c: meta_get(c, "usage")) == "on"
    except sqlite3.Error:
        return False


def poll_usage(home, user, box, notify=None, force=False, fetch=None, now=None):
    """Read this account's own Claude login, record its plan windows, alert at 80% and 90%. Returns a dict."""
    now = int(now or time.time())
    cred = os.path.join(home, ".claude", ".credentials.json")
    try:
        fd = os.open(cred, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            if os.fstat(f.fileno()).st_uid != os.geteuid():
                return {"ok": False, "error": "that Claude login file isn't this account's"}
            tok = (json.load(f).get("claudeAiOauth") or {})
    except (OSError, ValueError, AttributeError):
        return {"ok": False, "error": "no Claude login on this box (box login claude)"}
    if not tok.get("accessToken"):
        return {"ok": False, "error": "this Claude login has no plan (API-key logins have no plan usage)"}
    exp = num(tok.get("expiresAt"))
    con = connect(fleet_path(home))
    try:
        last = num(meta_get(con, "usage_polled"), 0)
        if not force and now - last < USAGE_EVERY:
            return {"ok": True, "skipped": True}
        meta_set(con, "usage_polled", now)
        con.commit()
        if exp and exp / 1000 < now:
            return {"ok": False, "error": "the Claude login expired; Claude Code renews it the next time it runs"}
        try:
            data = (fetch or _fetch_usage)(tok["accessToken"])
        except Exception as e:  # noqa: BLE001 - network or API trouble: try again next tick
            return {"ok": False, "error": f"couldn't read plan usage ({type(e).__name__}: {str(e)[:120]})"}
        w = usage_windows(data)
        store(con, {"plan_usage": [{"ts": now, "box": box, "user": user, "window": k, "utilization": u,
                                    "resets_at": r} for k, (u, r) in w.items()]})
        try:
            alerted = set(json.loads(meta_get(con, "usage_alerted", "[]")))
        except ValueError:
            alerted = set()
        send, alerted = usage_alerts(w, alerted)
        for k, t, util, resets in send:
            if notify:
                notify(f"Claude {k.replace('_', ' ')} usage at {util:.0f}%",
                       f"Your Claude plan's {k.replace('_', ' ')} window is at {util:.0f}% (past {t}%). It resets "
                       f"{resets or 'later'}. Switch account or slow down agents before it runs out.")
        meta_set(con, "usage_alerted", json.dumps(sorted(alerted)[-60:]))
        return {"ok": True, "windows": {k: {"utilization": u, "resets_at": r} for k, (u, r) in w.items()},
                "alerts": [f"{k} {t}%" for k, t, _, _ in send]}
    finally:
        close(con)


# ---------------------------------------------------------------- command line (tests and hand runs)

def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog="sbtelemetry", description="telemetry by hand (the real job runs in "
                                "sharedbox-health sample)")
    s = p.add_subparsers(dest="cmd", required=True)
    c = s.add_parser("collect", help="take one sample into --db (state in --state)")
    c.add_argument("--db", required=True)
    c.add_argument("--state", required=True)
    c.add_argument("--box", default=socket.gethostname().split(".")[0])
    c.add_argument("--no-journal", action="store_true")
    c.add_argument("--print", action="store_true")
    b = s.add_parser("backfill", help="import health/metrics*.jsonl into --db")
    b.add_argument("--db", required=True)
    b.add_argument("--health-dir", default="/var/lib/sharedbox/health")
    b.add_argument("--box", default=socket.gethostname().split(".")[0])
    for name in ("summary", "patterns", "rollup"):
        x = s.add_parser(name)
        x.add_argument("--db", required=True)
        x.add_argument("--hours", type=int, default=24)
        x.add_argument("--days", type=int, default=14)
        x.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if a.cmd == "collect":
        try:
            with open(a.state) as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        data, new = Collector(a.box, state, journal=not a.no_journal).collect()
        con = connect(a.db)
        store(con, data)
        close(con)
        with open(a.state, "w") as f:
            json.dump(new, f)
        print(json.dumps(data, indent=1, default=str) if a.print else json.dumps({t: len(r) for t, r in data.items()}))
    elif a.cmd == "backfill":
        con = connect(a.db)
        print(backfill(con, a.box, a.health_dir))
        meta_set(con, "backfilled", int(time.time()))
        close(con)
    elif a.cmd == "rollup":
        con = connect(a.db)
        print(rollup(con))
        close(con)
    else:
        if a.cmd == "summary":
            res = read(a.db, lambda con: summary(con, a.hours))
            print(json.dumps(res, default=str) if a.json else render_summary(res))
        else:
            res = read(a.db, lambda con: patterns(con, a.days))
            print(json.dumps(res, default=str) if a.json else render_patterns(res))


if __name__ == "__main__":
    main()
