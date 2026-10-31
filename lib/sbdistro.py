"""Distro layer: the few things sharedbox does differently on Debian/Ubuntu (apt) and Arch/Omarchy (pacman).

Stdlib only, safe to import unprivileged (nothing runs at import time). Root tools call the
package functions; the audit and health report call the read-only ones.

Families: "debian" (ID/ID_LIKE debian or ubuntu), "arch" (arch, omarchy and Arch derivatives),
everything else is "unknown": read-only checks return "not supported" and package changes raise
Unsupported with a message the installer shows.

Arch rules this module keeps:
  - never `pacman -Sy` on its own (partial upgrades break Arch). Installs use the sync database as it
    is (`pacman -S --needed`); if it is too old to download from, we warn and skip. Refreshing it is
    the owner's job, as part of a full `pacman -Syu`.
  - no automatic upgrades. Pending updates and "reboot required" are reported instead.
  - AUR packages are never installed (that would mean building as root); they are skipped with a note.

    python3 -I sbdistro.py shell        DISTRO_FAMILY=... lines for install.sh (eval)
    python3 -I sbdistro.py preflight    detected distro, package manager, missing tools + install commands
"""

import glob
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

OS_RELEASE = "/etc/os-release"
DEBIAN_IDS = ("debian", "ubuntu")
ARCH_IDS = ("arch", "omarchy", "archarm", "manjaro", "endeavouros", "garuda", "cachyos")
PACMAN_SYNC = "/var/lib/pacman/sync"
PACMAN_LOCK = "/var/lib/pacman/db.lck"
PACMAN_LOG = "/var/log/pacman.log"
APT_HISTORY = "/var/log/apt/history.log"
SYNC_DB_OLD_DAYS = 14

# Debian package name -> Arch package name, for everything sharedbox-tools installs by default
# (TOOLS_APT, TOOLS_APT_BROWSER, Android JDK, smartmontools) plus common `box request`s and the
# installer's required tools. Names not listed are used as-is. Arch libraries ship their headers,
# so "-dev" packages map to the library itself.
DEB_TO_ARCH = {
    # TOOLS_APT
    "git": "git", "gh": "github-cli", "tmux": "tmux", "curl": "curl",
    "build-essential": "base-devel", "python3-venv": "python", "python3-pip": "python-pip",
    "pipx": "python-pipx", "ripgrep": "ripgrep", "fd-find": "fd", "jq": "jq", "unzip": "unzip",
    "zip": "zip", "sqlite3": "sqlite", "htop": "htop", "nodejs": "nodejs", "npm": "npm",
    "golang-go": "go", "golang": "go", "rustup": "rustup",
    # TOOLS_APT_BROWSER (atk and at-spi2-atk are part of at-spi2-core on Arch; libgbm is in mesa)
    "libnss3": "nss", "libatk1.0-0": "at-spi2-core", "libatk1.0-0t64": "at-spi2-core",
    "libatk-bridge2.0-0": "at-spi2-core", "libatk-bridge2.0-0t64": "at-spi2-core",
    "libatspi2.0-0": "at-spi2-core", "libatspi2.0-0t64": "at-spi2-core",
    "libxcomposite1": "libxcomposite", "libxdamage1": "libxdamage", "libxfixes3": "libxfixes",
    "libxrandr2": "libxrandr", "libasound2": "alsa-lib", "libasound2t64": "alsa-lib",
    "libgbm1": "mesa", "libxkbcommon0": "libxkbcommon", "libpango-1.0-0": "pango",
    "libcairo2": "cairo", "libcups2": "libcups", "libcups2t64": "libcups",
    "fonts-liberation": "ttf-liberation", "xvfb": "xorg-server-xvfb", "libdrm2": "libdrm",
    # TOOLS_ANDROID, health report
    "openjdk-21-jdk-headless": "jdk21-openjdk", "openjdk-21-jdk": "jdk21-openjdk",
    "openjdk-17-jdk-headless": "jdk17-openjdk", "default-jdk": "jdk-openjdk",
    "default-jdk-headless": "jdk-openjdk", "smartmontools": "smartmontools",
    # common requests
    "python3": "python", "python3-dev": "python", "pkg-config": "pkgconf", "libssl-dev": "openssl",
    "libffi-dev": "libffi", "zlib1g-dev": "zlib", "libsqlite3-dev": "sqlite", "cmake": "cmake",
    "ffmpeg": "ffmpeg", "imagemagick": "imagemagick", "postgresql-client": "postgresql-libs",
    "redis-tools": "valkey", "dnsutils": "bind", "netcat-openbsd": "openbsd-netcat",
    "chromium-browser": "chromium", "golang-1.22": "go",
    # installer requirements
    "openssh-client": "openssh", "openssh-server": "openssh", "iproute2": "iproute2",
    "procps": "procps-ng", "passwd": "shadow", "util-linux": "util-linux", "coreutils": "coreutils",
    "systemd": "systemd", "tailscale": "tailscale", "pacman-contrib": "pacman-contrib",
}

