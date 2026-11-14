"""Shared helpers for sharedbox: config, registry, cgroup reads, small utils.

Everything here is stdlib-only and safe to import from unprivileged code.
"""

import hashlib
import hmac
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
# versioned coding agents: AGENTS_DIR/<slug>/<version>/ (npm prefix) + <slug>/current -> newest
AGENTS_DIR = os.environ.get("SHAREDBOX_AGENTS", "/opt/sharedbox/agents")
AGENT_PINS = os.path.join(STATE_DIR, "agent-pins")    # <user>/<slug> -> pinned version dir (root writes)
AGENT_PREF_REL = os.path.join(".sharedbox", "agent-updates")  # "auto" | "manual", written by the user
AGENT_RENAMED = {"opencode-ai": "@opencode/cli"}   # npm packages that moved (opencode v2 = @opencode/cli)
ALERTS = os.path.join(STATE_DIR, "alerts")   # per-friend security alerts (tripwire), root-owned, group=friend
ALERT_DAYS = 7
# public front door (sharedbox-gate): invite, session and device-login records, keyed by sha256(token).
# Root writes them (0640 root:sharedbox); the gate and sharedbox-web only read them.
GATE_DIR = os.path.join(STATE_DIR, "gate")
ADMIN_DROP = os.path.join(STATE_DIR, "admin")          # 1733: admins drop requests (box invite), root acts
ADMIN_REPLIES = os.path.join(STATE_DIR, "admin-replies")  # 0711: root's answer, readable only by the asker
GATE_KINDS = ("invites", "sessions", "logins")
# group-owned shared servers (box shared): registry root writes (0644), and a drop box like ADMIN_DROP
# that friends and admins write to
SHARED_JSON = os.path.join(STATE_DIR, "shared.json")
SHARED_DROP = os.path.join(STATE_DIR, "shared-drop")        # 1733
SHARED_REPLIES = os.path.join(STATE_DIR, "shared-replies")  # 0711
# skills every account's agents see (symlinked into ~/.claude/skills and ~/.agents/skills):
# the ones shipped with sharedbox, then the ones admins publish with `box skills share` (those win)
SKILLS_BUILTIN = os.path.join(SHARE_DIR, "skills")
SKILLS_SHARED = os.path.join(STATE_DIR, "skills")
SKILL_NAME_RE = r"^[a-z0-9][a-z0-9-]{0,47}$"
SKILL_MAX_FILES, SKILL_MAX_BYTES = 40, 256 << 10
CF_CERTS_CACHE = os.path.join(GATE_DIR, "cf-certs.json")  # Cloudflare Access signing keys (root writes)
# last time each friend's T3 Code was opened through the public gate: the gate (user sharedbox) touches
# T3_SEEN/<user>, the janitor reads the mtimes to put idle T3 servers to sleep (T3_IDLE_DAYS)
T3_SEEN = os.path.join(STATE_DIR, "t3-seen")
# the scheduled reboot's state while it waits for agents to finish a turn (root writes, 0644)
REBOOT_STATE = os.path.join(STATE_DIR, "reboot.json")

