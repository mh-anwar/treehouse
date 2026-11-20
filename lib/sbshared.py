"""Group-owned shared servers: code in SHARED_DIR/NAME, run by root-written system units as SHARED_USER.

Any friend or admin may create, edit, run and remove one. Unprivileged callers (`box shared`, the web
page) only read the registry and systemd state; every change is a request that root carries out with
handle() (root drops in SHARED_DROP via sharedbox-admin, or the web portal), after checking who asked.

Stdlib only. The read side is safe to import unprivileged; the write side assumes root.
"""

import argparse
import fcntl
import grp
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
import time

import sbcommon as sb

SYSTEMD_DIR = "/etc/systemd/system"
SLICE = "sharedbox-shared.slice"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
ACTIONS = ("create", "describe", "set", "start", "stop", "restart", "rm", "logs")
DESC_MIN, DESC_MAX = 12, 300


def check_desc(desc):
    """(clean one-line description, error or None). Required on create; shown on everyone's dashboard."""
    if not isinstance(desc, str):
        return "", "a description is required: --desc \"what it does and who it's for\""
    d = " ".join("".join(c if c.isprintable() else " " for c in desc).split())
    if len(d) < DESC_MIN:
        return "", (f"the description needs at least {DESC_MIN} characters: one plain sentence saying what it does "
                    "and who it's for (e.g. \"Discord bot that posts the week's hackathon schedule\")")
    if len(d) > DESC_MAX:
        return "", f"the description can be at most {DESC_MAX} characters"
    return d, None
EXEC_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
MAX_ARGS, MAX_CMD_BYTES, MAX_LOG_LINES = 64, 4096, 500
LOCK = os.path.join(sb.STATE_DIR, ".shared.lock")


def unit(name):
    return f"sb-shared-{name}.service"


def server_dir(cfg, name):
    return os.path.join(cfg["SHARED_DIR"], name)


def port_range(cfg):
    base = sb.cfg_int(cfg, "SHARED_PORT_BASE")
    return base, base + sb.cfg_int(cfg, "SHARED_PORT_COUNT") - 1


def registry():
    """name -> record. Root writes it (0644); anyone may read it."""
    reg = sb.read_json(sb.SHARED_JSON, {})
    return {k: v for k, v in reg.items() if NAME_RE.match(k) and isinstance(v, dict)} \
        if isinstance(reg, dict) else {}


def states(names):
    """name -> systemd ActiveState (active/inactive/failed/...), one systemctl call, no root needed."""
    names = list(names)
    if not names:
        return {}
    try:
        r = subprocess.run(["systemctl", "is-active", *map(unit, names)], capture_output=True,
                           text=True, timeout=10)
        out = r.stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        out = []
    return {n: (out[i] if i < len(out) else "unknown") for i, n in enumerate(names)}


def list_servers(cfg):
    """What `box shared ls --json` prints under "servers" (and what the web page shows)."""
    reg = registry()
    st = states(reg)
    out = []
    for name in sorted(reg):
        r = reg[name]
        out.append({"name": name, "port": r.get("port"), "cmd": r.get("cmd") or [],
                    # "new" = created, nothing set to run yet (no unit)
                    "state": st.get(name, "unknown") if r.get("cmd") else "new",
                    "dir": server_dir(cfg, name),
                    "desc": r.get("desc") or "",
                    **{k: r.get(k) for k in ("created_by", "created", "updated_by", "updated")}})
    return out


def parse_tail(a):
    """argparse's REMAINDER also swallows options typed after NAME (`set web --port 19001 --json -- node x`):
    split a.cmd into our options (before `--`, or before the first plain word) and the command. Error or None."""
    rest = list(a.cmd or [])
    if "--" in rest:
        i = rest.index("--")
    else:
        i = next((k for k, t in enumerate(rest)
                  if not t.startswith("-") and (k == 0 or rest[k - 1] not in ("--port", "-n", "--lines", "--desc"))),
                 len(rest))
    opts, a.cmd = rest[:i], rest[i + 1:] if "--" in rest else rest[i:]
    p = argparse.ArgumentParser(add_help=False, exit_on_error=False, argument_default=argparse.SUPPRESS)
    p.add_argument("--port", type=int)
    p.add_argument("--desc")
    p.add_argument("-n", "--lines", type=int)
    p.add_argument("--json", action="store_true")
    try:
        ns, extra = p.parse_known_args(opts)
    except argparse.ArgumentError as e:
        return str(e)
    if extra:
        return f"unknown option {extra[0]} (put the command after --)"
    vars(a).update(vars(ns))
    return None


def may_use(cfg, user):
    """Friends (active) and ADMIN_USERS may manage shared servers."""
    return user in sb.admin_users(cfg) or bool(sb.active_friend(user))


