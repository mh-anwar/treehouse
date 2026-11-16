"""Federation: the same friend accounts on every treehouse box (docs/multibox.md, "Accounts across boxes").

The primary box (BOX_ROLE=primary, where invites happen and the public site lives) is the only source of truth
for which accounts exist. Every box publishes a small *roster* (users, uids, status, limits, the per-user
box-to-box SSH key and this box's SSH host key), signed by that box's root-only ed25519 key
(`ssh-keygen -Y sign`, namespace treehouse-roster). sharedbox-web serves it to the boxes in BOXES at
GET /api/peer/roster; each box's root (`sharedbox-admin fed-sync`, a 5-minute timer, no daemon) pulls the
others', verifies the signature against /etc/sharedbox/fed/allowed_signers, and:
  - on a secondary: creates missing accounts with the SAME uid/gid (so the same port block, file owners and
    limits), resumes/suspends them to match, never deletes a home (removed upstream = suspended here);
  - on every box: authorizes the user's keys from the other boxes in a managed block of their
    ~/.ssh/authorized_keys, writes `Host <box>` blocks and pinned host keys, so `ssh minastirith` and
    `box run --anywhere` work as that user with no prompt, in both directions;
  - then, as the user, links their T3 Code on each box to the other one (`t3 environment link`), copies the
    GitHub token (long-lived, safe to share) and git identity from the home box. Claude/Codex logins are NOT
    copied (rotating refresh tokens): `box boxes login KEY --on BOX` runs the normal device login there.

Trust: an attacker who can bind the web port on a peer (a friend, if sharedbox-web is down) can serve any JSON,
but can't sign it; a replayed old roster is refused by its serial. Root on any listed box is the owner.

This module is pure logic plus tiny I/O helpers that take their paths explicitly, so it's unit-testable.
Stdlib only.
"""

import base64
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

NAMESPACE = "treehouse-roster"
FED_ETC = "/etc/sharedbox/fed"               # root 0700: box_ed25519 (signing key), allowed_signers
ROSTER_REL = "fed/roster.json"               # under STATE_DIR, 0644: what sharedbox-web serves to peers
STATE_REL = "fed/state.json"                 # under STATE_DIR, root 0600: serials, last good peer rosters
MAX_AGE = 2 * 86400       # a roster older than this changes no accounts (a stale box never suspends anyone)
KEEP_KEYS_DAYS = 14       # a peer silent this long: its keys are dropped from authorized_keys
SKEW = 600
MAX_KEYS = 20
MAX_BYTES = 256 << 10
LINK_EVERY = 12 * 3600    # the as-user link pass (T3 links, gh token) per user, unless something changed
RELINK_DAYS = 7           # T3 links last 30 days: renew when fewer than this are left
BEGIN = "# >>> treehouse boxes (managed by sharedbox; edits inside are replaced) >>>"
END = "# <<< treehouse boxes <<<"
KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa",
             "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")
NAME_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
STATUSES = ("active", "suspended")
LIMIT_KEYS = ("CPU_WEIGHT", "IO_WEIGHT", "MEMORY_HIGH", "MEMORY_MAX", "MEMORY_SWAP_MAX", "TASKS_MAX",
              "DISK_BUDGET_GB")
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def role(cfg):
    """primary (default; makes accounts) | secondary (copies them from the primary)."""
    return "secondary" if str(cfg.get("BOX_ROLE") or "").strip().lower() == "secondary" else "primary"


def self_name(boxes, my_ip, hostname):
    return next((n for n, ip in boxes.items() if ip == my_ip), (hostname or "").split(".")[0].lower())


# ---------- keys ----------

def clean_key(line):
    """A plain public key line (type, base64, optional tidy comment) or None. No options, ever: a roster can't
    smuggle command=/from= into authorized_keys."""
    parts = str(line or "").strip().split()
    if len(parts) < 2 or parts[0] not in KEY_TYPES or len(parts[1]) > 2000:
        return None
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError:
        return None
    n = int.from_bytes(blob[:4], "big") if len(blob) > 4 else 0
    if blob[4:4 + n].decode("ascii", "replace") != parts[0]:
        return None
    comment = re.sub(r"[^A-Za-z0-9@._+-]", "", " ".join(parts[2:]))[:64]
    return f"{parts[0]} {parts[1]}" + (f" {comment}" if comment else "")


def key_id(line):
    p = line.split()
    return f"{p[0]} {p[1]}" if len(p) >= 2 else line


def managed_block(text, lines):
    """text with our BEGIN..END block replaced by `lines` (appended when missing, removed when lines is empty).
    Everything outside the block is the user's and is kept byte for byte."""
    text = text or ""
    pat = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)
    body = (BEGIN + "\n" + "".join(l + "\n" for l in lines) + END + "\n") if lines else ""
    if pat.search(text):
        return pat.sub(lambda _m: body, text, count=1)
    if not body:
        return text
    return text + ("" if not text or text.endswith("\n") else "\n") + body