DEFAULTS = {
    # the name people see on pages, emails and broadcasts ("treehouse · <hostname>"). Commands, paths and
    # unit names stay sharedbox-*.
    "BRAND": "treehouse",
    # who may join: open | approve | closed. Not set in the conf file + PUBLIC_MODE=cloudflare = closed
    # (invites only; see load_config)
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
    # an expired project is moved to ~/.sharedbox/archive/ (restorable: `box restore NAME`) and deleted this
    # many days later; archives still count toward the disk budget and go first when someone is over it
    "ARCHIVE_DAYS": "14",
    # a friend's T3 Code server (~0.4 GB RAM) naps after this many minutes unused; its port stays open and the
    # next connection (T3 app, browser) wakes it in a few seconds. Accounts linked to T3 Connect use T3_IDLE_DAYS
    # instead (T3's relay only runs while T3 does, so a napping T3 is unreachable that way until a visit wakes it)
    "T3_NAP_MINUTES": "60",
    "T3_IDLE_DAYS": "3",
    # a Docker database stack (Postgres, Supabase, Redis, ...) is stopped after this many minutes with no connection
    # and no queries; everyone's, the owner's too. Volumes are kept: `supabase start` / `docker compose up -d`
    "DOCKER_NAP_MINUTES": "60",
    # no swap at all: sharedbox-tune makes /swap.treehouse (min(8 GB, max(2 GB, RAM/2))) when the disk has room
    "TUNE_SWAP": "auto",
    "OVER_BUDGET_GRACE_HOURS": "24",
    "DISK_LOW_FREE_PCT": "20",
    "DISK_CRITICAL_FREE_PCT": "10",
    # heavy-job queue (box heavy)
    "HEAVY_SLOTS": "2",
    "HEAVY_WAIT_MAX_MIN": "30",
    # when the box may reboot for security updates (only told to agents; on Debian/Ubuntu the actual
    # schedule is Unattended-Upgrade::Automatic-Reboot-Time in /etc/apt/apt.conf.d; Arch has no
    # automatic updates, so only SCHEDULED_REBOOT_DAYS reboots it there)
    "REBOOT_TIME": "04:00",
    "SCHEDULED_REBOOT_DAYS": "Wed,Sun",
    "TOOLS_ANDROID": "0",
    # coding agents (TOOLS_NPM): "auto" = everyone gets the newest version daily unless they opt out
    # (`box updates manual`); "manual" = people stay on the version they had unless they opt in.
    # Either way, a pinned version is moved to the newest one when it has a security advisory at or
    # above AGENT_FORCE_SEVERITY (low|moderate|high|critical|off) that the newest version doesn't.
    "AGENT_UPDATES_DEFAULT": "auto",
    "AGENT_FORCE_SEVERITY": "high",
    "ANDROID_SDK_ROOT": "/opt/android-sdk",
    # `box request install PKG`: install a requested system package right away (no owner needed) when it's in
    # the distro's repos, not installed yet, matches none of REQUEST_DENY (globs, also checked against every
    # package it would pull in) and pulls in at most REQUEST_MAX_DEPS new packages; at most REQUEST_DAILY_LIMIT
    # auto-installed packages per friend per day. Everything else goes to REQUEST_REVIEWER.
    "REQUEST_AUTO_INSTALL": "yes",
    # vetted third-party packages (sbdistro.VETTED: Google Chrome) may add their vendor repo by themselves
    "REQUEST_VETTED_SOURCES": "yes",
    # needs a second look (the AI reviewer) instead of installing right away: privileged container/VM
    # engines, mail servers, system-wide app stores, hacking/sniffing/mining/anonymizing tools. Servers and
    # daemons are fine: a requested package never starts or enables a system service.
    "REQUEST_DENY": ("docker* containerd* runc lxc* lxd* incus* libvirt* virtualbox* snapd flatpak postfix* exim* "
                     "sendmail* opensmtpd* nmap masscan zmap hydra* medusa john hashcat aircrack-ng ettercap* "
                     "bettercap* sqlmap nikto metasploit* tcpdump wireshark* tshark dsniff macchanger xmrig* "
                     "cpuminer* ethminer* tor torsocks proxychains* firmware* linux-firmware*"),
    # who answers a request the rules above don't auto-install: "ai" = an AI reviewer (the owner only gets an
    # hourly FYI), "owner" = email the owner to approve. The AI is OpenRouter (REQUEST_REVIEW_MODEL, paid with
    # the owner's OpenRouter key from OpenCode's login or OPENROUTER_KEY_FILE), else Claude Code as the owner's
    # account (REQUEST_REVIEW_CLAUDE_MODEL, no tools); if neither answers, the owner is asked.
    "REQUEST_REVIEWER": "ai",
    "REQUEST_REVIEW_MODEL": "~deepseek/deepseek-flash-latest",
    "REQUEST_REVIEW_CLAUDE_MODEL": "claude-sonnet-5-5",
    "OPENROUTER_KEY_FILE": "/etc/sharedbox/openrouter-key",
    # never installed on request, whatever the reviewer says (the box's own security: root, logins, network,
    # boot); also checked against every package they'd pull in. The owner can still approve one by hand.
    "REQUEST_NEVER": ("sudo doas polkit* pkexec openssh-server linux linux-lts linux-zen linux-hardened "
                      "linux-image* linux-headers* linux-modules* kernel* grub* systemd systemd-* libpam* shadow "
                      "passwd login apparmor* selinux* sssd* ufw iptables* nftables firewalld network-manager* "
                      "netplan* tailscale* cloudflared wireguard* openvpn* cron cron-* cronie at"),
    # temporarily open requests for some users (a hackathon): they skip REQUEST_DENY, REQUEST_MAX_DEPS and
    # REQUEST_DAILY_LIMIT (not REQUEST_NEVER) until REQUEST_OPEN_UNTIL ("YYYY-MM-DD HH:MM", local; empty = no end)
    "REQUEST_OPEN_USERS": "",
    "REQUEST_OPEN_UNTIL": "",
    "REQUEST_MAX_DEPS": "500",
    "REQUEST_DAILY_LIMIT": "200",
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
    # public front door (sharedbox-gate behind Tailscale Funnel); empty PUBLIC_URL = off
    "PUBLIC_URL": "",
    "GATE_PORT": "8091",
    # where friends' T3 Code listens, as seen from the gate: "tailscale" = this box's Tailscale IPv4
    # (that's what `box t3` binds T3 to), or an explicit address
    "GATE_BACKEND_HOST": "tailscale",
    "SESSION_DAYS": "90",
    "INVITE_DAYS": "7",
    "LOGIN_LINK_MINUTES": "10",
    # how `invite` picks a username when none is given: "email" (from the email or note) or "lotr"
    "AUTO_USERNAMES": "email",
    # how the public front door is reached: "funnel" (Tailscale Funnel; the default when PUBLIC_URL is set)
    # or "cloudflare" (Cloudflare Tunnel + Access: every request carries a Cloudflare-signed JWT the gate
    # verifies). `sharedbox-admin cloudflare setup` fills in the CF_* values.
    "PUBLIC_MODE": "",
    "CF_ACCOUNT_ID": "",
    "CF_TEAM_DOMAIN": "",
    "CF_ACCESS_AUD": "",
    "CF_TUNNEL_ID": "",
    "CF_ACCESS_APP_ID": "",
    "CF_ACCESS_POLICY_ID": "",
    "CF_OWNER_EMAILS": "",
    # most emails allowed through Cloudflare Access (Zero Trust Free = 50 users; stay well under, never pay)
    "CF_SEAT_LIMIT": "40",
    # port of the owner's own T3 Code on GATE_BACKEND_HOST; set it to let CF_OWNER_EMAILS open it on the public site
    "OWNER_T3_PORT": "",
    # the owner's "Update treehouse" button installs the latest commit of UPDATE_BRANCH from this git checkout
    "UPDATE_SOURCE": "",
    "UPDATE_BRANCH": "main",
    # serve group servers on the public site (/s/NAME/)? Off: any member can edit a group server's code, and on the
    # same site as everyone's T3 Code a malicious script there could act as whoever opens it. Tailnet links only.
    "SHARED_PUBLIC_LINKS": "no",
    "OWNER_T3_USER": "",   # account that runs that T3 (default: the first ADMIN_USERS)
    "CF_TOKEN_FILE": "/etc/sharedbox/cloudflare-token",
    "CF_TUNNEL_ENV": "/etc/sharedbox/cloudflared.env",
    "CF_SESSION_HOURS": "720",
    # SSH through Cloudflare (friends' laptops: `ssh treehouse` via `cloudflared access ssh`, no VPN). Written by
    # `sharedbox-admin cloudflare setup|ssh`; set SSH_HOST before that to pick the hostname, or "off".
    "SSH_HOST": "",
    "CF_SSH_APP_ID": "",
    # group-owned shared servers (box shared): code in SHARED_DIR/NAME, every friend and admin may edit and
    # run it; each runs as SHARED_USER (no login, not in FRIENDS_GROUP/SSH_GROUP) in one slice capped below
    "SHARED_USER": "sbshared",
    "SHARED_GROUP": "sbshared",
    "SHARED_DIR": "/srv/shared",
    "SHARED_PORT_BASE": "19000",
    "SHARED_PORT_COUNT": "100",
    "SHARED_MAX": "20",
    "SHARED_MEMORY_MAX": "4G",
    "SHARED_CPU_QUOTA": "200%",
}


