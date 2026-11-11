"""Cloudflare API v4 for PUBLIC_MODE=cloudflare (root only): tunnel, DNS, Access app/policy, invites.

Stdlib only (urllib). The API token is read by root from CF_TOKEN_FILE (/etc/sharedbox/cloudflare-token,
mode 600, owned by root); it is never logged, never put in argv, an error message, or the config.

The token needs (dash.cloudflare.com > My Profile > API Tokens > Create Token > Create Custom Token):
  Account  Cloudflare Tunnel                                    Edit
  Account  Access: Apps and Policies                            Edit
  Account  Access: Organizations, Identity Providers, and Groups Edit
  Zone     DNS                                                  Edit
  Zone     Zone                                                 Read
A 403 names the missing one.

Friends' emails live in ONE managed reusable Access policy ("treehouse <host> friends", CF_ACCESS_POLICY_ID).
Every change is a locked read-modify-write of its include list that keeps every other rule as it was, never
drops CF_OWNER_EMAILS, never writes an empty list, and re-reads the policy to confirm.

SHAREDBOX_CF_API overrides the API base URL (tests point it at a fake server).
"""

import fcntl
import json
import os
import re
import secrets
import stat
import urllib.error
import urllib.parse
import urllib.request

import sbcommon as sb

API = os.environ.get("SHAREDBOX_CF_API", "https://api.cloudflare.com/client/v4").rstrip("/")
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,200}")
HOST_RE = re.compile(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
ID_RE = re.compile(r"[A-Za-z0-9-]{8,64}")

PERM = {
    "account": "any Account permission (the token must be scoped to your account)",
    "tunnel": "Account > Cloudflare Tunnel > Edit",
    "access": "Account > Access: Apps and Policies > Edit",
    "org": "Account > Access: Organizations, Identity Providers, and Groups > Edit",
    "zone": "Zone > Zone > Read",
    "dns": "Zone > DNS > Edit",
}
PERMISSIONS_HELP = "\n".join(f"  - {v}" for k, v in PERM.items() if k != "account")


class CFError(Exception):
    pass


# ---------------------------------------------------------------- token + client

def read_token(path):
    """The API token from a root-owned regular file that nobody else can read. Refuses anything looser."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise CFError(f"{path} doesn't exist. Run: sudo bash {sb.SHARE_DIR}/cloudflare-setup.sh") from None
    except OSError as e:
        raise CFError(f"can't open {path} ({e.strerror}; it must be a plain file, not a symlink)") from None
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise CFError(f"{path} must be a regular file")
        if st.st_uid != os.geteuid():
            raise CFError(f"{path} must be owned by root")
        if st.st_mode & 0o077:
            raise CFError(f"{path} is mode {stat.S_IMODE(st.st_mode):o}; it must be 600. Fix: sudo chmod 600 {path}")
        tok = f.read(4096).strip()
    if not TOKEN_RE.fullmatch(tok):
        raise CFError(f"{path} doesn't hold a Cloudflare API token (expected one line of letters/digits)")
    return tok


class Client:
    def __init__(self, token, base=None, timeout=30):
        self._auth = "Bearer " + token  # kept off repr/str; never printed
        self.base = (base or API).rstrip("/")
        self.timeout = timeout

    def __repr__(self):
        return f"<cloudflare.Client {self.base}>"

    def call(self, method, path, body=None, params=None, need="account"):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": self._auth, "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "sharedbox-admin"})
        code = 0
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                code, raw = r.status, r.read(8 << 20)
        except urllib.error.HTTPError as e:
            code, raw = e.code, e.read(1 << 20)
        except (urllib.error.URLError, OSError) as e:
            raise CFError(f"can't reach the Cloudflare API ({getattr(e, 'reason', e)})") from None
        try:
            doc = json.loads(raw or b"{}")
        except ValueError:
            doc = {}
        errs = "; ".join(f"{x.get('code', '')} {x.get('message', '')}".strip()
                         for x in (doc.get("errors") or []) if isinstance(x, dict))[:400]
        if "not_enabled" in errs:  # Zero Trust/Access not turned on yet (comes back as an error, sometimes 403)
            raise CFError(f"not_enabled: {errs}", code)
        if code in (401, 403) or any(str(x.get("code")) in ("10000", "9109") for x in (doc.get("errors") or [])
                                     if isinstance(x, dict)):
            raise CFError(f"Cloudflare refused {method} {path.split('?')[0]} (HTTP {code}: {errs or 'forbidden'}).\n"
                          f"The API token is probably missing this permission: {PERM.get(need, need)}\n"
                          f"Edit the token at https://dash.cloudflare.com/profile/api-tokens; it needs:\n"
                          f"{PERMISSIONS_HELP}")
        if not (200 <= code < 300) or not doc.get("success"):
            raise CFError(f"Cloudflare API error on {method} {path.split('?')[0]} (HTTP {code}): {errs or 'unknown'}",
                          code)
        return doc.get("result"), doc.get("result_info") or {}

    def get(self, path, params=None, need="account"):
        return self.call("GET", path, params=params, need=need)[0]

    def all(self, path, params=None, need="account", per_page=50):
        out, page = [], 1
        while True:
            res, info = self.call("GET", path, params=dict(params or {}, page=page, per_page=per_page), need=need)
            out += res or []
            total = info.get("total_pages") or (
                -(-int(info.get("total_count") or 0) // per_page) if info.get("total_count") is not None else 1)
            if page >= total or not res or page > 200:
                return out
            page += 1


def client(cfg):
    return Client(read_token(cfg["CF_TOKEN_FILE"]))


# ---------------------------------------------------------------- pieces of setup

def normalize_host(h):
    h = (h or "").strip().lower()
    h = re.sub(r"^https?://", "", h).split("/")[0].rstrip(".")
    if not HOST_RE.fullmatch(h):
        raise CFError(f"{h!r} isn't a hostname like box.example.com")
    return h


def find_account(c, cfg, host=None):
    acct = (cfg.get("CF_ACCOUNT_ID") or "").strip()
    if acct:
        return acct
    accts = c.all("/accounts", need="account")
    if len(accts) == 1:
        return accts[0]["id"]
    if not accts and host:
        # tokens without "Account Settings: Read" list no accounts; the zone tells us its account
        labels = host.split(".")
        for i in range(len(labels) - 1):
            zones = c.all("/zones", params={"name": ".".join(labels[i:])}, need="zone")
            ids = {(z.get("account") or {}).get("id") for z in zones} - {None}
            if len(ids) == 1:
                return ids.pop()
    if not accts:
        raise CFError("the API token can't see any Cloudflare account. Scope it to your account "
                      f"(it needs: {PERM['tunnel']}, ...)")
    names = ", ".join(f"{a.get('name')} ({a.get('id')})" for a in accts)
    raise CFError(f"the token sees {len(accts)} accounts: {names}. Set CF_ACCOUNT_ID=<id> in {sb.CONF_PATH}")


def team_names(host):
    """Candidate Zero Trust team names, from the hostname's domain (example.org -> example, example-treehouse, ...)."""
    base = re.sub(r"[^a-z0-9-]", "-", host.split(".")[-2] if host.count(".") >= 1 else host).strip("-")[:40] or "team"
    return [base, f"{base}-treehouse", f"{base}-box"] + [f"{base}-treehouse-{secrets.token_hex(2)}" for _ in range(3)]


def enable_zero_trust(c, acct, host, log):
    """Turn on Cloudflare Zero Trust (Access) for the account, picking a free team name. Free plan, no card."""
    last = ""
    for team in team_names(host):
        try:
            org, _ = c.call("POST", f"/accounts/{acct}/access/organizations",
                            {"name": "treehouse", "auth_domain": f"{team}.cloudflareaccess.com"}, need="org")
            log(f"Zero Trust: turned on with team name {team}")
            return org
        except CFError as e:
            if "refused" in str(e):
                raise
            last = str(e)
            if "auth_domain_not_available" not in last:
                break
    raise CFError("couldn't turn on Cloudflare Zero Trust automatically (" + last[:200] + "). Open "
                  "https://one.dash.cloudflare.com/ once, pick a team name and the Free plan, then run this again.")


def team_domain(c, acct, host=None, log=print):
    try:
        org = c.get(f"/accounts/{acct}/access/organizations", need="org")
    except CFError as e:
        if "refused" in str(e):
            raise
        if host and "not_enabled" in str(e):
            org = enable_zero_trust(c, acct, host, log)
        else:
            raise CFError("Cloudflare Zero Trust isn't set up on this account yet. Open https://one.dash.cloudflare.com/ "
                          "once, pick a team name and the Free plan, then run this again.") from None
    dom = (org or {}).get("auth_domain") or ""
    if not dom:
        raise CFError("Zero Trust has no team domain yet: open https://one.dash.cloudflare.com/ and pick a team name")
    return dom.lower()


def find_zone(c, acct, host):
    labels = host.split(".")
    for i in range(len(labels) - 1):
        name = ".".join(labels[i:])
        zones = c.all("/zones", params={"name": name}, need="zone")
        zones = [z for z in zones if (z.get("account") or {}).get("id") in (None, acct)]
        if zones:
            return zones[0]["id"], name
    raise CFError(f"no Cloudflare zone for {host}. Add the domain to Cloudflare first (its nameservers must point "
                  "to Cloudflare), or check the token's Zone > Zone > Read covers it.")


def ensure_tunnel(c, acct, name, log):
    found = [t for t in c.all(f"/accounts/{acct}/cfd_tunnel", params={"name": name, "is_deleted": "false"},
                              need="tunnel") if t.get("name") == name and not t.get("deleted_at")]
    if found:
        t = found[0]
        if t.get("config_src", "cloudflare") != "cloudflare":
            raise CFError(f"tunnel {name} exists but is locally managed; delete it in the dashboard or rename it")
        log(f"tunnel {name}: reusing {t['id']}")
        return t["id"]
    t = c.call("POST", f"/accounts/{acct}/cfd_tunnel", {"name": name, "config_src": "cloudflare"}, need="tunnel")[0]
    log(f"tunnel {name}: created {t['id']}")
    return t["id"]


def set_ingress(c, acct, tid, host, port, log):
    cfg = {"config": {"ingress": [
        {"hostname": host, "service": f"http://127.0.0.1:{int(port)}", "originRequest": {}},
        {"service": "http_status:404"}]}}
    c.call("PUT", f"/accounts/{acct}/cfd_tunnel/{tid}/configurations", cfg, need="tunnel")
    log(f"tunnel ingress: {host} -> http://127.0.0.1:{port}, everything else 404")


def ensure_dns(c, zone, host, tid, log):
    target = f"{tid}.cfargotunnel.com"
    recs = [r for r in c.all(f"/zones/{zone}/dns_records", params={"name": host}, need="dns")
            if str(r.get("name", "")).lower() == host]
    body = {"type": "CNAME", "name": host, "content": target, "proxied": True, "ttl": 1,
            "comment": "treehouse (sharedbox): Cloudflare Tunnel"}
    for r in recs:
        if r.get("type") != "CNAME":
            raise CFError(f"{host} already has a {r.get('type')} record; pick another hostname or delete it")
        if str(r.get("content", "")).lower() == target and r.get("proxied"):
            log(f"DNS: {host} CNAME {target} (already there)")
            return
        if not str(r.get("content", "")).lower().endswith(".cfargotunnel.com"):
            raise CFError(f"{host} already points to {r.get('content')}; pick another hostname or delete that record")
        c.call("PUT", f"/zones/{zone}/dns_records/{r['id']}", body, need="dns")
        log(f"DNS: {host} CNAME updated to {target}")
        return
    c.call("POST", f"/zones/{zone}/dns_records", body, need="dns")
    log(f"DNS: {host} CNAME {target} (proxied) created")


def login_methods(c, acct, log):
    """IdP ids for the app's login page: one-time PIN by email (created if missing and allowed), plus Google
    if the owner configured it. None = leave allowed_idps unset (Cloudflare then offers every IdP)."""
    try:
        idps = c.all(f"/accounts/{acct}/access/identity_providers", need="org")
    except CFError as e:
        log(f"warning: can't list login methods ({str(e).splitlines()[0]}); keeping Cloudflare's defaults")
        return None
    otp = [i["id"] for i in idps if i.get("type") == "onetimepin"]
    if not otp:
        try:
            otp = [c.call("POST", f"/accounts/{acct}/access/identity_providers",
                          {"name": "One-time PIN", "type": "onetimepin", "config": {}}, need="org")[0]["id"]]
            log("login: added One-time PIN (a code by email)")
        except CFError:
            log("note: couldn't add the One-time PIN login (needs Access: Organizations, Identity Providers, and "
                "Groups Edit). If friends see no email-code option, enable it once in Zero Trust > Settings > "
                "Authentication > Login methods > Add new > One-time PIN.")
            return None
    google = [i["id"] for i in idps if i.get("type") in ("google", "google-apps")]
    log("login: email one-time code" + (" or Google" if google else ""))
    return otp + google


POLICY_FIELDS = ("name", "decision", "include", "exclude", "require", "session_duration", "approval_required",
                 "approval_groups", "isolation_required", "purpose_justification_required",
                 "purpose_justification_prompt", "mfa_config")


def policy_name(host):
    return f"treehouse {host} friends"


def ensure_policy(c, acct, host, owners, hours, log):
    name = policy_name(host)
    found = [p for p in c.all(f"/accounts/{acct}/access/policies", need="access") if p.get("name") == name]
    if found:
        log(f"Access policy '{name}': reusing {found[0]['id']}")
        return found[0]["id"]
    body = {"name": name, "decision": "allow", "include": [{"email": {"email": e}} for e in owners],
            "session_duration": f"{int(hours)}h"}
    p = c.call("POST", f"/accounts/{acct}/access/policies", body, need="access")[0]
    log(f"Access policy '{name}': created {p['id']} (allow: {', '.join(owners)})")
    return p["id"]


def ensure_app(c, acct, host, policy_id, idps, hours, log):
    apps = [a for a in c.all(f"/accounts/{acct}/access/apps", need="access")
            if a.get("type") == "self_hosted" and str(a.get("domain", "")).lower() == host]
    body = {"name": f"treehouse {host}", "domain": host, "type": "self_hosted",
            "session_duration": f"{int(hours)}h", "app_launcher_visible": False,
            "policies": [{"id": policy_id, "precedence": 1}]}
    if idps:
        body["allowed_idps"] = idps
        body["auto_redirect_to_identity"] = len(idps) == 1
    if apps:
        app = apps[0]
        # keep policies the owner attached by hand; ours goes first
        others = [p for p in (app.get("policies") or []) if isinstance(p, dict) and p.get("id") != policy_id]
        body["policies"] += [{"id": p["id"], "precedence": i + 2} for i, p in enumerate(others) if p.get("id")]
        app = c.call("PUT", f"/accounts/{acct}/access/apps/{app['id']}", body, need="access")[0]
        log(f"Access app for {host}: updated {app['id']}")
    else:
        app = c.call("POST", f"/accounts/{acct}/access/apps", body, need="access")[0]
        log(f"Access app for {host}: created {app['id']}")
    if not app.get("aud"):
        raise CFError("Cloudflare didn't return the Access app's AUD tag")
    return app["id"], app["aud"]


def tunnel_token(c, acct, tid):
    tok = c.get(f"/accounts/{acct}/cfd_tunnel/{tid}/token", need="tunnel")
    if not isinstance(tok, str) or not re.fullmatch(r"[A-Za-z0-9+/=_-]{40,4096}", tok):
        raise CFError("Cloudflare returned an unexpected tunnel token")
    return tok


def write_secret(path, line):
    """Write a root-only (0600) file atomically. The content is never printed."""
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o755, exist_ok=True)
    tmp = os.path.join(d, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(line + "\n")
            os.fchmod(f.fileno(), 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def setup(cfg, c, host, owners, log=print):
    """Idempotent: create or reuse everything, write the CF_* config. Returns the config updates."""
    host = normalize_host(host)
    owners = sorted({o.strip().lower() for o in owners if o.strip()})
    for o in owners:
        if not sb.EMAIL_RE.fullmatch(o):
            raise CFError(f"{o!r} isn't an email address")
    if not owners:
        raise CFError("give the owner's email (--owner-email you@example.com): it's always allowed in")
    port = sb.cfg_int(cfg, "GATE_PORT")
    hours = max(1, min(int(cfg.get("CF_SESSION_HOURS") or 720), 8760))
    acct = find_account(c, cfg, host)
    log(f"account: {acct}")
    team = team_domain(c, acct, host, log)
    log(f"Zero Trust team domain: {team}")
    zone, zname = find_zone(c, acct, host)
    log(f"zone: {zname}")
    tid = ensure_tunnel(c, acct, f"treehouse-{host}", log)
    set_ingress(c, acct, tid, host, port, log)
    ensure_dns(c, zone, host, tid, log)
    idps = login_methods(c, acct, log)
    pid = ensure_policy(c, acct, host, owners, hours, log)
    app_id, aud = ensure_app(c, acct, host, pid, idps, hours, log)
    updates = {"PUBLIC_URL": f"https://{host}", "PUBLIC_MODE": "cloudflare", "CF_ACCOUNT_ID": acct,
               "CF_TEAM_DOMAIN": team, "CF_ACCESS_AUD": aud, "CF_TUNNEL_ID": tid, "CF_ACCESS_APP_ID": app_id,
               "CF_ACCESS_POLICY_ID": pid, "CF_OWNER_EMAILS": ",".join(owners)}
    # the owner is always in, even if the policy existed before with other owners
    update_policy(dict(cfg, **updates), c, add=owners)
    write_secret(cfg["CF_TUNNEL_ENV"], "TUNNEL_TOKEN=" + tunnel_token(c, acct, tid))
    log(f"tunnel token saved to {cfg['CF_TUNNEL_ENV']} (root only, 600)")
    # last: the box switches to cloudflare mode only once everything on Cloudflare's side worked
    sb.set_config(updates)
    log(f"wrote CF_* settings to {sb.CONF_PATH}")
    return updates


# ---------------------------------------------------------------- the managed allow list

def _lockfile():
    return open(os.path.join(sb.STATE_DIR, ".cf-access.lock"), "a")


def policy_emails(policy):
    out = []
    for r in (policy or {}).get("include") or []:
        if isinstance(r, dict) and set(r) == {"email"} and isinstance(r["email"], dict):
            out.append(str(r["email"].get("email", "")).strip().lower())
    return out


def _policy_path(cfg):
    acct, pid = cfg.get("CF_ACCOUNT_ID", ""), cfg.get("CF_ACCESS_POLICY_ID", "")
    if not (ID_RE.fullmatch(acct) and ID_RE.fullmatch(pid)):
        raise CFError("CF_ACCOUNT_ID / CF_ACCESS_POLICY_ID aren't set: run sudo sharedbox-admin cloudflare setup")
    return f"/accounts/{acct}/access/policies/{pid}"


def get_policy(cfg, c):
    return c.get(_policy_path(cfg), need="access")


def update_policy(cfg, c, add=(), remove=()):
    """Read-modify-write the managed include list under a lock. Returns the emails now allowed."""
    path = _policy_path(cfg)
    owners = set(sb.owner_emails(cfg))
    add = {e.strip().lower() for e in add if e and e.strip()}
    remove = {e.strip().lower() for e in remove if e and e.strip()} - owners - add
    for e in add:
        if not sb.EMAIL_RE.fullmatch(e) or len(e) > 254:
            raise CFError(f"{e!r} isn't an email address")
    with _lockfile() as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        pol = c.get(path, need="access")
        include = pol.get("include") or []
        new, seen = [], set()
        for r in include:
            if isinstance(r, dict) and set(r) == {"email"} and isinstance(r["email"], dict):
                e = str(r["email"].get("email", "")).strip().lower()
                if e in remove or e in seen:
                    continue
                seen.add(e)
                new.append({"email": {"email": e}})
            else:
                new.append(r)  # domain / group / IdP rules the owner added by hand stay exactly as they were
        for e in sorted(owners | add):
            if e not in seen:
                new.append({"email": {"email": e}})
                seen.add(e)
        if not new:
            raise CFError("refusing to write an empty Access policy (nobody could log in)")
        limit = sb.cfg_int(cfg, "CF_SEAT_LIMIT")
        if limit and len(seen) > limit and add - set(policy_emails(pol)):
            # Zero Trust Free is 50 seats; staying well under means the owner never gets a bill
            raise CFError(f"refusing to allow more than CF_SEAT_LIMIT={limit} emails (Cloudflare's free plan is 50 "
                          "users). Remove someone first: sharedbox-admin access remove EMAIL")
        if new != include:
            body = {k: pol[k] for k in POLICY_FIELDS if k in pol and pol[k] is not None}
            body["include"] = new
            c.call("PUT", path, body, need="access")
            now = set(policy_emails(c.get(path, need="access")))
            if not (add | owners) <= now or now & remove:
                raise CFError("the Access policy didn't take the change; check it in the Zero Trust dashboard")
        return sorted(seen)


def set_seat_expiry(cfg, c, hours="2190h"):
    """Free the Access seat of anyone who hasn't logged in for `hours` (default ~3 months), so seats never pile up."""
    acct = cfg.get("CF_ACCOUNT_ID", "")
    org = c.get(f"/accounts/{acct}/access/organizations", need="access")
    if org.get("user_seat_expiration_inactive_time") == hours:
        return False
    body = {k: org[k] for k in ("name", "auth_domain") if org.get(k)}
    body["user_seat_expiration_inactive_time"] = hours
    c.call("PUT", f"/accounts/{acct}/access/organizations", body, need="access")
    return True


def seats_in_use(cfg, c):
    """Emails currently holding a Zero Trust seat (each one counts toward the free plan's 50)."""
    acct = cfg.get("CF_ACCOUNT_ID", "")
    return [u.get("email") for u in c.all(f"/accounts/{acct}/access/users", need="access")
            if u.get("access_seat") or u.get("gateway_seat")]