def outside_block(text):
    """The user's own lines (outside our block)."""
    return re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "", text or "", flags=re.S)


def own_keys(authorized_text):
    """Valid keys the user put in ~/.ssh/authorized_keys themselves (their laptop): sent from their home box so
    their laptop reaches the other boxes too."""
    out = []
    for line in outside_block(authorized_text).splitlines():
        k = clean_key(line)
        if k and key_id(k) not in {key_id(x) for x in out}:
            out.append(k)
    return out[:MAX_KEYS]


# ---------- roster ----------

def build_roster(box, box_role, users, serial, now, host_key=""):
    """users: {name: {uid, status, home, limits, keys}}. Keys are cleaned here; bad entries dropped."""
    out = {}
    for name, u in sorted(users.items()):
        if not NAME_RE.fullmatch(name) or not isinstance(u.get("uid"), int):
            continue
        keys = [k for k in (clean_key(x) for x in u.get("keys") or []) if k][:MAX_KEYS]
        lim = {k: str(v) for k, v in (u.get("limits") or {}).items() if k in LIMIT_KEYS and v not in (None, "")}
        out[name] = {"uid": u["uid"], "status": u.get("status") if u.get("status") in STATUSES else "suspended",
                     "home": u.get("home") or box, "limits": lim, "keys": keys}
    return {"v": 1, "box": box, "role": box_role, "serial": int(serial), "ts": int(now),
            "host_key": clean_key(host_key) or "", "users": out}


def encode(doc):
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()


def check_roster(doc, peer, last_serial, now):
    """(ok, why). ok=False: ignore it entirely (keep the last good one)."""
    if not isinstance(doc, dict) or doc.get("v") != 1:
        return False, "not a roster"
    if doc.get("box") != peer:
        return False, f"signed for {doc.get('box')!r}, expected {peer!r}"
    if doc.get("role") not in ("primary", "secondary"):
        return False, "bad role"
    if not isinstance(doc.get("serial"), int) or not isinstance(doc.get("ts"), int):
        return False, "no serial"
    if doc["serial"] < (last_serial or 0):
        return False, "older than one already seen (replay?)"
    if doc["ts"] > now + SKEW:
        return False, "from the future (clock?)"
    if not isinstance(doc.get("users"), dict):
        return False, "no users"
    return True, ""


def fresh(doc, now):
    return isinstance(doc, dict) and now - int(doc.get("ts") or 0) <= MAX_AGE


def plan_accounts(upstream, local, passwd, uid_lo, uid_hi):
    """What a secondary does to match the primary's roster `upstream`. Never deletes.

    local:  this box's registry records {name: {uid, status, home_box}} (only friends' accounts)
    passwd: {name: uid} of every account on this box (plus groups' gids under the key "gid:<n>" -> name)
    Returns [(action, name, detail)], action in create|resume|suspend|limits|conflict."""
    box = upstream["box"]
    users = upstream.get("users") or {}
    uid_owner = {uid: n for n, uid in passwd.items() if not n.startswith("gid:")}
    gid_owner = {int(n[4:]): uid for n, uid in passwd.items() if n.startswith("gid:")}
    out = []
    for name, u in sorted(users.items()):
        uid = u.get("uid")
        if not NAME_RE.fullmatch(name) or not isinstance(uid, int) or not uid_lo <= uid <= uid_hi:
            out.append(("conflict", name, "uid outside the friends' range"))
            continue
        if u.get("home") not in (None, box):
            continue  # someone else's home: only the home box decides about it
        rec = local.get(name)
        active = u.get("status") == "active"
        if rec is None:
            if name in passwd:
                out.append(("conflict", name, "an account with that name already exists here (not a treehouse one)"))
            elif uid in uid_owner:
                out.append(("conflict", name, f"uid {uid} is taken here by {uid_owner[uid]}"))
            elif uid in gid_owner:
                out.append(("conflict", name, f"gid {uid} is taken here by group {gid_owner[uid]}"))
            elif active:
                out.append(("create", name, uid))
            continue
        if rec.get("home_box") != box:
            out.append(("conflict", name, "a local account here, not a copy from " + box))
            continue
        if rec.get("uid") != uid:
            out.append(("conflict", name, f"uid differs (here {rec.get('uid')}, {box} {uid})"))
            continue
        if active and rec.get("status") != "active":
            out.append(("resume", name, ""))
        elif not active and rec.get("status") == "active":
            out.append(("suspend", name, f"suspended on {box}"))
        if (rec.get("limits") or {}) != (u.get("limits") or {}):
            out.append(("limits", name, u.get("limits") or {}))
    for name, rec in sorted(local.items()):
        if rec.get("home_box") == box and name not in users and rec.get("status") == "active":
            out.append(("suspend", name, f"removed on {box} (home kept here)"))
    return out