# binaries install.sh needs -> package that provides them, per family
TOOL_PACKAGES = {
    "setpriv": {"debian": "util-linux", "arch": "util-linux"},
    "ssh-keygen": {"debian": "openssh-client", "arch": "openssh"},
    "sshd": {"debian": "openssh-server", "arch": "openssh"},
    "useradd": {"debian": "passwd", "arch": "shadow"},
    "groupadd": {"debian": "passwd", "arch": "shadow"},
    "usermod": {"debian": "passwd", "arch": "shadow"},
    "gpasswd": {"debian": "passwd", "arch": "shadow"},
    "loginctl": {"debian": "systemd", "arch": "systemd"},
    "du": {"debian": "coreutils", "arch": "coreutils"},
    "ss": {"debian": "iproute2", "arch": "iproute2"},
    "ip": {"debian": "iproute2", "arch": "iproute2"},
    "pkill": {"debian": "procps", "arch": "procps-ng"},
    "flock": {"debian": "util-linux", "arch": "util-linux"},
    "ionice": {"debian": "util-linux", "arch": "util-linux"},
    "tailscale": {"debian": "tailscale", "arch": "tailscale"},
    "smartctl": {"debian": "smartmontools", "arch": "smartmontools"},
    "checkupdates": {"arch": "pacman-contrib"},
}
REQUIRED_TOOLS = ("setpriv", "ssh-keygen", "useradd", "groupadd", "usermod", "gpasswd", "loginctl",
                  "du", "ss", "ip", "pkill", "sshd")
OPTIONAL_TOOLS = {"tailscale": "signup identity and the default WEB_BIND",
                  "smartctl": "disk health in the weekly report",
                  "checkupdates": "pending-update count in the weekly report (Arch has no auto-updates)",
                  "ionice": "lower IO priority for `box heavy`"}


class Unsupported(Exception):
    pass


# ---------------------------------------------------------------- detection

def os_release(path=OS_RELEASE):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                k, sep, v = line.strip().partition("=")
                if sep and re.fullmatch(r"[A-Z_][A-Z0-9_]*", k):
                    try:
                        v = shlex.split(v)[0] if v else ""
                    except (ValueError, IndexError):
                        pass
                    out[k] = v
    except OSError:
        pass
    return out


def family(osr=None):
    """'debian' | 'arch' | 'unknown', from ID first, then ID_LIKE."""
    osr = os_release() if osr is None else osr
    ids = [osr.get("ID", "").lower()] + osr.get("ID_LIKE", "").lower().split()
    for i in ids:
        if i in DEBIAN_IDS:
            return "debian"
        if i in ARCH_IDS:
            return "arch"
    return "unknown"


_FAMILY = None


def current():
    global _FAMILY
    if _FAMILY is None:
        _FAMILY = family()
    return _FAMILY


def pretty_name(osr=None):
    osr = os_release() if osr is None else osr
    return osr.get("PRETTY_NAME") or osr.get("NAME") or osr.get("ID") or "unknown Linux"


def package_manager(fam=None):
    return {"debian": "apt-get", "arch": "pacman"}.get(fam or current())


def _unsupported(fam, what):
    osr = os_release()
    return Unsupported(f"don't know how to {what} on {pretty_name(osr)} (ID={osr.get('ID', '?')}, "
                       f"ID_LIKE={osr.get('ID_LIKE', '')}). Supported: Debian/Ubuntu (apt) and "
                       "Arch/Omarchy (pacman). Install packages yourself with your package manager.")


# ---------------------------------------------------------------- names

def native_name(name, fam=None):
    """Translate a Debian package name (what TOOLS_APT and old `apt NAME` lines use) for this distro."""
    if (fam or current()) == "arch":
        return DEB_TO_ARCH.get(name, name)
    return name


def native_names(names, fam=None):
    out = []
    for n in names:
        n = native_name(n, fam)
        if n and n not in out:
            out.append(n)
    return out


def install_command(pkgs, fam=None):
    """Human-readable install command (for messages), or None on an unknown distro."""
    fam = fam or current()
    pkgs = native_names(pkgs, fam)
    if fam == "debian":
        return "sudo apt-get install --no-install-recommends " + " ".join(pkgs)
    if fam == "arch":
        return "sudo pacman -S --needed " + " ".join(pkgs)
    return None


