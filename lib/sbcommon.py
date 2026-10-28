"""Shared helpers for sharedbox: config, registry, cgroup reads, small utils.

Everything here is stdlib-only and safe to import from unprivileged code.
"""

import json
import os
import pwd
import grp
import re
import subprocess
import tempfile
import time

CONF_PATH = os.environ.get("SHAREDBOX_CONF", "/etc/sharedbox/sharedbox.conf")
STATE_DIR = os.environ.get("SHAREDBOX_STATE", "/var/lib/sharedbox")
SHARE_DIR = os.environ.get("SHAREDBOX_SHARE", "/usr/share/sharedbox")
LIB_DIR = os.environ.get("SHAREDBOX_LIB", "/usr/lib/sharedbox")
CGROUP_ROOT = "/sys/fs/cgroup"

REGISTRY = os.path.join(STATE_DIR, "users.json")
USAGE = os.path.join(STATE_DIR, "usage.json")
EVENTS = os.path.join(STATE_DIR, "events.jsonl")
SPOOL = os.path.join(STATE_DIR, "spool")
PENDING = os.path.join(STATE_DIR, "pending")
SLOTS = os.path.join(STATE_DIR, "slots")
REQUESTS = os.path.join(STATE_DIR, "requests")
TOOLS_JSON = os.path.join(STATE_DIR, "tools.json")
ALERTS = os.path.join(STATE_DIR, "alerts")   # per-friend security alerts (tripwire), root-owned, group=friend
ALERT_DAYS = 7

DEFAULTS = {
    # who may join: open | approve | closed
    "JOIN_MODE": "open",
    "MAX_USERS": "25",
    "ADMIN_USERS": "",
    "FRIENDS_GROUP": "friends",
    "SSH_GROUP": "ssh-users",
    "SERVICE_USER": "sharedbox",
    "FRIEND_UID_MIN": "2000",
    "FRIEND_UID_MAX": "2999",
    # web dashboard + join page; "tailscale" = bind to this box's tailscale IPv4
    "WEB_BIND": "tailscale",
    "WEB_PORT": "8090",
    # per-friend port block: PORT_BASE + index*PORT_BLOCK .. +PORT_BLOCK-1
    "PORT_BASE": "20000",
    "PORT_BLOCK": "100",
    # per-friend resource limits (systemd slice properties)
    "CPU_QUOTA": "300%",
    "CPU_WEIGHT": "20",
    "IO_WEIGHT": "20",
    "MEMORY_HIGH": "7G",
    "MEMORY_MAX": "16G",
    # dynamic balancing: while the box has room, friends may use up to these instead
    # (sharedbox-janitor guard drops them back to CPU_QUOTA / MEMORY_HIGH when the box gets busy)
    "BURST_CPU_QUOTA": "500%",
    "BURST_MEMORY_HIGH": "15G",
    "BALANCE_MEM_TIGHT_PCT": "25",   # box free memory below this % -> everyone back to MEMORY_HIGH
    "BALANCE_MEM_OK_PCT": "40",      # ...and bursting is allowed again once it's back above this %
    "BALANCE_CPU_BUSY": "0.75",      # 1-min load above this x cores -> everyone back to CPU_QUOTA
    "MEMORY_SWAP_MAX": "1G",
    "TASKS_MAX": "2048",
    "OOM_SCORE_ADJ": "500",
    "BUILD_JOBS": "2",
    # disk + cleanup policy
    "DISK_BUDGET_GB": "40",
    "PROJECT_TTL_DAYS": "7",
    "ARTIFACT_TTL_DAYS": "3",
    "CACHE_TTL_DAYS": "7",
    "TMP_TTL_DAYS": "2",
    "WARN_DAYS": "3",
    "OVER_BUDGET_GRACE_HOURS": "24",
    "DISK_LOW_FREE_PCT": "20",
    "DISK_CRITICAL_FREE_PCT": "10",
    # heavy-job queue (box heavy)
    "HEAVY_SLOTS": "2",
    "HEAVY_WAIT_MAX_MIN": "30",
    # when the box may reboot for security updates (only told to agents; the actual
    # schedule is Unattended-Upgrade::Automatic-Reboot-Time in /etc/apt/apt.conf.d)
    "REBOOT_TIME": "04:00",
    "SCHEDULED_REBOOT_DAYS": "Wed,Sun",
    "TOOLS_ANDROID": "0",
    "ANDROID_SDK_ROOT": "/opt/android-sdk",
    # email (weekly health report + urgent alerts)
    "MAIL_TO": "",
    "MAIL_FROM": "",
    "SMTP_HOST": "smtp.gmail.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "",
    "SMTP_PASSWORD_FILE": "/etc/sharedbox/smtp-password",
    "HEALTH_REPORT_TIME": "Mon 09:00",
    # trap (decoy ports, bait URLs, AI-crawler maze)
    "TRAP_ENABLED": "1",
    "TRAP_BIND": "lan,tailscale",
    "TRAP_PORTS": "21,23,445,1433,2375,2376,3306,3389,5432,5900,6379,8080,8888,9200,11211,27017",
    "TRAP_BOMB_GB": "10",
    "TRAP_MAX_CONNS": "1000",
    "TRAP_MAX_BOMBS": "4",
    "TRAP_BOMBS_PER_IP_HOUR": "3",
    "TRAP_TARPIT_HOURS": "6",
    "TRAP_MAZE_PREFIX": "archive",
    "TRAP_MAZE_DELAY": "3-8",
    "TRAP_BAIT_PATHS": ("/.env /.env.local /.env.production /.git/ /.aws/ /.ssh/ /wp-login.php /wp-admin/ "
                        "/wp-config.php /xmlrpc.php /phpmyadmin/ /pma/ /phpinfo.php /info.php /shell.php "
                        "/admin.php /server-status /actuator/ /.DS_Store /.vscode/ /config.php /backup.zip "
                        "/backup.sql /db.sql /vendor/phpunit/ /cgi-bin/ /boaform/ /HNAP1 /owa/ /solr/ "
                        "/console/ /.htpasswd /.svn/ /api/.env /laravel/.env /docker-compose.yml"),
    "TRAP_AI_AGENTS_EXTRA": "",
    "TRAP_PROXY_BASE": "8800",
    # tripwire decoy secrets
    "CANARY_DIR": "/srv/backups/prod",
    "CANARY_ACTION": "alert",
}