# ---------- unit file ----------

def sd_quote(arg):
    """One ExecStart= word: quoted, no specifier (%) or variable ($) expansion."""
    if arg == ";":
        return "\\;"  # a lone ; separates commands
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def check_cmd(cmd):
    """Error text for a malformed cmd list, else None."""
    if not isinstance(cmd, list) or not cmd or len(cmd) > MAX_ARGS:
        return f"cmd must be a list of 1-{MAX_ARGS} strings"
    if not all(isinstance(a, str) and a and a.isprintable() for a in cmd):
        return "every part of the command must be non-empty printable text (no newlines or control characters)"
    if sum(len(a.encode()) for a in cmd) > MAX_CMD_BYTES:
        return f"command too long (max {MAX_CMD_BYTES} bytes)"
    return None


def resolve_exe(cfg, name, exe):
    """(absolute path, error). ./x = inside the server's folder; else absolute or on EXEC_PATH."""
    d = server_dir(cfg, name)
    if exe.startswith("./"):
        p = os.path.normpath(os.path.join(d, exe[2:]))
        if not p.startswith(d + "/"):
            return None, f"{exe} points outside {d}"
        return p, None  # may not exist yet; the unit just fails until it does
    if exe.startswith("/"):
        p = os.path.normpath(exe)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p, None
        return None, f"{exe} is not an executable file"
    if "/" in exe:
        return None, f"{exe}: use ./path for a file in the server's folder, or an absolute path"
    p = shutil.which(exe, path=os.pathsep.join(EXEC_PATH))
    if not p:
        return None, (f"{exe} is not installed system-wide ({':'.join(EXEC_PATH)}); give its absolute path "
                      "or put it in the server's folder (tools in someone's home aren't reachable)")
    return p, None


def render_unit(cfg, name, argv, port):
    d = server_dir(cfg, name)
    esc = lambda s: s.replace("%", "%%").replace("$", "$$")  # noqa: E731 - paths/values on plain lines
    return "\n".join([
        "# managed by sharedbox (box shared) — changes are overwritten",
        "[Unit]", f"Description=shared server {name} (box shared)",
        "After=network-online.target", "Wants=network-online.target",
        "StartLimitIntervalSec=600", "StartLimitBurst=10", "",
        "[Service]",
        f"User={cfg['SHARED_USER']}", f"Group={cfg['SHARED_GROUP']}",
        f"WorkingDirectory={esc(d)}",
        f"Environment=PORT={int(port)}", f'Environment="HOME={esc(d)}"',
        "Environment=PATH=/usr/local/bin:/usr/bin:/bin",
        "ExecStart=" + " ".join(sd_quote(a) for a in argv),
        "Restart=on-failure", "RestartSec=5",
        f"Slice={SLICE}", "OOMScoreAdjust=500",
        "UMask=0002",
        # every member can edit the code, so the code gets nothing but its own folder
        "NoNewPrivileges=yes", "ProtectSystem=strict", f"ReadWritePaths={esc(d)}", "ProtectHome=yes",
        "PrivateTmp=yes", "PrivateDevices=yes", "ProtectKernelTunables=yes", "ProtectKernelModules=yes",
        "ProtectControlGroups=yes", "RestrictSUIDSGID=yes", "LockPersonality=yes", "RestrictNamespaces=yes",
        "", "[Install]", "WantedBy=multi-user.target", ""])


def render_slice(cfg):
    return "\n".join([
        "# managed by sharedbox: every shared server (box shared) together",
        "[Unit]", "Description=sharedbox shared servers", "", "[Slice]",
        f"MemoryMax={cfg['SHARED_MEMORY_MAX']}", f"CPUQuota={cfg['SHARED_CPU_QUOTA']}",
        f"CPUWeight={cfg['CPU_WEIGHT']}", f"IOWeight={cfg['IO_WEIGHT']}", f"TasksMax={cfg['TASKS_MAX']}", ""])


# ---------- root side ----------

def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)


def _write(path, text):
    """Atomic 0644 root file; False if it was already identical."""
    try:
        with open(path) as f:
            if f.read() == text:
                return False
    except FileNotFoundError:
        pass
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-sb-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            os.fchmod(f.fileno(), 0o644)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    return True


def _ids(cfg):
    return pwd.getpwnam(cfg["SHARED_USER"]).pw_uid, grp.getgrnam(cfg["SHARED_GROUP"]).gr_gid


def _real_dir(path):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    return st if stat.S_ISDIR(st.st_mode) else False  # False: exists but isn't a plain directory