def load_config(path=CONF_PATH):
    cfg = dict(DEFAULTS)
    explicit = set()
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
                explicit.add(k.strip())
    except FileNotFoundError:
        pass
    # behind Cloudflare Access only invited emails get in at all, so self-signup makes no sense there:
    # invites only, unless the owner chose otherwise in the conf file
    if "JOIN_MODE" not in explicit and (cfg.get("PUBLIC_MODE") or "").strip().lower() == "cloudflare":
        cfg["JOIN_MODE"] = "closed"
    return cfg


def brand(cfg):
    return (cfg.get("BRAND") or "").strip() or "treehouse"


def brand_host(cfg):
    """"treehouse · sietchtabr": the name on pages and emails, with this machine's name second."""
    import socket
    return f"{brand(cfg)} · {socket.gethostname()}"


def cfg_bool(cfg, key):
    return str(cfg.get(key, "")).strip().lower() in ("1", "yes", "true", "on")


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


def agent_slug(name):
    """'@anthropic-ai/claude-code' -> 'anthropic-ai+claude-code' (a directory name)"""
    return name.lstrip("@").replace("/", "+")


def agent_pref(cfg, home=None):
    """(mode, mtime) of a user's agent update choice; run as that user. mtime 0 = never chose."""
    home = home or os.path.expanduser("~")
    try:
        path = os.path.join(home, AGENT_PREF_REL)
        with open(path) as f:
            mode = f.read(16).strip()
        mtime = os.stat(path).st_mtime
    except OSError:
        mode, mtime = "", 0
    if mode not in ("auto", "manual"):
        mode, mtime = (cfg.get("AGENT_UPDATES_DEFAULT") if cfg.get("AGENT_UPDATES_DEFAULT") in ("auto", "manual")
                       else "auto"), 0
    return mode, mtime


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