def load_config(path=CONF_PATH):
    cfg = dict(DEFAULTS)
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return cfg


def cfg_int(cfg, key):
    return int(cfg[key])


def admin_users(cfg):
    return [u.strip() for u in cfg["ADMIN_USERS"].split(",") if u.strip()]


# ---------- json files ----------

def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, data, mode=0o644, owner=None):
    """Atomic write via tmp file + rename in the same directory.

    chmod/chown go through the fd: `d` may be writable by a less-privileged user (the spool),
    who could swap the tmp name for a symlink before a path-based chown.
    """
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fchmod(f.fileno(), mode)
            if owner is not None:
                os.fchown(f.fileno(), owner[0], owner[1])
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def log_event(kind, user=None, **fields):
    rec = {"ts": int(time.time()), "kind": kind, "user": user}
    rec.update(fields)
    try:
        with open(EVENTS, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def recent_events(n=50):
    try:
        with open(EVENTS) as f:
            lines = f.readlines()[-n:]
    except FileNotFoundError:
        return []
    out = []
    for l in lines:
        try:
            out.append(json.loads(l))
        except json.JSONDecodeError:
            pass
    return out


# ---------- registry ----------

def security_alert(user):
    """The friend's latest tripwire alert if it is recent, else None."""
    rec = read_json(os.path.join(ALERTS, f"{user}.json"))
    if rec and time.time() - rec.get("ts", 0) < ALERT_DAYS * 86400:
        return rec
    return None


def registry():
    return read_json(REGISTRY, {"users": {}})


def friend_users(reg=None):
    """username -> record, for users that still exist on the system."""
    reg = reg or registry()
    out = {}
    for name, rec in reg.get("users", {}).items():
        try:
            pw = pwd.getpwnam(name)
        except KeyError:
            continue
        rec = dict(rec)
        rec["uid"] = pw.pw_uid
        rec["gid"] = pw.pw_gid
        rec["home"] = pw.pw_dir
        out[name] = rec
    return out


def port_range(cfg, uid):
    idx = uid - cfg_int(cfg, "FRIEND_UID_MIN")
    base = cfg_int(cfg, "PORT_BASE") + idx * cfg_int(cfg, "PORT_BLOCK")
    return base, base + cfg_int(cfg, "PORT_BLOCK") - 1


def user_limits(cfg, rec=None):
    lim = {k: cfg[k] for k in ("CPU_QUOTA", "CPU_WEIGHT", "IO_WEIGHT", "MEMORY_HIGH",
                               "MEMORY_MAX", "MEMORY_SWAP_MAX", "TASKS_MAX",
                               "DISK_BUDGET_GB", "BURST_CPU_QUOTA", "BURST_MEMORY_HIGH")}
    if rec:
        lim.update(rec.get("limits", {}))
    return lim


# ---------- sizes ----------

_UNITS = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}


