"""Security self-audit: look at the box the way an attacker (or a compromised friend account) would.

Run as root by `sharedbox-health audit` and by the weekly report. Read-only: it never changes
the system. Each finding has a stable id so the report can say what is new since last time.

Areas: network exposure, sshd, privileged groups/sudo, home isolation, SSH key and persistence
changes, setuid inventory, world-writable system paths, package integrity (dpkg --verify / pacman -Qkk),
live compromise indicators (processes from /tmp or memfd, reverse shells, miners), kernel
hardening, pending (security) updates and reboot-required, tripwire (canary) hits.
Distro differences (Debian/Ubuntu vs Arch/Omarchy) live in sbdistro.
"""

import glob
import grp
import hashlib
import ipaddress
import json
import os
import pwd
import re
import stat
import subprocess
import time

import sbcommon as sb
import sbdistro as distro

AUDIT_DIR = os.path.join(sb.STATE_DIR, "audit")
LAST = os.path.join(AUDIT_DIR, "last.json")
BASELINE = os.path.join(AUDIT_DIR, "baseline.json")
CANARY_LOG = os.path.join(sb.STATE_DIR, "canary.jsonl")

SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
TAILNET4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
PRIV_GROUPS = ("sudo", "admin", "wheel", "docker", "lxd", "incus-admin", "adm", "disk", "shadow",
               "libvirt", "systemd-journal", "log")   # log: Arch's adm
MINERS = re.compile(r"xmrig|kdevtmpfsi|kinsing|minerd|cpuminer|nbminer|t-rex|lolminer|phoenixminer"
                    r"|xmr-stak|ccminer|ethminer|\bsrbminer", re.I)
SUSPICIOUS_PORTS = {1080, 3333, 4444, 5555, 6666, 6667, 6697, 7777, 9050, 14444, 14433, 45700}
WATCHED = ["/etc/sudoers", "/etc/sudoers.d/*", "/etc/ld.so.preload", "/etc/pam.d/*",
           "/etc/ssh/sshd_config", "/etc/ssh/sshd_config.d/*", "/etc/crontab", "/etc/cron.d/*",
           "/etc/cron.daily/*", "/etc/cron.hourly/*", "/etc/systemd/system/*.service",
           "/etc/systemd/system/*.timer", "/etc/profile.d/*", "/etc/environment",
           "/etc/security/*.conf", "/root/.bashrc", "/root/.profile", "/etc/rc.local",
           "/var/spool/cron/crontabs/*", "/etc/apt/sources.list.d/*", "/etc/apt/apt.conf.d/*"]
# Arch: package sources/hooks instead of /etc/apt, cronie's spool instead of crontabs/
WATCHED_ARCH = ["/etc/pacman.conf", "/etc/pacman.d/*", "/etc/pacman.d/hooks/*", "/usr/share/libalpm/hooks/*",
                "/var/spool/cron/*", "/etc/makepkg.conf"]
UPDATES_CACHE_H = 6   # checkupdates (Arch) downloads the package databases; the hourly check reuses this