def _secure(path, mode, uid, gid):
    """mkdir if missing; fix owner/mode of an existing plain directory (never follows a symlink)."""
    if _real_dir(path) is None:
        os.mkdir(path, mode)
    if _real_dir(path) is False:
        raise RuntimeError(f"{path} is not a directory")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _group_acl(cfg, path):
    """New files stay group-writable whatever the creator's umask (a default ACL overrides it)."""
    if shutil.which("setfacl"):
        g = cfg["SHARED_GROUP"]
        _run(["setfacl", "-m", f"g:{g}:rwX,d:g:{g}:rwX,d:u::rwX,d:g::rwX,d:o::rX", "--", path])


def ensure_base(cfg):
    """SHARED_DIR (root:group 0750: code may hold secrets, members only), its .trash, and the slice."""
    _, gid = _ids(cfg)
    _secure(cfg["SHARED_DIR"], 0o750, 0, gid)
    _secure(os.path.join(cfg["SHARED_DIR"], ".trash"), 0o750, 0, gid)
    return _write(os.path.join(SYSTEMD_DIR, SLICE), render_slice(cfg))


def save(reg):
    sb.write_json(sb.SHARED_JSON, reg, mode=0o644)


def _free_port(cfg, reg, want=None, keep=None):
    lo, hi = port_range(cfg)
    used = {r.get("port") for n, r in reg.items() if n != keep}
    if want is not None:
        if not isinstance(want, int) or isinstance(want, bool) or not lo <= want <= hi:
            return None, f"port must be in the shared range {lo}-{hi}"
        if want in used:
            return None, f"port {want} is taken by another shared server"
        return want, None
    p = next((p for p in range(lo, hi + 1) if p not in used), None)
    return (p, None) if p else (None, f"no free port left in {lo}-{hi}")


def _sctl(*args):
    r = _run(["systemctl", *args])
    return r.returncode == 0, (r.stderr or r.stdout).strip()[-400:]


def handle(cfg, user, req):
    """Carry out one request for `user` (already identified by the caller). Returns the reply dict."""
    if not may_use(cfg, user):
        return {"ok": False, "error": "only friends and the box owner's accounts can manage shared servers"}
    action, name = req.get("action"), req.get("name")
    if action not in ACTIONS:
        return {"ok": False, "error": f"unknown action; one of {', '.join(ACTIONS)}"}
    if not isinstance(name, str) or not NAME_RE.match(name):
        return {"ok": False, "error": "name: 2-31 lowercase letters, digits or -, starting with a letter"}
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    with open(LOCK, "a") as lk:  # the spool and the web portal may both act at once
        fcntl.flock(lk, fcntl.LOCK_EX)
        return _handle(cfg, user, action, name, req)