# ---------- reboots ----------

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def reboot_days(cfg):
    """SCHEDULED_REBOOT_DAYS as ['Wed', 'Sun'] (config order, bad entries dropped)."""
    out = []
    for d in (cfg.get("SCHEDULED_REBOOT_DAYS") or "").split(","):
        d = d.strip()[:3].title()
        if d in WEEKDAYS and d not in out:
            out.append(d)
    return out


def _and(items):
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def reboot_schedule_text(cfg):
    """'twice a week (Wed and Sun at 04:00)', or "" when there are no scheduled reboots."""
    days, t = reboot_days(cfg), cfg.get("REBOOT_TIME") or "04:00"
    if not days:
        return ""
    if len(days) == 7:
        return f"every day at {t}"
    times = {1: "once a week", 2: "twice a week", 3: "three times a week"}.get(len(days), f"{len(days)} times a week")
    return f"{times} ({_and(days)} at {t})"


def next_scheduled_reboot(cfg, now=None):
    """Epoch seconds of the next SCHEDULED_REBOOT_DAYS reboot, or None."""
    days = reboot_days(cfg)
    try:
        hh, mm = (int(x) for x in (cfg.get("REBOOT_TIME") or "04:00").split(":")[:2])
    except ValueError:
        return None
    now = now or time.time()
    for i in range(8):
        t = time.localtime(now + i * 86400)
        if WEEKDAYS[t.tm_wday] not in days:
            continue
        ts = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hh, mm, 0, 0, 0, -1))
        if ts > now:
            return ts
    return None


def pending_shutdown():
    """Epoch seconds of a pending `shutdown -r` (security updates, or the scheduled one), or None."""
    try:
        with open("/run/systemd/shutdown/scheduled") as f:
            kv = dict(l.strip().split("=", 1) for l in f if "=" in l)
        if kv.get("MODE", "reboot") == "reboot" and kv.get("USEC"):
            return int(kv["USEC"]) / 1e6
    except (OSError, ValueError):
        pass
    return None


def reboot_info(cfg, now=None):
    """What to tell people about the next reboot: {at, why ('updates'|'scheduled'), postponed, latest, schedule}.
    `at` None = nothing planned. A scheduled reboot that is waiting for agents to finish their turn has
    postponed=True and `latest` = when it happens regardless."""
    now = now or time.time()
    out = {"at": None, "why": None, "postponed": False, "latest": None, "schedule": reboot_schedule_text(cfg)}
    st = read_json(REBOOT_STATE, {}) or {}
    pend = pending_shutdown()
    if pend and pend > now - 600:
        out.update(at=pend, why="scheduled" if st.get("state") == "rebooting" else "updates")
        return out
    try:
        latest = float(st.get("deadline") or 0)
    except (TypeError, ValueError):
        latest = 0
    if st.get("state") == "waiting" and latest > now:
        out.update(at=now, why="scheduled", postponed=True, latest=latest)
        return out
    nxt = next_scheduled_reboot(cfg, now)
    if nxt:
        out.update(at=nxt, why="scheduled")
    return out


# ---------- coding-agent processes ----------