def run(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return None


class Audit:
    def __init__(self, cfg):
        self.cfg = cfg
        self.findings = []
        self.facts = {}           # non-finding info for the report (public services, logins...)
        self.baseline = sb.read_json(BASELINE, {}) or {}
        self.new_baseline = {}

    def add(self, fid, sev, title, detail="", fix=""):
        if any(f["id"] == fid for f in self.findings):   # v4 + v6 listeners etc.
            return
        self.findings.append({"id": fid, "sev": sev, "title": title, "detail": detail, "fix": fix})

    # ------------------------------------------------------------------ network
    def firewall_active(self):
        """Name of the active host firewall, or None. Works without any of these installed:
        ufw (Ubuntu, Omarchy), nftables, iptables (nft or legacy backend), firewalld."""
        rc, out = run(["ufw", "status"])
        if rc == 0 and out.startswith("Status: active"):
            return "ufw"
        rc, out = run(["nft", "list", "ruleset"])
        if rc == 0 and re.search(r"hook input .*policy drop", out):
            return "nftables"
        rc, out = run(["iptables", "-S", "INPUT"])   # iptables-nft without nft(8), or iptables-legacy
        if rc == 0 and re.search(r"^-P INPUT DROP", out, re.M):
            return "iptables"
        rc, out = run(["firewall-cmd", "--state"], timeout=10)
        if rc == 0 and out.strip() == "running":
            return "firewalld"
        return None

    def check_exposure(self):
        fw = self.firewall_active()
        self.facts["firewall"] = fw or "off"
        rc, out = run(["ss", "-Htulnp"])
        exposed = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            proto, local = parts[0], parts[4]
            proc = re.search(r'\(\("([^"]+)"', line)
            proc = proc.group(1) if proc else "?"
            host, _, port = local.rpartition(":")
            host = host.strip("[]")
            iface = ""
            if "%" in host:
                host, iface = host.split("%", 1)
            if iface in ("lo", "tailscale0"):
                continue
            if host in ("*", "0.0.0.0", "::"):
                where = "all interfaces"
            else:
                try:
                    ip = ipaddress.ip_address(host)
                except ValueError:
                    continue
                if ip.is_loopback or ip in TAILNET4 or ip in TAILNET6 or ip.is_link_local:
                    continue
                where = host
            if proto == "udp" and port in ("68", "546", "41641", "5353"):   # dhcp, tailscale, mdns
                continue
            exposed.append((proto, port, proc, where))
        self.facts["exposed_ports"] = [f"{p}/{port} {proc} on {w}" for p, port, proc, w in exposed]
        for proto, port, proc, where in exposed:
            if proc in ("sshd", "sshd-session") or (port == "22" and proc == "?"):
                continue
            sev = "info" if fw else "medium"
            self.add(f"exposed:{proto}/{port}", sev,
                     f"{proc} listens on {proto}/{port} on {where} (not just Tailscale)",
                     "Anyone on the LAN can reach it now; anyone on the internet could if a router port "
                     "is ever forwarded." + (f" Firewall ({fw}) is on, so check it blocks this port."
                                             if fw else " The firewall is OFF."),
                     "bind it to 127.0.0.1 or the Tailscale IP, or turn on the firewall "
                     "(see README: Firewall)")
        if not fw:
            self.add("firewall:off", "medium" if exposed else "low",
                     "No host firewall (ufw/nftables/iptables input policy is accept)",
                     "Nothing stops a new server from being reachable from the LAN/internet.",
                     "README: Firewall (allows Tailscale + LAN ssh, denies the rest)")

        # public internet via Tailscale Funnel
        rc, out = run(["tailscale", "funnel", "status", "--json"], timeout=10)
        public = []
        try:
            d = json.loads(out) if rc == 0 and out.strip() else {}
        except json.JSONDecodeError:
            d = {}
        for hostport, on in (d.get("AllowFunnel") or {}).items():
            if on:
                handlers = (d.get("Web") or {}).get(hostport, {}).get("Handlers", {})
                tgt = ", ".join(f"{p} -> {h.get('Proxy') or h.get('Path') or h.get('Text', '')}"
                                for p, h in handlers.items())
                public.append(f"https://{hostport} {tgt}".strip())
        self.facts["public"] = public
        prev = set(self.baseline.get("public", []))
        for p in public:
            if p not in prev and "public" in self.baseline:
                self.add(f"public-new:{p}", "medium", f"Newly public on the internet: {p}",
                         "Tailscale Funnel started exposing this since the last audit.",
                         "if unexpected: tailscale funnel reset")
        self.new_baseline["public"] = public

    # ------------------------------------------------------------------ ssh
    def check_sshd(self):
        rc, out = run(["sshd", "-T"])
        if rc != 0:
            return
        conf = {}
        for line in out.splitlines():
            k, _, v = line.partition(" ")
            conf.setdefault(k, v)
        if conf.get("passwordauthentication") == "yes":
            self.add("sshd:password", "high", "sshd accepts passwords", "Password guessing is possible.",
                     "PasswordAuthentication no")
        if conf.get("kbdinteractiveauthentication") == "yes":
            self.add("sshd:kbd", "medium", "sshd allows keyboard-interactive auth", "",
                     "KbdInteractiveAuthentication no")
        if conf.get("permitrootlogin") not in ("no", "prohibit-password", "without-password"):
            self.add("sshd:root", "high", f"sshd PermitRootLogin={conf.get('permitrootlogin')}", "",
                     "PermitRootLogin no")
        elif conf.get("permitrootlogin") != "no":
            self.add("sshd:rootkey", "low", "root can log in over ssh with a key", "",
                     "PermitRootLogin no")
        if not conf.get("allowgroups") and not conf.get("allowusers"):
            self.add("sshd:allow", "medium", "sshd has no AllowGroups/AllowUsers",
                     "Every account with a key can log in, including service accounts.", "")
        if conf.get("x11forwarding") == "yes":
            self.add("sshd:x11", "low", "sshd X11Forwarding yes", "", "X11Forwarding no")
        if conf.get("permittunnel", "no") != "no":
            self.add("sshd:tun", "low", "sshd PermitTunnel enabled", "", "PermitTunnel no")
        if int(conf.get("maxauthtries", "6")) > 4:
            self.add("sshd:tries", "low", f"sshd MaxAuthTries {conf.get('maxauthtries')}", "",
                     "MaxAuthTries 3")

    # ------------------------------------------------------------------ accounts
    def check_accounts(self):
        admins = set(sb.admin_users(self.cfg)) | {"root"}
        friends = set()
        try:
            friends = set(grp.getgrnam(self.cfg["FRIENDS_GROUP"]).gr_mem)
        except KeyError:
            pass
        for pw in pwd.getpwall():
            if pw.pw_gid == _gid(self.cfg["FRIENDS_GROUP"]):
                friends.add(pw.pw_name)
            if pw.pw_uid == 0 and pw.pw_name != "root":
                self.add(f"uid0:{pw.pw_name}", "high", f"account {pw.pw_name} has uid 0 (a second root)", "",
                         f"userdel {pw.pw_name} after investigating")
        for g in PRIV_GROUPS:
            try:
                members = set(grp.getgrnam(g).gr_mem)
            except KeyError:
                continue
            for m in members:
                if m in friends:
                    self.add(f"privgroup:{g}:{m}", "high", f"friend {m} is in privileged group {g}",
                             f"Membership in {g} is root-equivalent or exposes logs/secrets.",
                             f"gpasswd -d {m} {g}")
                elif g in ("sudo", "admin", "wheel", "docker", "lxd", "incus-admin", "disk") and m not in admins:
                    self.add(f"privgroup:{g}:{m}", "medium",
                             f"{m} is in {g} (root-equivalent) but not in ADMIN_USERS", "",
                             "add to ADMIN_USERS if intended, else remove from the group")
        shadow = read("/etc/shadow")
        for line in (shadow or "").splitlines():
            f = line.split(":")
            if len(f) > 1 and f[1] == "":
                self.add(f"emptypw:{f[0]}", "high", f"account {f[0]} has an EMPTY password", "",
                         f"passwd -l {f[0]}")
        for path in ["/etc/sudoers"] + glob.glob("/etc/sudoers.d/*"):
            for line in (read(path) or "").splitlines():
                s = line.strip()
                if s and not s.startswith("#") and "NOPASSWD" in s:
                    self.add(f"nopasswd:{path}:{sha_str(s)}", "medium",
                             f"passwordless sudo rule in {path}", s,
                             "anyone who gets that account gets root without a password")
        self.facts["friends"] = sorted(friends)

    def check_homes(self):
        for pw in pwd.getpwall():
            h = pw.pw_dir
            if not h.startswith("/home/") or not os.path.isdir(h):
                continue
            try:
                st = os.stat(h)
            except OSError:
                continue
            if st.st_mode & 0o007:
                self.add(f"home:{pw.pw_name}", "medium",
                         f"/home/{pw.pw_name} is accessible to other users (mode {oct(st.st_mode & 0o777)})",
                         "Other friends (or an attacker in any account) can read files there.",
                         f"chmod 750 {h}")
            ssh = os.path.join(h, ".ssh")
            for p in (ssh, os.path.join(ssh, "authorized_keys")):
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if st.st_mode & 0o022 or st.st_uid not in (0, pw.pw_uid):
                    self.add(f"sshperm:{p}", "high", f"{p} is writable by someone other than its owner",
                             "Another user could add their key and log in as this account.",
                             f"chown {pw.pw_name} {p}; chmod go-w {p}")

    def check_keys(self):
        keys = {}
        for pw in pwd.getpwall():
            for p in (os.path.join(pw.pw_dir, ".ssh", "authorized_keys"),
                      os.path.join(pw.pw_dir, ".ssh", "authorized_keys2")):
                txt = read(p)
                if txt is None:
                    continue
                lines = sorted(l.strip() for l in txt.splitlines() if l.strip() and not l.startswith("#"))
                keys[p] = [l.split()[-2][-12:] if len(l.split()) >= 2 else l[:12] for l in lines]
                for l in lines:
                    if re.match(r"\S+=", l) and pw.pw_uid == 0:
                        self.add(f"rootkeyopt:{sha_str(l)}", "low", "root authorized_keys has key options", l[:120])
        prev = self.baseline.get("keys")
        if prev is not None:
            for p, ks in keys.items():
                added = set(ks) - set(prev.get(p, []))
                if added:
                    self.add(f"newkey:{p}:{','.join(sorted(added))}", "medium",
                             f"new SSH key(s) in {p} since last audit",
                             "Expected if this user just joined or added a laptop. Otherwise this is "
                             "how attackers keep access.", f"review {p}")
        self.new_baseline["keys"] = keys

    # ------------------------------------------------------------------ persistence / tampering
    def check_watched(self):
        hashes = {}
        for pat in WATCHED + (WATCHED_ARCH if distro.current() == "arch" else []):
            for p in glob.glob(pat):
                if os.path.isfile(p):
                    hashes[p] = sha(p)
        prev = self.baseline.get("watched")
        if prev is not None:
            changed = sorted(p for p in hashes if prev.get(p) != hashes[p])
            gone = sorted(p for p in prev if p not in hashes)
            for p in changed:
                kind = "new" if p not in prev else "changed"
                self.add(f"watched:{p}:{hashes[p]}", "medium", f"{kind} since last audit: {p}",
                         "Sensitive file (login, sudo, cron, boot-time services). Fine if you or an "
                         "update did it; otherwise investigate.", "")
            for p in gone:
                self.add(f"watched-gone:{p}", "low", f"removed since last audit: {p}")
        if os.path.exists("/etc/ld.so.preload") and (read("/etc/ld.so.preload") or "").strip():
            self.add("ldpreload", "high", "/etc/ld.so.preload is set (classic rootkit hook)",
                     read("/etc/ld.so.preload")[:200])
        self.new_baseline["watched"] = hashes

    def check_setuid(self):
        rc, out = run(["find", "/", "-xdev", "(", "-path", "/proc", "-o", "-path", "/sys", "-o",
                       "-path", "/home/*/.local/share/docker", "-o", "-path", "/var/lib/docker", "-o",
                       "-path", "/snap", ")", "-prune", "-o", "-type", "f", "(", "-perm", "-4000", "-o",
                       "-perm", "-2000", ")", "-printf", "%U %m %p\\n"], timeout=600)
        found = {}
        for line in out.splitlines():
            uid, mode, path = line.split(" ", 2)
            found[path] = f"{uid}:{mode}"
            if uid == "0" and re.match(r"/(home|tmp|var/tmp|dev/shm|srv|opt)/", path):
                self.add(f"suidhome:{path}", "high", f"root-owned setuid/setgid file in a user-writable area: {path}",
                         "This is a planted privilege escalation backdoor unless you made it.", f"rm {path}")
        prev = self.baseline.get("setuid")
        if prev is not None:
            for p in sorted(set(found) - set(prev)):
                self.add(f"suidnew:{p}:{found[p]}", "medium", f"new setuid/setgid binary: {p}",
                         "Fine after installing a package that ships one; otherwise suspicious.", "")
        self.new_baseline["setuid"] = found

    def check_writable(self):
        hits = []
        for top in ("/etc", "/usr", "/opt", "/boot", sb.LIB_DIR, "/var/lib/sharedbox", "/srv"):
            for root, dirs, files in os.walk(top):
                if root.startswith(("/usr/src", "/var/lib/sharedbox/requests")):
                    dirs[:] = []
                    continue
                for n in dirs + files:
                    p = os.path.join(root, n)
                    try:
                        st = os.lstat(p)
                    except OSError:
                        continue
                    if stat.S_ISLNK(st.st_mode):
                        continue
                    if st.st_mode & 0o002 and not (stat.S_ISDIR(st.st_mode) and st.st_mode & 0o1000):
                        hits.append(p)
                if len(hits) > 50:
                    break
        for p in hits[:20]:
            self.add(f"worldw:{p}", "high", f"world-writable system path: {p}",
                     "Any user can modify it; if root ever runs or reads it, that is root for everyone.",
                     f"chmod o-w {p}")

    def check_dpkg(self):
        rc, out = run(["dpkg", "--verify"], timeout=900)
        bad = distro.parse_dpkg_verify(out)
        for p in bad[:25]:
            sev = "high" if re.match(r"/(usr/)?s?bin/|/usr/lib/x86_64-linux-gnu/security/|/lib/", p) else "low"
            self.add(f"dpkg:{p}", sev, f"system file differs from its package: {p}",
                     "A tampered binary or library is how rootkits hide. (Low severity for data files.)",
                     f"apt reinstall $(dpkg -S {p} | cut -d: -f1)")

    def check_pacman(self):
        bad = distro.verify_packages("arch") or []
        for p, pkg in bad[:25]:
            sev = "high" if re.match(r"/(usr/)?s?bin/|/usr/lib/security/|/(usr/)?lib(64)?/[^/]+\.so", p) else "low"
            self.add(f"pkgfile:{p}", sev, f"system file differs from its package: {p}",
                     "A tampered binary or library is how rootkits hide. (Low severity for data files.)",
                     f"pacman -S {pkg}   (reinstall; check `pacman -Qkk {pkg}` first)")

    # ------------------------------------------------------------------ live compromise indicators
    def check_processes(self):
        tcp_inodes = _tcp_socket_inodes()
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                exe = os.readlink(f"/proc/{pid}/exe")
                uid = int(re.search(r"^Uid:\s+(\d+)", read(f"/proc/{pid}/status"), re.M).group(1))
                cmd = (read(f"/proc/{pid}/cmdline") or "").replace("\0", " ").strip()
            except (OSError, AttributeError, TypeError):
                continue
            user = _uname(uid)
            label = f"pid {pid} ({user}): {cmd[:100] or exe}"
            base = os.path.basename(exe.replace(" (deleted)", ""))
            if exe.startswith("/memfd:"):
                self.add(f"memfd:{user}:{base}", "high", "process running from memory only (fileless)", label,
                         "investigate, then sharedbox-admin kill USER")
            elif re.match(r"/(tmp|var/tmp|dev/shm|run/user/\d+)/", exe):
                self.add(f"tmpexe:{user}:{base}", "medium", f"process running from a temp dir: {exe}", label,
                         "builds/tests sometimes do this; malware usually does")
            if MINERS.search(cmd) or MINERS.search(base):
                self.add(f"miner:{user}:{base}", "high", "crypto-miner process", label,
                         f"sharedbox-admin suspend {user}")
            if base in ("sh", "bash", "dash", "zsh", "python3", "perl", "nc", "ncat", "socat", "php"):
                try:
                    fd0 = os.readlink(f"/proc/{pid}/fd/0")
                except OSError:
                    continue
                m = re.match(r"socket:\[(\d+)\]", fd0)
                if m and m.group(1) in tcp_inodes:
                    self.add(f"revshell:{user}:{base}", "high",
                             "shell with its input wired to a TCP connection (reverse shell)",
                             f"{label} <-> {tcp_inodes[m.group(1)]}", f"sharedbox-admin kill {user}")
        rc, out = run(["ss", "-Htnp", "state", "established"])
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            peer = parts[3]
            port = int(peer.rpartition(":")[2] or 0)
            if port in SUSPICIOUS_PORTS:
                proc = re.search(r'\(\("([^"]+)"', line)
                self.add(f"outport:{port}:{proc.group(1) if proc else '?'}", "medium",
                         f"outbound connection to port {port} (mining pool / IRC / proxy port)",
                         line.strip()[:200])

    # ------------------------------------------------------------------ hardening + updates
    def check_kernel(self):
        want = {"kernel/kptr_restrict": 1, "kernel/dmesg_restrict": 1, "kernel/yama/ptrace_scope": 1,
                "fs/protected_symlinks": 1, "fs/protected_hardlinks": 1, "fs/protected_regular": 1,
                "fs/protected_fifos": 1, "fs/suid_dumpable": 0, "kernel/unprivileged_bpf_disabled": 1,
                "net/ipv4/tcp_syncookies": 1, "kernel/apparmor_restrict_unprivileged_userns": 1}
        for k, v in want.items():
            cur = read(f"/proc/sys/{k}")
            if cur is None:
                continue
            cur = int(cur.split()[0])
            ok = cur == 0 if v == 0 else cur >= v
            if not ok:
                self.add(f"sysctl:{k}", "low", f"sysctl {k.replace('/', '.')} = {cur} (want {v})", "",
                         f"echo '{k.replace('/', '.')} = {v}' > /etc/sysctl.d/90-sharedbox.conf")
        rc, out = run(["aa-enabled"])
        if rc == 1:
            self.add("apparmor", "medium", "AppArmor is disabled")

    def check_updates(self):
        if distro.current() == "arch":
            return self.check_updates_arch()
        # Debian/Ubuntu (and, as before, anything that isn't Arch)
        sec = distro.pending_updates("debian")["security"]
        self.facts["pending_security"] = sec
        if sec:
            self.add("updates:security", "medium" if len(sec) > 5 else "low",
                     f"{len(sec)} security update(s) not installed yet", ", ".join(sec[:15]),
                     "normally installed tonight by unattended-upgrades; if this persists, check "
                     "journalctl -u apt-daily-upgrade")
        age = distro.reboot_required("debian")
        if age is not None:
            self.facts["reboot_required_days"] = round(age, 1)
            if age > 3:
                self.add("reboot", "medium", f"reboot pending for {age:.0f} days (kernel/libc fixes not active)")
        au = distro.auto_updates("debian")
        if not au["enabled"]:
            self.add("unattended", "medium", "unattended security upgrades are not enabled", "", au["fix"])

    def check_updates_arch(self):
        """Arch has no unattended upgrades (and unattended full upgrades are risky), so the owner is
        told here, in the weekly email: pending updates (needs checkupdates) and reboot-required."""
        au = distro.auto_updates("arch")
        self.facts["auto_updates"] = au["how"]
        cache = os.path.join(AUDIT_DIR, "updates.json")
        c = sb.read_json(cache, {}) or {}
        if time.time() - c.get("ts", 0) < UPDATES_CACHE_H * 3600 and isinstance(c.get("result"), dict):
            res = c["result"]
        else:
            res = distro.pending_updates("arch")
            if res["supported"] and not res["error"]:
                try:
                    sb.write_json(cache, {"ts": int(time.time()), "result": res}, mode=0o600)
                except OSError:
                    pass
        pend = res.get("all")
        self.facts["pending_updates"] = pend if pend is not None else (res.get("error") or "unknown")
        if not res.get("supported"):
            self.add("updates:unchecked", "low", "can't check for pending updates",
                     res.get("error") or "", "pacman -S --needed pacman-contrib")
        elif pend:
            self.add("updates:pending", "medium" if len(pend) > 50 else "low",
                     f"{len(pend)} package update(s) pending (no automatic updates on Arch)",
                     ", ".join(pend[:15]) + (", ..." if len(pend) > 15 else ""), au["fix"])
        age = distro.reboot_required("arch")
        if age is not None:
            self.facts["reboot_required_days"] = round(age, 1)
            self.add("reboot", "medium" if age > 3 else "low",
                     f"reboot pending for {age:.0f} days: the running kernel {os.uname().release} "
                     "was upgraded (its modules are gone, so new USB/filesystem drivers can't load)",
                     "", "reboot at a quiet time (or wait for the scheduled reboot)")

    def check_canary(self, since):
        hits = []
        for line in (read(CANARY_LOG) or "").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("ts", 0) >= since:
                hits.append(r)
        self.facts["canary_hits"] = hits
        users = sorted({h.get("user", "?") for h in hits})
        for u in users:
            n = sum(1 for h in hits if h.get("user") == u)
            ex = next(h for h in hits if h.get("user") == u)
            self.add(f"canary:{u}", "high", f"TRIPWIRE: {u} opened decoy secrets ({n}x)",
                     f"e.g. {ex.get('file')} by `{ex.get('cmd', '')[:100]}`"
                     + (" — account was frozen" if any(h.get("frozen") for h in hits if h.get("user") == u) else ""),
                     f"investigate; sharedbox-canary thaw {u} to unfreeze")

    # ------------------------------------------------------------------ driver
    def run_all(self, deep=True, since=None):
        since = since or time.time() - 7 * 86400
        checks = [self.check_exposure, self.check_sshd, self.check_accounts, self.check_homes,
                  self.check_keys, self.check_watched, self.check_processes, self.check_kernel,
                  self.check_updates, lambda: self.check_canary(since)]
        if deep:
            checks += [self.check_setuid, self.check_writable,
                       self.check_pacman if distro.current() == "arch" else self.check_dpkg]
        for c in checks:
            try:
                c()
            except Exception as e:  # one broken check must not kill the report
                self.add(f"auditerr:{getattr(c, '__name__', 'check')}", "low",
                         f"audit check {getattr(c, '__name__', '?')} failed: {e}")
        self.findings.sort(key=lambda f: (SEV_ORDER[f["sev"]], f["id"]))
        return self.findings

    def save(self, deep=True):
        """Persist results; returns (new, resolved) relative to the previous saved run."""
        os.makedirs(AUDIT_DIR, mode=0o700, exist_ok=True)
        prev = sb.read_json(LAST, {}) or {}
        prev_ids = {f["id"] for f in prev.get("findings", [])}
        cur_ids = {f["id"] for f in self.findings}
        new = [f for f in self.findings if f["id"] not in prev_ids] if prev else []
        resolved = [f for f in prev.get("findings", []) if f["id"] not in cur_ids]
        base = dict(self.baseline)
        base.update(self.new_baseline)
        if not deep:   # quick runs don't refresh the deep baselines
            for k in ("setuid",):
                base[k] = self.baseline.get(k, base.get(k))
        sb.write_json(BASELINE, base, mode=0o600)
        sb.write_json(LAST, {"ts": int(time.time()), "findings": self.findings, "facts": self.facts},
                      mode=0o600)
        return new, resolved


def _gid(name):
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return -1


def _uname(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def sha_str(s):
    return hashlib.sha256(s.encode()).hexdigest()[:10]


def _tcp_socket_inodes():
    """inode -> 'remote ip:port' for TCP sockets connected to a non-loopback peer."""
    out = {}
    for path, v6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        for line in (read(path) or "").splitlines()[1:]:
            f = line.split()
            if len(f) < 10 or f[3] != "01":   # ESTABLISHED
                continue
            rip, rport = f[2].split(":")
            try:
                if v6:
                    b = bytes.fromhex(rip)
                    ip = ipaddress.IPv6Address(b"".join(b[i:i + 4][::-1] for i in range(0, 16, 4)))
                    if ip.ipv4_mapped:
                        ip = ip.ipv4_mapped
                else:
                    ip = ipaddress.IPv4Address(bytes.fromhex(rip)[::-1])
            except ValueError:
                continue
            if ip.is_loopback:
                continue
            out[f[9]] = f"{ip}:{int(rport, 16)}"
    return out