# ---------------------------------------------------------------- package operations (root)

def _run(cmd, dry, env=None, check=True):
    if dry:
        print("    would run: " + " ".join(cmd))
        return 0
    return subprocess.run(cmd, check=check, env=env).returncode


def sync_db_age_days():
    """Age of the oldest pacman sync database in days, or None if there are none."""
    dbs = glob.glob(os.path.join(PACMAN_SYNC, "*.db"))
    if not dbs:
        return None
    return (time.time() - min(os.path.getmtime(p) for p in dbs)) / 86400


def available(pkgs, fam=None):
    """The subset of pkgs (native names) the package manager knows; prints why others are skipped."""
    fam = fam or current()
    ok = []
    if fam == "debian":
        for p in pkgs:
            r = subprocess.run(["apt-cache", "show", "--no-all-versions", p],
                               capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                ok.append(p)
            else:
                print(f"    skipping {p}: not available in apt", file=sys.stderr)
        return ok
    if fam == "arch":
        if not shutil.which("pacman"):
            raise _unsupported(fam, "look up packages (pacman not found)")
        for p in pkgs:
            r = subprocess.run(["pacman", "-Si", "--", p], capture_output=True, text=True)
            if r.returncode != 0:   # a group (e.g. xorg) is fine too
                r = subprocess.run(["pacman", "-Sg", "--", p], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                ok.append(p)
            else:
                print(f"    skipping {p}: not in the pacman repos (AUR packages are not installed by "
                      "sharedbox; an admin can build one with yay as their own user)", file=sys.stderr)
        return ok
    raise _unsupported(fam, "look up packages")


def install(pkgs, dry=False, fam=None):
    """Install packages by native name. Never upgrades the whole system, never refreshes the
    pacman sync database (no -Sy). Raises Unsupported on an unknown distro."""
    fam = fam or current()
    if fam == "debian":
        pkgs = available(pkgs, fam)
        if not pkgs:
            return
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive", NEEDRESTART_MODE="l")
        _run(["apt-get", "install", "-y", "--no-install-recommends",
              "-o", "Dpkg::Options::=--force-confold"] + pkgs, dry, env=env)
        return
    if fam != "arch":
        raise _unsupported(fam, "install packages")
    age = sync_db_age_days()
    if age is None:
        print("    warning: pacman has no sync database yet, so nothing can be installed. sharedbox "
              "never runs `pacman -Sy` by itself (partial upgrades break Arch): run a full "
              "`sudo pacman -Syu`, then retry", file=sys.stderr)
        return
    if age > SYNC_DB_OLD_DAYS:
        print(f"    warning: the pacman sync database is {age:.0f} days old; mirrors may no longer have "
              "those versions. If downloads fail, run a full `sudo pacman -Syu` (sharedbox never runs "
              "-Sy alone), then retry", file=sys.stderr)
    pkgs = available(pkgs, fam)
    if not pkgs:
        return
    if os.path.exists(PACMAN_LOCK) and not dry:
        print(f"    warning: pacman is busy ({PACMAN_LOCK} exists); skipping {' '.join(pkgs)}. "
              "Retry when the other pacman/yay run is done", file=sys.stderr)
        return
    base = ["pacman", "-S", "--needed", "--noconfirm"]
    if _run(base + ["--"] + pkgs, dry, check=False) == 0 or dry:
        return
    # one conflict or a stale mirror fails the whole transaction; retry one by one
    failed = [p for p in pkgs if _run(base + ["--", p], False, check=False) != 0]
    if failed:
        print(f"    warning: pacman could not install: {' '.join(failed)}. A conflict (answer it by "
              f"hand: sudo pacman -S {' '.join(failed)}) or a stale sync database (run a full "
              "`sudo pacman -Syu` first) are the usual causes", file=sys.stderr)


def remove(pkgs, fam=None):
    fam = fam or current()
    if fam == "debian":
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive", NEEDRESTART_MODE="l")
        return _run(["apt-get", "remove", "-y"] + list(pkgs), False, env=env, check=False)
    if fam == "arch":
        return _run(["pacman", "-R", "--noconfirm", "--"] + list(pkgs), False, check=False)
    raise _unsupported(fam, "remove packages")


# ---------------------------------------------------------------- read-only checks (audit, health)

def _capture(cmd, timeout=120, stderr=False):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + (r.stderr if stderr else "")
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""


def parse_dpkg_verify(out):
    """Paths whose content (md5) differs from the package; conffiles excluded."""
    bad = []
    for line in out.splitlines():
        m = re.match(r"^(\S{9})\s+(c?)\s*(/\S.*)$", line)
        if not m or m.group(2) == "c" or "5" not in m.group(1):
            continue
        bad.append(m.group(3))
    return bad


def parse_pacman_qkk(out):
    """[(path, pkg)] whose checksum differs from the package. pacman skips backup (config) files'
    checksums, like dpkg skips conffiles."""
    bad = []
    for line in out.splitlines():
        m = re.match(r"^warning: (\S+): (/.*?) \((?:SHA256|MD5) checksum mismatch\)$", line.strip())
        if m:
            bad.append((m.group(2), m.group(1)))
    return bad


def verify_packages(fam=None, timeout=900):
    """[(path, pkg_or_None)] of system files whose content differs from their package, or None if
    this distro can't check."""
    fam = fam or current()
    if fam == "debian":
        rc, out = _capture(["dpkg", "--verify"], timeout=timeout)
        return [(p, None) for p in parse_dpkg_verify(out)]
    if fam == "arch":
        rc, out = _capture(["pacman", "-Qkk"], timeout=timeout, stderr=True)   # warnings go to stderr
        return parse_pacman_qkk(out)
    return None


def parse_checkupdates(out):
    """'name 1.0-1 -> 1.1-1' lines -> [name]"""
    return [m.group(1) for m in re.finditer(r"^(\S+) \S+ -> \S+", out, re.M)]


def pending_updates(fam=None, timeout=180):
    """{'supported': bool, 'security': [...] or None, 'all': [...] or None, 'error': str or None}.
    Debian: security updates from a simulated upgrade. Arch: every pending update via checkupdates
    (pacman-contrib), which syncs a private copy of the database and never touches the real one."""
    fam = fam or current()
    if fam == "debian":
        rc, out = _capture(["apt-get", "-s", "-o", "Debug::NoLocking=1", "upgrade"], timeout=timeout)
        sec = sorted({m.group(1) for m in re.finditer(r"^Inst (\S+) .*-security", out, re.M)})
        return {"supported": True, "security": sec, "all": None, "error": None}
    if fam == "arch":
        if not shutil.which("checkupdates"):
            return {"supported": False, "security": None, "all": None,
                    "error": "checkupdates not installed (pacman -S --needed pacman-contrib)"}
        rc, out = _capture(["checkupdates"], timeout=timeout)
        if rc == 2 or (rc == 0 and not out.strip()):     # 2 = no updates
            return {"supported": True, "security": None, "all": [], "error": None}
        if rc != 0:
            return {"supported": True, "security": None, "all": None,
                    "error": f"checkupdates failed (exit {rc}); offline or mirror problem?"}
        return {"supported": True, "security": None, "all": sorted(set(parse_checkupdates(out))),
                "error": None}
    return {"supported": False, "security": None, "all": None, "error": f"unsupported distro ({fam})"}


def reboot_required(fam=None, release=None, modules_dir="/usr/lib/modules"):
    """Days since a reboot became necessary (0.0 if unknown), or None if no reboot is needed.
    Debian: /var/run/reboot-required. Arch: the running kernel's module directory is gone, which
    is what a kernel upgrade does (pacman removes the old /usr/lib/modules/<release>)."""
    fam = fam or current()
    if fam == "debian":
        if os.path.exists("/var/run/reboot-required"):
            return (time.time() - os.path.getmtime("/var/run/reboot-required")) / 86400
        return None
    if fam == "arch":
        release = release or os.uname().release
        if os.path.isdir(os.path.join(modules_dir, release)):
            return None
        try:
            newest = max(os.path.getmtime(p) for p in glob.glob(os.path.join(modules_dir, "*")))
            return max(0.0, (time.time() - newest) / 86400)
        except ValueError:   # no module dirs at all: not a normal Arch kernel setup
            return None
    return None


def parse_apt_history(text, since):
    ups, cur_ok = [], False
    for l in text.splitlines():
        if l.startswith("Start-Date:"):
            try:
                cur_ok = time.mktime(time.strptime(l.split(": ", 1)[1].strip(), "%Y-%m-%d  %H:%M:%S")) >= since
            except ValueError:
                cur_ok = False
        elif cur_ok and l.startswith(("Upgrade:", "Install:")):
            ups += re.findall(r"(\S+?):\S+ \(", l)
    return sorted(set(ups))


def _pacman_ts(s):
    from datetime import datetime
    try:
        d = datetime.fromisoformat(s)              # 2024-05-01T10:22:33+0200
        return d.timestamp()
    except ValueError:
        pass
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M"))   # pacman < 5.2
    except ValueError:
        return None


def parse_pacman_log(text, since):
    ups = set()
    for m in re.finditer(r"^\[([^\]]+)\] \[ALPM\] (?:upgraded|installed) (\S+) \(", text, re.M):
        ts = _pacman_ts(m.group(1))
        if ts is not None and ts >= since:
            ups.add(m.group(2))
    return sorted(ups)


def package_history(since, fam=None):
    """Packages installed or upgraded since `since` (epoch), sorted."""
    fam = "arch" if (fam or current()) == "arch" else "debian"   # unknown: apt's log, as before
    path = PACMAN_LOG if fam == "arch" else APT_HISTORY
    try:
        with open(path, errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    return parse_apt_history(text, since) if fam == "debian" else parse_pacman_log(text, since)


def auto_updates(fam=None):
    """{'enabled': True/False/None, 'how': text, 'fix': text}. None = not applicable here."""
    fam = fam or current()
    if fam == "debian":
        try:
            with open("/etc/apt/apt.conf.d/20auto-upgrades") as f:
                uu = f.read()
        except OSError:
            uu = ""
        on = 'Unattended-Upgrade "1"' in uu
        return {"enabled": on, "how": "unattended-upgrades",
                "fix": "" if on else "dpkg-reconfigure -plow unattended-upgrades"}
    if fam == "arch":
        how = "omarchy-update (as your own user)" if shutil.which("omarchy-update") else "sudo pacman -Syu"
        return {"enabled": None, "how": "manual on Arch (sharedbox does not auto-upgrade)",
                "fix": f"read https://archlinux.org/news, then run {how}; reboot if the kernel changed"}
    return {"enabled": None, "how": "unknown", "fix": ""}


def sshd_unit():
    """'ssh' on Debian/Ubuntu, 'sshd' on Arch (and most others)."""
    for u in ("ssh", "sshd"):
        if os.path.exists(f"/usr/lib/systemd/system/{u}.service") or \
                os.path.exists(f"/lib/systemd/system/{u}.service"):
            return u
    return "ssh" if current() == "debian" else "sshd"


def sshd_reads_dropins(config="/etc/ssh/sshd_config"):
    """True if sshd_config Includes sshd_config.d (Debian/Ubuntu do; Arch's default may not)."""
    try:
        with open(config) as f:
            txt = f.read()
    except OSError:
        return None
    return bool(re.search(r"^\s*Include\s+\S*sshd_config\.d/", txt, re.M | re.I))


# ---------------------------------------------------------------- CLI for install.sh

def _cli_shell():
    osr = os_release()
    fam = family(osr)
    print(f"DISTRO_FAMILY={shlex.quote(fam)}")
    print(f"DISTRO_NAME={shlex.quote(pretty_name(osr))}")
    print(f"PKG_MGR={shlex.quote(package_manager(fam) or '')}")


def _cli_preflight():
    osr = os_release()
    fam = family(osr)
    pm = {"debian": "apt-get (install only, never a full upgrade)",
          "arch": "pacman -S --needed (never -Sy alone; no AUR, no automatic upgrades)"}.get(
        fam, "none: package installs will fail with an error; install them yourself")
    print(f"    distro: {pretty_name(osr)} (family: {fam})")
    print(f"    package manager: {pm}")
    missing = [t for t in REQUIRED_TOOLS if not shutil.which(t)]
    optional = [t for t in OPTIONAL_TOOLS
                if (t != "checkupdates" or fam == "arch") and not shutil.which(t)]
    for label, tools in (("missing required tool", missing), ("missing optional tool", optional)):
        for t in tools:
            pkg = TOOL_PACKAGES.get(t, {}).get(fam)
            cmd = install_command([pkg], fam) if pkg else None
            why = f" ({OPTIONAL_TOOLS[t]})" if t in OPTIONAL_TOOLS else ""
            print(f"    {label}: {t}{why}" + (f"   install: {cmd}" if cmd else ""))
    if fam == "arch":
        age = sync_db_age_days()
        if age is None:
            print("    pacman sync database missing: run a full `sudo pacman -Syu` before installing tools")
        elif age > SYNC_DB_OLD_DAYS:
            print(f"    pacman sync database is {age:.0f} days old: consider a full `sudo pacman -Syu` first")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "preflight"
    if cmd == "shell":
        _cli_shell()
    elif cmd == "preflight":
        _cli_preflight()
    else:
        sys.exit(f"usage: {sys.argv[0]} shell|preflight")