AGENT_PROC_RE = re.compile(r"(?:^|/)(claude|codex|opencode)(?:\.m?js)?$|/claude-code/|/@openai/codex/|/@opencode/|/opencode-ai/")
_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def agent_processes(uids=None, pids=None):
    """{pid: (uid, agent, cpu_ticks)} for claude/codex/opencode processes, optionally only `uids` / `pids`.
    Reads /proc only; other users' entries are visible to root (and to themselves)."""
    out = {}
    for pid in (pids if pids is not None else os.listdir("/proc")):
        pid = str(pid)
        if not pid.isdigit():
            continue
        try:
            uid = os.stat(f"/proc/{pid}").st_uid
            if uids is not None and uid not in uids:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read(4096).split(b"\0")[:2]
            with open(f"/proc/{pid}/stat") as f:
                stat_f = f.read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        for a in argv:
            m = AGENT_PROC_RE.search(a.decode(errors="replace"))
            if m:
                out[int(pid)] = (uid, m.group(1) or "agent", int(stat_f[11]) + int(stat_f[12]))
                break
    return out


def _tree_ticks(root_pids):
    """CPU ticks of each root pid plus all its descendants (builds, tests, dev servers it started)."""
    kids, ticks = {}, {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                st = f.read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        kids.setdefault(int(st[1]), []).append(int(pid))
        ticks[int(pid)] = int(st[11]) + int(st[12])
    out = {}
    for r in root_pids:
        total, todo, seen = 0, [r], set()
        while todo:
            p = todo.pop()
            if p in seen:
                continue
            seen.add(p)
            total += ticks.get(p, 0)
            todo += kids.get(p, [])
        out[r] = total
    return out


def busy_agents(uids=None, pids=None, seconds=3.0, min_cpu_pct=1.0):
    """{pid: (uid, agent)} of agent processes that, with everything they started, used CPU during a short sample:
    an agent mid-turn (streaming, running a build or tests). One waiting for its user's next message uses ~none."""
    a = agent_processes(uids, pids)
    if not a:
        return {}
    t0 = _tree_ticks(list(a))
    time.sleep(seconds)
    t1 = _tree_ticks(list(a))
    need = min_cpu_pct / 100 * seconds * _CLK_TCK
    return {p: (v[0], v[1]) for p, v in a.items() if p in t1 and t1[p] - t0.get(p, 0) >= need}


# ---------- tailscale ----------

def tailscale_ip4():
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=5).stdout.split()
        return out[0] if out else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def tailscale_status():
    """`tailscale status --json` (peers included) as a dict, or None. Read-only; works without root."""
    try:
        r = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5)
        d = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return d if isinstance(d, dict) else None


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


# ---------- public front door (sharedbox-gate) ----------

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")  # secrets.token_urlsafe(32)
SESSION_COOKIE = "__Host-sbgate"


def public_url(cfg):
    """PUBLIC_URL without a trailing slash, or "" if the gateway is off (or misconfigured)."""
    u = (cfg.get("PUBLIC_URL") or "").strip().rstrip("/")
    return u if re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?", u) else ""


def public_mode(cfg):
    """"" (front door off), "funnel" or "cloudflare". Anything but PUBLIC_MODE=cloudflare is funnel, so
    configs from before PUBLIC_MODE existed keep working unchanged."""
    if not public_url(cfg):
        return ""
    return "cloudflare" if (cfg.get("PUBLIC_MODE") or "").strip().lower() == "cloudflare" else "funnel"