def parse_size(s):
    s = str(s).strip().upper()
    if s in ("INFINITY", "MAX", ""):
        return None
    if s[-1] in _UNITS:
        return int(float(s[:-1]) * _UNITS[s[-1]])
    return int(s)


def human(n):
    if n is None:
        return "∞"
    n = float(n)
    for u in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or u == "T":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


def printable(s):
    """Friend-controlled text for an admin's terminal: no escape sequences or other controls."""
    return "".join(c if c.isprintable() else "?" for c in str(s))


# ---------- agent-reported issues (each user's ~/.sharedbox/issues.jsonl) ----------

ISSUES_REL = os.path.join(".sharedbox", "issues.jsonl")
ISSUE_KINDS = ("slow", "broken", "missing", "limit", "confusing", "other")


def read_issues(since=0, users=None, limit=1 << 20):
    """Every user's agent-reported issues newer than `since`, oldest first.

    Runs as root over files users control: O_NOFOLLOW + owner/regular-file checks on the opened fd,
    only the last `limit` bytes, and malformed lines are skipped. Text is NOT sanitized here; use
    printable()/html.escape when showing it.
    """
    import stat as _stat
    out = []
    for pw in pwd.getpwall():
        if pw.pw_uid < 1000 or pw.pw_uid >= 65534 or (users and pw.pw_name not in users):
            continue
        path = os.path.join(pw.pw_dir, ISSUES_REL)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            continue
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(fd)
            if not _stat.S_ISREG(st.st_mode) or st.st_uid != pw.pw_uid or st.st_nlink != 1:
                continue
            if st.st_size > limit:
                f.seek(st.st_size - limit)
                f.readline()  # drop the partial first line
            data = f.read(limit).decode(errors="replace")
        for line in data.splitlines():
            try:
                r = json.loads(line)
                ts = float(r.get("ts", 0))
            except (ValueError, TypeError, AttributeError):
                continue
            if ts >= since and isinstance(r.get("text"), str):
                r["user"] = pw.pw_name  # the file's owner, not whatever the line claims
                r["kind"] = r.get("kind") if r.get("kind") in ISSUE_KINDS else "other"
                out.append(r)
    out.sort(key=lambda r: r["ts"])
    return out


def group_issues(issues):
    """Collapse repeats: [(count, users, first_ts, last_ts, sample)] most-reported first."""
    groups = {}
    for r in issues:
        key = (r["kind"], re.sub(r"\W+", " ", r["text"].lower()).strip()[:80])
        g = groups.setdefault(key, {"n": 0, "users": set(), "first": r["ts"], "last": r["ts"], "r": r})
        g["n"] += 1
        g["users"].add(r["user"])
        g["last"], g["r"] = r["ts"], r
    return sorted(((g["n"], sorted(g["users"]), g["first"], g["last"], g["r"]) for g in groups.values()),
                  key=lambda x: (-len(x[1]), -x[0], -x[3]))


# ---------- cgroup / system reads (world-readable) ----------

def slice_path(uid):
    return os.path.join(CGROUP_ROOT, "user.slice", f"user-{uid}.slice")


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def read_cgroup(path):
    """Snapshot of one cgroup: cpu usec, memory, pids, throttling, limits."""
    if not os.path.isdir(path):
        return None
    stat = {}
    for line in (_read(os.path.join(path, "cpu.stat")) or "").splitlines():
        k, _, v = line.partition(" ")
        stat[k] = int(v)

    def num(name):
        v = _read(os.path.join(path, name))
        if v is None or v == "max":
            return None
        try:
            return int(v.split()[0])
        except ValueError:
            return None

    cpu_max = (_read(os.path.join(path, "cpu.max")) or "max").split()
    quota = None
    if cpu_max and cpu_max[0] != "max":
        quota = int(cpu_max[0]) / int(cpu_max[1])
    return {
        "t": time.monotonic(),
        "cpu_usec": stat.get("usage_usec", 0),
        "throttled_usec": stat.get("throttled_usec", 0),
        "mem": num("memory.current") or 0,
        "mem_high": num("memory.high"),
        "mem_max": num("memory.max"),
        "swap": num("memory.swap.current") or 0,
        "pids": num("pids.current") or 0,
        "pids_max": num("pids.max"),
        "cpu_quota_cores": quota,
    }