def _handle(cfg, user, action, name, req):
    reg = registry()
    rec = reg.get(name)
    d, u = server_dir(cfg, name), unit(name)
    upath = os.path.join(SYSTEMD_DIR, u)
    now = int(time.time())
    if action == "create":
        if rec:
            return {"ok": False, "error": f"{name} already exists ({d})"}
        desc, err = check_desc(req.get("desc"))
        if err:
            return {"ok": False, "error": err}
        if len(reg) >= sb.cfg_int(cfg, "SHARED_MAX"):
            return {"ok": False, "error": f"the box already has {len(reg)} shared servers (SHARED_MAX)"}
        port, err = _free_port(cfg, reg)
        if err:
            return {"ok": False, "error": err}
        ensure_base(cfg)
        if _real_dir(d) is False:
            return {"ok": False, "error": f"{d} exists and isn't a folder; ask the owner"}
        uid, gid = _ids(cfg)
        _secure(d, 0o2775, uid, gid)  # an existing folder (registry lost) is adopted, code kept
        _group_acl(cfg, d)
        reg[name] = {"port": port, "cmd": [], "desc": desc, "created_by": user, "created": now,
                     "updated_by": user, "updated": now}
        save(reg)
        sb.log_event("shared_created", user, server=name, port=port)
        return {"ok": True, "message": f"created {name}: put its code in {d}, then "
                                       f"`box shared set {name} -- COMMAND...` to run it (port {port}, also in $PORT)",
                "dir": d, "port": port}
    if not rec:
        return {"ok": False, "error": f"no shared server called {name} (box shared ls)"}
    if action == "describe":
        desc, err = check_desc(req.get("desc"))
        if err:
            return {"ok": False, "error": err}
        rec.update(desc=desc, updated_by=user, updated=now)
        save(reg)
        sb.log_event("shared_described", user, server=name)
        return {"ok": True, "message": f"{name}: description saved"}
    if action == "set":
        cmd = req.get("cmd")
        err = check_cmd(cmd)
        if err:
            return {"ok": False, "error": err}
        exe, err = resolve_exe(cfg, name, cmd[0])
        if err:
            return {"ok": False, "error": err}
        want = req.get("port")
        port, err = _free_port(cfg, reg, want if want is not None else rec.get("port"), keep=name)
        if err and want is None:  # the old port is no longer valid (range changed): pick a new one
            port, err = _free_port(cfg, reg, keep=name)
        if err:
            return {"ok": False, "error": err}
        if _real_dir(d) is None:  # someone moved it away by hand: give it back, empty
            uid, gid = _ids(cfg)
            _secure(d, 0o2775, uid, gid)
            _group_acl(cfg, d)
        ensure_base(cfg)
        _write(upath, render_unit(cfg, name, [exe] + cmd[1:], port))
        _sctl("daemon-reload")
        _sctl("enable", u)
        ok, out = _sctl("restart", u)
        rec.update(cmd=list(cmd), port=port, updated_by=user, updated=now)
        save(reg)
        sb.log_event("shared_set", user, server=name, port=port, cmd=cmd)
        if not ok:
            return {"ok": False, "error": f"saved, but it didn't start: {out} (box shared logs {name})"}
        note = "" if os.path.exists(exe) else f" (note: {exe} doesn't exist yet, so it will fail until it does)"
        return {"ok": True, "message": f"{name} runs `{' '.join(cmd)}` on port {port}{note}. "
                                       f"logs: box shared logs {name}", "port": port}
    if action == "logs":
        try:
            n = max(1, min(int(req.get("lines") or 100), MAX_LOG_LINES))
        except (TypeError, ValueError):
            n = 100
        r = _run(["journalctl", "-u", u, "-n", str(n), "--no-pager", "-o", "short-iso"])
        lines = [sb.printable(ln) for ln in r.stdout.splitlines()[-n:]]
        return {"ok": True, "lines": lines}
    if action in ("start", "stop", "restart"):
        if not os.path.exists(upath):
            return {"ok": False, "error": f"{name} has nothing to run yet: box shared set {name} -- COMMAND..."}
        # stop = stays off across reboots too; start/restart = on, and back after a reboot
        ok, out = _sctl(*{"start": ("enable", "--now"), "stop": ("disable", "--now"),
                          "restart": ("enable",)}[action], u)
        if ok and action == "restart":
            ok, out = _sctl("restart", u)
        rec.update(updated_by=user, updated=now)
        save(reg)
        sb.log_event(f"shared_{action}", user, server=name, ok=ok)
        if not ok:
            return {"ok": False, "error": f"{action} failed: {out} (box shared logs {name})"}
        return {"ok": True, "message": {"start": "started", "stop": "stopped", "restart": "restarted"}[action]
                + f" {name}"}
    # rm: the unit and the registry entry go, the code is only moved aside
    _sctl("disable", "--now", u)
    if os.path.exists(upath):
        os.unlink(upath)
        _sctl("daemon-reload")
    _sctl("reset-failed", u)
    moved = None
    if _real_dir(d):
        ensure_base(cfg)
        base = os.path.join(cfg["SHARED_DIR"], ".trash", f"{name}-{time.strftime('%Y%m%d-%H%M%S')}")
        moved, i = base, 1
        while os.path.lexists(moved):  # removed twice within a second
            moved, i = f"{base}.{i}", i + 1
        os.rename(d, moved)  # same filesystem, never follows links
    del reg[name]
    save(reg)
    sb.log_event("shared_removed", user, server=name, moved_to=moved)
    return {"ok": True, "message": f"removed {name}" + (f"; its code was moved to {moved}" if moved else "")}


def sync(cfg, log=print):
    """Root, idempotent (install + reapply): base dirs, slice, every server folder's owner/mode, and
    SHARED_GROUP membership = active friends + ADMIN_USERS. Never starts, stops or rewrites a server."""
    changed = ensure_base(cfg)
    uid, gid = _ids(cfg)
    for name in registry():
        d = server_dir(cfg, name)
        if _real_dir(d):
            _secure(d, 0o2775, uid, gid)
    try:
        members = set(grp.getgrnam(cfg["SHARED_GROUP"]).gr_mem)
    except KeyError:
        members = set()
    want = {n for n, r in sb.friend_users().items() if r.get("status", "active") == "active"}
    for a in sb.admin_users(cfg):
        try:
            pwd.getpwnam(a)
            want.add(a)
        except KeyError:
            pass
    for n in sorted(want - members):
        if _run(["gpasswd", "-a", n, cfg["SHARED_GROUP"]]).returncode == 0:
            log(f"added {n} to {cfg['SHARED_GROUP']} (takes effect at their next login/T3 restart)")
    if changed:
        _sctl("daemon-reload")
    return sorted(want)