def ssh_host(cfg):
    """The hostname friends' laptops SSH to through Cloudflare, or "" when that isn't turned on."""
    h = (cfg.get("SSH_HOST") or "").strip().lower()
    if public_mode(cfg) != "cloudflare" or not cfg.get("CF_SSH_APP_ID") or h == "off":
        return ""
    return h if re.fullmatch(r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", h) else ""


def owner_emails(cfg):
    return sorted({e.strip().lower() for e in (cfg.get("CF_OWNER_EMAILS") or "").split(",") if e.strip()})


def owner_account(cfg):
    """The owner's own Linux account (OWNER_T3_USER, default the first ADMIN_USERS) as a pwd entry, else None.
    CF_OWNER_EMAILS act as this account on the public site (their T3 Code and their /_box/me page)."""
    name = (cfg.get("OWNER_T3_USER") or "").strip() or next(iter(admin_users(cfg)), "")
    try:
        return pwd.getpwnam(name) if name else None
    except KeyError:
        return None


def user_by_email(email, users=None):
    """(name, record) of the one active friend whose registry email is `email` (case-insensitive).
    Two accounts claiming one email is ambiguous: nobody gets in with it."""
    if not isinstance(email, str) or not email:
        return None, None
    email = email.strip().lower()
    users = friend_users() if users is None else users
    hits = [(n, r) for n, r in users.items() if str(r.get("email") or "").strip().lower() == email]
    if len(hits) != 1 or hits[0][1].get("status", "active") != "active":
        return None, None
    return hits[0]


def email_taken(email, reg=None):
    """Name of any account (active or not) already using `email`, else None."""
    email = (email or "").strip().lower()
    for n, r in (reg or registry()).get("users", {}).items():
        if email and str(r.get("email") or "").strip().lower() == email:
            return n
    return None


CONF_VALUE_RE = re.compile(r"[A-Za-z0-9 ._:/@,+%=-]*")


def set_config(updates, path=None):
    """Set KEY=VALUE lines in sharedbox.conf in place: comments, order and other keys are kept. The first
    KEY= line is rewritten (later duplicates dropped, since the last one would win); new keys are appended.
    Atomic, and the file keeps its mode and owner."""
    path = path or CONF_PATH
    for k, v in updates.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", k) or not CONF_VALUE_RE.fullmatch(str(v)):
            raise ValueError(f"refusing to write {k}: unexpected characters in the value")
    try:
        with open(path) as f:
            lines = f.read().splitlines()
        st = os.stat(path)
        mode, owner = st.st_mode & 0o7777, (st.st_uid, st.st_gid)
    except FileNotFoundError:
        lines, mode, owner = [], 0o644, None
    done, out = set(), []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            if key in done:
                continue
            out.append(f"{key}={updates[key]}")
            done.add(key)
        else:
            out.append(line)
    missing = [k for k in updates if k not in done]
    if missing:
        out += ["", "# set by sharedbox-admin"] + [f"{k}={updates[k]}" for k in missing]
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".tmp-conf-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fchmod(f.fileno(), mode)
            if owner and os.geteuid() == 0:
                os.fchown(f.fileno(), *owner)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def token_hash(tok):
    return hashlib.sha256(tok.encode()).hexdigest()


def gate_path(kind, h):
    assert kind in GATE_KINDS and re.fullmatch(r"[0-9a-f]{64}", h)
    return os.path.join(GATE_DIR, kind, h + ".json")


def gate_lookup(kind, tok):
    """(record, hash) for a live (unexpired) token of `kind`, else (None, hash-or-None).

    Records are stored under sha256(token), never the token itself. The lookup is by hash, so
    timing reveals nothing useful about the secret; the stored hash is still compared in constant time.
    """
    if not isinstance(tok, str) or not TOKEN_RE.fullmatch(tok):
        return None, None
    h = token_hash(tok)
    rec = read_json(gate_path(kind, h))
    if not isinstance(rec, dict) or not hmac.compare_digest(str(rec.get("hash", "")), h):
        return None, h
    try:
        if float(rec.get("expires", 0)) < time.time():
            return None, h
    except (TypeError, ValueError):
        return None, h
    return rec, h


def gate_records(kind):
    """[(hash, record)] for every stored record of `kind`, expired ones included."""
    out = []
    try:
        names = os.listdir(os.path.join(GATE_DIR, kind))
    except OSError:
        return out
    for n in names:
        h = n[:-5]
        if n.endswith(".json") and re.fullmatch(r"[0-9a-f]{64}", h):
            rec = read_json(os.path.join(GATE_DIR, kind, n))
            if isinstance(rec, dict):
                out.append((h, rec))
    return out


def active_friend(name, users=None):
    """The registry record of an existing, active friend account, else None."""
    users = friend_users() if users is None else users
    rec = users.get(name) if isinstance(name, str) else None
    return rec if rec and rec.get("status", "active") == "active" else None


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


EMAIL_RE = re.compile(r"[^@\s]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")


def validate_email(email):
    """Optional contact email (security alerts). Returns (normalized, error)."""
    email = (email or "").strip()
    if not email:
        return "", None
    if len(email) > 254 or not EMAIL_RE.fullmatch(email) or not email.isprintable():
        return None, "that email address doesn't look right (or leave it empty)"
    return email, None


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