def merge_keys(local_uids, rosters, now):
    """{name: [keys]} to authorize on this box: every peer's keys for a user with the SAME uid here. A peer
    silent for KEEP_KEYS_DAYS stops counting."""
    out = {n: [] for n in local_uids}
    for doc in rosters.values():
        if not isinstance(doc, dict) or now - int(doc.get("ts") or 0) > KEEP_KEYS_DAYS * 86400:
            continue
        for name, u in (doc.get("users") or {}).items():
            if name not in local_uids or u.get("uid") != local_uids[name] or u.get("status") != "active":
                continue
            for k in u.get("keys") or []:
                k = clean_key(k)
                if k and key_id(k) not in {key_id(x) for x in out[name]} and len(out[name]) < MAX_KEYS:
                    out[name].append(k)
    return out


def present_on(rosters, name, uid, now):
    """Boxes (peer names) where `name` is an active account with the same uid, per fresh-enough rosters."""
    out = []
    for peer, doc in sorted(rosters.items()):
        u = ((doc or {}).get("users") or {}).get(name)
        if u and u.get("uid") == uid and u.get("status") == "active" and \
                now - int(doc.get("ts") or 0) <= KEEP_KEYS_DAYS * 86400:
            out.append(peer)
    return out


def ssh_config_lines(boxes, me, user, key_path="~/.ssh/id_treehouse", kh="~/.ssh/treehouse_known_hosts"):
    out = []
    for name, ip in boxes.items():
        if name == me:
            continue
        out += [f"Host {name}", f"  HostName {ip}", f"  User {user}", f"  IdentityFile {key_path}",
                "  IdentitiesOnly yes", f"  UserKnownHostsFile {kh} ~/.ssh/known_hosts", ""]
    return out[:-1] if out else out


def known_hosts_lines(boxes, rosters):
    """Pinned host keys from the signed rosters: no trust-on-first-use prompt and no MITM on the first login."""
    out = []
    for name, ip in boxes.items():
        k = clean_key(((rosters.get(name) or {}).get("host_key")) or "")
        if k:
            out.append(f"{name},{ip} {key_id(k)}")
    return out


# ---------- T3 linking ----------

def pair_token(url):
    """The pairing code in a `t3 pair` URL (#token=... or ?token=...)."""
    u = urllib.parse.urlsplit(url or "")
    for part in (u.fragment, u.query):
        tok = (urllib.parse.parse_qs(part).get("token") or [""])[0]
        if tok:
            return tok
    return ""


def link_due(envs, url, now, days=RELINK_DAYS):
    """True unless `t3 environment list --json` already has a link to `url` that's good for > `days` days."""
    origin = url.rstrip("/")
    for e in envs or []:
        if not isinstance(e, dict) or origin not in [str(x).rstrip("/") for x in e.get("urls") or []]:
            continue
        try:
            exp = time.mktime(time.strptime(str(e.get("expiresAt"))[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        except ValueError:
            return True
        return exp - now < days * 86400
    return True


def link_argv(url, token, access="full-access"):
    return ["t3", "environment", "link", url, "--pairing-code", token, "--access", access]


# ---------- signing (ssh-keygen -Y) ----------

def sign(data, key_path):
    r = subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", key_path, "-n", NAMESPACE], input=data,
                       capture_output=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("ssh-keygen sign failed: " + r.stderr.decode(errors="replace")[-200:])
    return r.stdout.decode()


def verify(data, sig, signers_path, identity):
    with tempfile.NamedTemporaryFile("w", suffix=".sig") as f:
        f.write(sig)
        f.flush()
        r = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", signers_path, "-I", identity, "-n", NAMESPACE,
                            "-s", f.name], input=data, capture_output=True, timeout=30)
    return r.returncode == 0


def wire(doc, key_path):
    data = encode(doc)
    return {"doc": data.decode(), "sig": sign(data, key_path)}


def unwire(w, signers_path, identity):
    """(doc, why) from a peer's /api/peer/roster answer."""
    if not isinstance(w, dict) or not isinstance(w.get("doc"), str) or not isinstance(w.get("sig"), str):
        return None, "not a signed roster"
    data = w["doc"].encode()
    if not verify(data, w["sig"], signers_path, identity):
        return None, "bad signature (or this box doesn't trust that box's key yet: sharedbox-admin fed-trust)"
    try:
        return json.loads(data), ""
    except ValueError:
        return None, "bad JSON"


def signer_line(name, pubkey):
    k = clean_key(pubkey)
    if not k or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", name or ""):
        return None
    return f'{name} namespaces="{NAMESPACE}" {key_id(k)}'


def fetch(ip, port, timeout=5):
    try:
        with _OPENER.open(f"http://{ip}:{port}/api/peer/roster", timeout=timeout) as r:
            body = r.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            return None, "too big"
        return json.loads(body), ""
    except urllib.error.HTTPError as ex:
        return None, {403: "doesn't list this box in BOXES", 404: "needs a treehouse update (or fed-init)"}.get(
            ex.code, f"error {ex.code}")
    except Exception:  # noqa: BLE001 - refused, timeout, bad JSON
        return None, "not reachable"


def write_private(path, text, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