def read_psi(kind):
    """{'some': avg60, 'full': avg60} from /proc/pressure/<kind>."""
    out = {}
    for line in (_read(f"/proc/pressure/{kind}") or "").splitlines():
        parts = line.split()
        vals = dict(p.split("=") for p in parts[1:])
        out[parts[0]] = float(vals.get("avg60", 0))
    return out


def meminfo():
    m = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        m[k] = int(v.split()[0]) * 1024
    return m


def disk_free(path="/home"):
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return total, free


def box_load():
    """Box-wide pressure summary used by `box status` and the dashboard."""
    total, free = disk_free()
    mi = meminfo()
    la = (_read("/proc/loadavg") or "0 0 0").split()[:3]
    cpu = read_psi("cpu").get("some", 0.0)
    mem = read_psi("memory").get("some", 0.0)
    io = read_psi("io").get("some", 0.0)
    if cpu > 60 or mem > 20 or io > 40:
        level = "busy"
    elif cpu > 25 or mem > 5 or io > 15:
        level = "moderate"
    else:
        level = "calm"
    return {
        "level": level,
        "ncpu": os.cpu_count(),
        "loadavg": [float(x) for x in la],
        "psi_cpu": cpu, "psi_mem": mem, "psi_io": io,
        "mem_total": mi.get("MemTotal", 0),
        "mem_avail": mi.get("MemAvailable", 0),
        "disk_total": total, "disk_free": free,
    }


# ---------- tailscale ----------

def tailscale_ip4():
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=5).stdout.split()
        return out[0] if out else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def tailscale_whois(ip):
    """Return {'login','id','node'} for a tailnet peer IP, or None."""
    try:
        r = subprocess.run(["tailscale", "whois", "--json", ip], capture_output=True,
                           text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    up = d.get("UserProfile") or {}
    node = d.get("Node") or {}
    if not up.get("LoginName") or not up.get("ID"):
        return None
    return {"login": up["LoginName"], "id": str(up["ID"]),
            "display": up.get("DisplayName") or up["LoginName"],
            "node": (node.get("Name") or "").rstrip(".")}


# ---------- validation ----------

USERNAME_RE = re.compile(r"^[a-z][a-z0-9]{2,15}$")
RESERVED = {"root", "admin", "sharedbox", "friends", "daemon", "bin", "sys", "sync",
            "games", "man", "mail", "news", "uucp", "proxy", "backup", "list", "irc",
            "nobody", "systemd", "syslog", "ubuntu", "ssh", "sshd", "docker", "www",
            "sudo", "adm", "staff", "users", "test", "guest", "operator"}
KEY_TYPES = ("ssh-ed25519", "sk-ssh-ed25519@openssh.com", "ecdsa-sha2-nistp256",
             "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
             "sk-ecdsa-sha2-nistp256@openssh.com", "ssh-rsa")


def validate_username(name, cfg):
    if not USERNAME_RE.match(name or ""):
        return "username must be 3-16 chars: lowercase letters/digits, starting with a letter"
    if name in RESERVED or name in admin_users(cfg):
        return "that username is reserved"
    try:
        pwd.getpwnam(name)
        return "that username is taken"
    except KeyError:
        pass
    try:
        grp.getgrnam(name)
        return "that username is taken"
    except KeyError:
        pass
    return None


def validate_pubkey(key):
    """Returns (normalized_key, error). Only plain single-line public keys, no options."""
    key = (key or "").strip()
    if not key or "\n" in key or "\r" in key or len(key) > 4096:
        return None, "paste exactly one public key line (e.g. the contents of ~/.ssh/id_ed25519.pub)"
    parts = key.split()
    if len(parts) < 2 or parts[0] not in KEY_TYPES:
        return None, "that doesn't look like an SSH public key (expected ssh-ed25519 AAAA...)"
    if not re.fullmatch(r"[A-Za-z0-9+/=]+", parts[1]):
        return None, "malformed key data"
    comment = re.sub(r"[^A-Za-z0-9@._+-]", "", " ".join(parts[2:]))[:64]
    norm = f"{parts[0]} {parts[1]}" + (f" {comment}" if comment else "")
    with tempfile.NamedTemporaryFile("w", suffix=".pub") as f:
        f.write(norm + "\n")
        f.flush()
        r = subprocess.run(["ssh-keygen", "-l", "-f", f.name], capture_output=True, text=True)
    if r.returncode != 0:
        return None, "ssh-keygen rejected that key"
    bits = int(r.stdout.split()[0])
    if parts[0] == "ssh-rsa" and bits < 3072:
        return None, "RSA keys must be at least 3072 bits (ed25519 recommended)"
    return norm, None
