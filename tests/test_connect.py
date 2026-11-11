"""Tests for SSH-through-Cloudflare + the laptop-agent key API. Stdlib only, no root, no network:
    python3 -m unittest discover -s tests -v
Cloudflare is a fake client that records calls; root (the spool) is stubbed; the gate runs for real on loopback
in front of a fake sharedbox-web socket."""

import asyncio
import email.parser
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
TMP = tempfile.mkdtemp(prefix="sbtest-")
CONF = os.path.join(TMP, "sharedbox.conf")
SITE = "https://treehouse.example.com"
with open(CONF, "w") as f:
    f.write(f"PUBLIC_URL={SITE}\nPUBLIC_MODE=cloudflare\nCF_ACCOUNT_ID=acct12345\nCF_TUNNEL_ID=tunnel12345\n"
            "CF_ACCESS_POLICY_ID=policy12345\nCF_TEAM_DOMAIN=team.cloudflareaccess.com\n"
            "CF_ACCESS_AUD=aud0123456789abcdef\nCF_OWNER_EMAILS=owner@example.com\n"
            "SSH_HOST=treehouse-ssh.example.com\nCF_SSH_APP_ID=sshapp12345\n")
os.environ.update(SHAREDBOX_CONF=CONF, SHAREDBOX_STATE=os.path.join(TMP, "state"), SHAREDBOX_LIB=LIB,
                  SHAREDBOX_SHARE=os.path.join(ROOT, "share"), SHAREDBOX_WEB_SOCK=os.path.join(TMP, "web.sock"))
os.makedirs(os.path.join(TMP, "state", "gate"), exist_ok=True)
sys.path.insert(0, LIB)

import sbaccess  # noqa: E402
import sbcloudflare as cf  # noqa: E402
import sbcommon as sb  # noqa: E402
import sbconnect  # noqa: E402


def load_script(name, modname):
    loader = importlib.machinery.SourceFileLoader(modname, os.path.join(LIB, name))
    spec = importlib.util.spec_from_loader(modname, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


# ---------------------------------------------------------------- Cloudflare side

class FakeClient:
    """Records every call; answers from `routes` {(METHOD, path): result or callable(body, params)}."""

    def __init__(self, routes=None):
        self.routes, self.calls = dict(routes or {}), []

    def call(self, method, path, body=None, params=None, need="account"):
        self.calls.append((method, path, body))
        r = self.routes.get((method, path))
        if r is None:
            if method == "GET":
                raise cf.CFError(f"Cloudflare API error on GET {path} (HTTP 404): not found", 404)
            r = {"id": "new-" + path.rsplit("/", 1)[-1], "aud": "audnew"}
        if callable(r):
            r = r(body, params)
        if isinstance(r, Exception):
            raise r
        return r, {}

    def get(self, path, params=None, need="account"):
        return self.call("GET", path, params=params, need=need)[0]

    def all(self, path, params=None, need="account", per_page=50):
        return self.call("GET", path, params=params, need=need)[0] or []

    def puts(self, path):
        return [b for m, p, b in self.calls if m == "PUT" and p == path]

    def posts(self, path):
        return [b for m, p, b in self.calls if m == "POST" and p == path]


A = "acct12345"
T = "tunnel12345"
TCONF = f"/accounts/{A}/cfd_tunnel/{T}/configurations"


def base_routes(ingress=None, dns=(), apps=()):
    return {
        ("GET", "/zones"): lambda b, p: ([{"id": "zone1", "name": "example.com", "account": {"id": A}}]
                                         if (p or {}).get("name") == "example.com" else []),
        ("GET", TCONF): {"config": {"ingress": ingress}} if ingress is not None else None,
        ("GET", f"/accounts/{A}/access/identity_providers"): [{"id": "otp1", "type": "onetimepin"}],
        ("GET", "/zones/zone1/dns_records"): list(dns),
        ("GET", f"/accounts/{A}/access/apps"): list(apps),
        ("POST", f"/accounts/{A}/access/apps"): lambda b, p: dict(b, id="sshapp1", aud="audssh"),
    }


class SSHHostname(unittest.TestCase):
    def test_default_stays_under_the_free_certificate(self):
        self.assertEqual(cf.ssh_hostname({}, "treehouse.example.org", "example.org"), "treehouse-ssh.example.org")
        self.assertEqual(cf.ssh_hostname({}, "example.com", "example.com"), "ssh.example.com")

    def test_config_wins_and_off(self):
        self.assertEqual(cf.ssh_hostname({"SSH_HOST": "SSH.Treehouse.example.org"}, "treehouse.example.org", "example.org"),
                         "ssh.treehouse.example.org")
        self.assertEqual(cf.ssh_hostname({"SSH_HOST": "off"}, "treehouse.example.org", "example.org"), "")
        with self.assertRaises(cf.CFError):
            cf.ssh_hostname({"SSH_HOST": "not a host"}, "treehouse.example.org", "example.org")


class Ingress(unittest.TestCase):
    def test_new_tunnel_gets_ssh_then_http_then_404(self):
        c = FakeClient(base_routes())
        changed = cf.set_ingress(c, A, T, cf.ingress_rules("box.example.com", 8091, "box-ssh.example.com"), lambda m: None)
        self.assertTrue(changed)
        ing = c.puts(TCONF)[0]["config"]["ingress"]
        self.assertEqual([(r.get("hostname"), r["service"]) for r in ing], [
            ("box-ssh.example.com", "ssh://localhost:22"), ("box.example.com", "http://127.0.0.1:8091"),
            (None, "http_status:404")])

    def test_keeps_hand_added_rules_and_replaces_ours(self):
        old = [{"hostname": "box.example.com", "service": "http://127.0.0.1:1"},
               {"hostname": "grafana.example.com", "service": "http://127.0.0.1:3000"},
               {"service": "http_status:404"}]
        c = FakeClient(base_routes(ingress=old))
        cf.set_ingress(c, A, T, cf.ingress_rules("box.example.com", 8091, "box-ssh.example.com"), lambda m: None)
        hosts = [r.get("hostname") for r in c.puts(TCONF)[0]["config"]["ingress"]]
        self.assertEqual(hosts, ["box-ssh.example.com", "box.example.com", "grafana.example.com", None])

    def test_idempotent(self):
        rules = cf.ingress_rules("box.example.com", 8091, "box-ssh.example.com")
        c = FakeClient(base_routes(ingress=rules + [{"service": "http_status:404"}]))
        self.assertFalse(cf.set_ingress(c, A, T, rules, lambda m: None))
        self.assertEqual(c.puts(TCONF), [])

    def test_without_ssh(self):
        self.assertEqual([r["hostname"] for r in cf.ingress_rules("box.example.com", 8091)], ["box.example.com"])


class SetupSSH(unittest.TestCase):
    def setUp(self):
        self.written = []
        self._orig = sb.set_config
        sb.set_config = lambda u, path=None: self.written.append(dict(u))

    def tearDown(self):
        sb.set_config = self._orig

    def cfg(self, **kw):
        c = dict(sb.load_config(CONF), SSH_HOST="", CF_SSH_APP_ID="", PUBLIC_URL="https://box.example.com",
                 CF_SESSION_HOURS="48")
        c.update(kw)
        return c

    def test_existing_box(self):
        c = FakeClient(base_routes())
        logs = []
        up = cf.setup_ssh(self.cfg(), c, logs.append)
        self.assertEqual(up, {"SSH_HOST": "box-ssh.example.com", "CF_SSH_APP_ID": "sshapp1"})
        self.assertEqual(self.written, [up])
        dns = c.posts("/zones/zone1/dns_records")[0]
        self.assertEqual((dns["type"], dns["name"], dns["content"], dns["proxied"]),
                         ("CNAME", "box-ssh.example.com", f"{T}.cfargotunnel.com", True))
        app = c.posts(f"/accounts/{A}/access/apps")[0]
        self.assertEqual(app["domain"], "box-ssh.example.com")
        self.assertEqual(app["type"], "self_hosted")
        self.assertEqual(app["policies"], [{"id": "policy12345", "precedence": 1}])  # the website's allow list
        self.assertEqual(app["session_duration"], "48h")
        self.assertEqual(app["allowed_idps"], ["otp1"])
        ing = c.puts(TCONF)[0]["config"]["ingress"]
        self.assertEqual(ing[0], {"hostname": "box-ssh.example.com", "service": "ssh://localhost:22", "originRequest": {}})
        self.assertTrue(any("ssh" in m for m in logs))

    def test_rerun_reuses_everything(self):
        rules = cf.ingress_rules("box.example.com", 8091, "box-ssh.example.com") + [{"service": "http_status:404"}]
        dns = [{"id": "r1", "type": "CNAME", "name": "box-ssh.example.com", "content": f"{T}.cfargotunnel.com",
                "proxied": True}]
        apps = [{"id": "sshapp1", "type": "self_hosted", "domain": "box-ssh.example.com", "aud": "audssh",
                 "policies": [{"id": "policy12345"}, {"id": "handmade"}]}]
        routes = base_routes(ingress=rules, dns=dns, apps=apps)
        routes[("PUT", f"/accounts/{A}/access/apps/sshapp1")] = lambda b, p: dict(b, id="sshapp1", aud="audssh")
        c = FakeClient(routes)
        cf.setup_ssh(self.cfg(), c, lambda m: None)
        self.assertEqual(c.puts(TCONF), [])
        self.assertEqual(c.posts("/zones/zone1/dns_records"), [])
        self.assertEqual(c.posts(f"/accounts/{A}/access/apps"), [])
        put = c.puts(f"/accounts/{A}/access/apps/sshapp1")[0]
        self.assertEqual(put["policies"], [{"id": "policy12345", "precedence": 1}, {"id": "handmade", "precedence": 2}])

    def test_needs_cloudflare_mode(self):
        with self.assertRaises(cf.CFError):
            cf.setup_ssh(self.cfg(PUBLIC_MODE=""), FakeClient(), lambda m: None)

    def test_ssh_pieces_failure_never_blocks_the_site(self):
        routes = base_routes(dns=[{"id": "x", "type": "A", "name": "box-ssh.example.com", "content": "1.2.3.4"}])
        logs = []
        up = cf.ssh_pieces(FakeClient(routes), A, "zone1", "box-ssh.example.com", T, "policy12345", None, 24, logs.append)
        self.assertEqual(up, {})
        self.assertIn("cloudflare ssh", logs[-1])

    def test_full_setup_includes_ssh(self):
        orig = (cf.update_policy, cf.write_secret, cf.tunnel_token, cf.find_account, cf.team_domain)
        cf.update_policy = lambda *a, **k: []
        cf.write_secret = lambda *a: None
        cf.tunnel_token = lambda *a: "t" * 50
        cf.find_account = lambda *a: A
        cf.team_domain = lambda *a, **k: "team.cloudflareaccess.com"
        try:
            routes = base_routes()
            routes[("GET", f"/accounts/{A}/cfd_tunnel")] = [{"id": T, "name": "treehouse-box.example.com",
                                                           "config_src": "cloudflare"}]
            routes[("GET", f"/accounts/{A}/access/policies")] = [{"id": "policy12345", "name": cf.policy_name("box.example.com")}]
            c = FakeClient(routes)
            up = cf.setup(self.cfg(), c, "box.example.com", ["owner@example.com"], log=lambda m: None)
        finally:
            cf.update_policy, cf.write_secret, cf.tunnel_token, cf.find_account, cf.team_domain = orig
        self.assertEqual(up["SSH_HOST"], "box-ssh.example.com")
        self.assertEqual(up["CF_SSH_APP_ID"], "sshapp1")
        apps = c.posts(f"/accounts/{A}/access/apps")
        self.assertEqual(sorted(a["domain"] for a in apps), ["box-ssh.example.com", "box.example.com"])
        self.assertTrue(all(a["policies"][0]["id"] == "policy12345" for a in apps))
        hosts = [r.get("hostname") for r in c.puts(TCONF)[0]["config"]["ingress"]]
        self.assertEqual(hosts, ["box-ssh.example.com", "box.example.com", None])

    def test_status(self):
        cfg = self.cfg(SSH_HOST="box-ssh.example.com", CF_SSH_APP_ID="sshapp1")
        routes = {("GET", f"/accounts/{A}/access/apps/sshapp1"): {"domain": "box-ssh.example.com",
                                                                 "policies": [{"id": "policy12345"}]},
                  ("GET", TCONF): {"config": {"ingress": cf.ingress_rules("box.example.com", 8091, "box-ssh.example.com")}}}
        self.assertTrue(all(ok for ok, _ in cf.ssh_status(cfg, FakeClient(routes))))
        routes[("GET", f"/accounts/{A}/access/apps/sshapp1")] = {"domain": "box-ssh.example.com", "policies": []}
        self.assertFalse(cf.ssh_status(cfg, FakeClient(routes))[0][0])
        self.assertTrue(cf.ssh_status(self.cfg(), FakeClient())[0][0])  # off is not an error


class Common(unittest.TestCase):
    def test_ssh_host_only_when_on(self):
        cfg = sb.load_config(CONF)
        self.assertEqual(sb.ssh_host(cfg), "treehouse-ssh.example.com")
        self.assertEqual(sb.ssh_host(dict(cfg, CF_SSH_APP_ID="")), "")
        self.assertEqual(sb.ssh_host(dict(cfg, PUBLIC_MODE="")), "")
        self.assertEqual(sb.ssh_host(dict(cfg, SSH_HOST="off")), "")


# ---------------------------------------------------------------- token header (sbaccess + the real gate)

class TokenHeaders(unittest.TestCase):
    def test_sources(self):
        f = sbaccess.token_from_headers
        self.assertEqual(f([("cf-access-token", " abc ")]), "abc")
        self.assertEqual(f([("Cf-Access-Token", "abc"), ("Cookie", "CF_Authorization=zzz")]), "abc")
        self.assertEqual(f([("Cf-Access-Jwt-Assertion", "jwt"), ("cf-access-token", "abc")]), "jwt")
        self.assertIsNone(f([("cf-access-token", "a"), ("cf-access-token", "b")]))
        self.assertEqual(f([("Cookie", "x=1; CF_Authorization=zzz")]), "zzz")
        self.assertIsNone(f([]))


class FakeVerifier:
    def verify(self, token):
        if token != "good-token":
            raise sbaccess.InvalidToken("bad")
        return {"email": "friend@example.com", "exp": 9999999999}


class GateAcceptsCliToken(unittest.TestCase):
    """The real gate on loopback, a fake sharedbox-web on its unix socket that records what reaches it."""

    @classmethod
    def setUpClass(cls):
        cls.gate = load_script("sharedbox-gate", "sbgate_test")
        cls.gate.VERIFIER = FakeVerifier()

    def roundtrip(self, extra_headers):
        seen = {}

        async def web(reader, writer):
            seen["head"] = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
            await writer.drain()
            writer.close()

        async def go():
            sock = os.environ["SHAREDBOX_WEB_SOCK"]
            if os.path.exists(sock):
                os.unlink(sock)
            wsrv = await asyncio.start_unix_server(web, sock)
            gsrv = await asyncio.start_server(self.gate.client, "127.0.0.1", 0)
            port = gsrv.sockets[0].getsockname()[1]
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(("GET /_box/api/whoami HTTP/1.1\r\nHost: treehouse.example.com\r\nConnection: close\r\n"
                     + "".join(f"{k}: {v}\r\n" for k, v in extra_headers) + "\r\n").encode())
            await w.drain()
            resp = await asyncio.wait_for(r.read(), 10)
            w.close()
            gsrv.close()
            wsrv.close()
            return resp.decode("latin-1")

        return asyncio.run(go()), seen.get("head", "")

    def test_cf_access_token_header_is_accepted_and_not_forwarded(self):
        resp, head = self.roundtrip([("cf-access-token", "good-token")])
        self.assertTrue(resp.startswith("HTTP/1.1 200"), resp[:200])
        low = head.lower()
        self.assertIn("x-sharedbox-cf-email: friend@example.com", low)
        self.assertIn("x-sharedbox-cf-jwt: good-token", low)
        self.assertNotIn("cf-access-token", low)

    def test_bad_or_missing_token_is_refused(self):
        self.assertIn(" 403 ", self.roundtrip([("cf-access-token", "forged")])[0].split("\r\n")[0] + " ")
        self.assertIn(" 403 ", self.roundtrip([])[0].split("\r\n")[0] + " ")


# ---------------------------------------------------------------- sharedbox-web: API, connect.md, dashboard

def make_pubkey():
    if not shutil.which("ssh-keygen"):
        return None
    d = tempfile.mkdtemp(dir=TMP)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t@x", "-f", os.path.join(d, "k")], check=True)
    with open(os.path.join(d, "k.pub")) as f:
        return f.read().strip()


class Web(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.web = load_script("sharedbox-web", "sbweb_test")
        cls.pubkey = make_pubkey()

    def setUp(self):
        w = self.web
        self.calls = []
        self.friend = {"email": "friend@example.com", "status": "active"}
        self._orig = (w.portal_call, w.sb.user_by_email, w.rate.copy())
        w.rate.clear()
        w.sb.user_by_email = lambda e, users=None: ("sam", self.friend) if e == "friend@example.com" else (None, None)

        def portal_call(ip, who, action, **kw):
            self.calls.append((action, who.get("cf_jwt"), kw))
            return {"ok": True, "key_added": True}
        w.portal_call = portal_call

    def tearDown(self):
        w = self.web
        w.portal_call, w.sb.user_by_email = self._orig[0], self._orig[1]
        w.rate.clear()
        w.rate.update(self._orig[2])

    def request(self, method, path, headers=(), body=b"", email="friend@example.com"):
        h = object.__new__(self.web.GateHandler)
        raw = "".join(f"{k}: {v}\r\n" for k, v in headers)
        if email:
            raw += f"X-Sharedbox-Cf-Email: {email}\r\nX-Sharedbox-Cf-Jwt: jwt-of-{email}\r\n"
        if method == "POST":
            raw += f"Content-Length: {len(body)}\r\n"
        h.headers = email_parser.parsestr(raw + "\r\n")
        h.path, h.rfile = path, io.BytesIO(body)
        out = {}

        def send(code, data, ctype="text/html; charset=utf-8", headers=()):
            out.update(code=code, body=data, ctype=ctype)
        h.send = send
        getattr(h, "do_" + method)()
        return out

    def post_key(self, body, headers=(("X-Treehouse-Agent", "1"),), **kw):
        r = self.request("POST", "/_box/api/ssh-key", headers, body.encode() if isinstance(body, str) else body, **kw)
        return r["code"], (json.loads(r["body"]) if r["ctype"].startswith("application/json") else r["body"])

    def test_add_key_ok(self):
        if not self.pubkey:
            self.skipTest("no ssh-keygen")
        code, res = self.post_key(self.pubkey + "\n")
        self.assertEqual(code, 200, res)
        self.assertEqual((res["ok"], res["user"], res["ssh_host"]), (True, "sam", "treehouse-ssh.example.com"))
        self.assertIn("Host treehouse\n  HostName treehouse-ssh.example.com\n  User sam\n", res["ssh_config"])
        self.assertIn("ProxyCommand cloudflared access ssh --hostname %h", res["ssh_config"])
        # through the spool to root's add-key, carrying the Access token root re-verifies
        self.assertEqual(self.calls, [("add-key", "jwt-of-friend@example.com", {"sshkey": self.pubkey})])

    def test_json_body_too(self):
        if not self.pubkey:
            self.skipTest("no ssh-keygen")
        self.assertEqual(self.post_key(json.dumps({"key": self.pubkey}))[0], 200)

    def test_csrf_header_required(self):
        code, res = self.post_key(self.pubkey or "ssh-ed25519 AAAA", headers=())
        self.assertEqual(code, 403)
        self.assertEqual(self.calls, [])

    def test_foreign_origin_refused(self):
        for hdr in (("Origin", "https://evil.example"), ("Origin", "null"), ("Sec-Fetch-Site", "cross-site")):
            code, _ = self.post_key(self.pubkey or "x", headers=(("X-Treehouse-Agent", "1"), hdr))
            self.assertEqual(code, 403, hdr)
        self.assertEqual(self.calls, [])
        if self.pubkey:
            code, _ = self.post_key(self.pubkey, headers=(("X-Treehouse-Agent", "1"), ("Origin", SITE)))
            self.assertEqual(code, 200)

    def test_private_key_and_garbage(self):
        code, res = self.post_key("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n")
        self.assertEqual(code, 400)
        self.assertNotIn("abc", res["error"])
        self.assertEqual(self.post_key("hello")[0], 400)
        self.assertEqual(self.post_key("")[0], 400)
        self.assertEqual(self.calls, [])

    def test_non_friend_and_owner(self):
        code, res = self.post_key(self.pubkey or "x", email="stranger@example.com")
        self.assertEqual(code, 403)
        self.assertIn("invite", res["error"])
        code, res = self.post_key(self.pubkey or "x", email="owner@example.com")
        self.assertEqual(code, 403)
        self.assertIn("owner", res["error"])
        self.assertEqual(self.calls, [])

    def test_root_refusal_is_passed_on(self):
        if not self.pubkey:
            self.skipTest("no ssh-keygen")
        self.web.portal_call = lambda *a, **k: {"ok": False, "relogin": True, "error": "your Cloudflare login couldn't be verified; reload"}
        code, res = self.post_key(self.pubkey)
        self.assertEqual(code, 403)
        self.assertFalse(res["ok"])

    def test_rate_limited(self):
        if not self.pubkey:
            self.skipTest("no ssh-keygen")
        codes = [self.post_key(self.pubkey)[0] for _ in range(12)]
        self.assertEqual(codes[:10], [200] * 10)
        self.assertEqual(codes[10:], [429, 429])

    def test_whoami(self):
        r = self.request("GET", "/_box/api/whoami")
        res = json.loads(r["body"])
        self.assertEqual(r["code"], 200)
        self.assertEqual((res["user"], res["ssh_host"], res["dashboard"]),
                         ("sam", "treehouse-ssh.example.com", SITE + "/_box/me"))
        self.assertIn("ports", res)
        self.assertEqual(self.request("GET", "/_box/api/whoami", email="stranger@example.com")["code"], 403)
        self.assertEqual(self.request("GET", "/_box/api/whoami", (("Origin", "https://evil.example"),))["code"], 403)

    def test_connect_md(self):
        r = self.request("GET", "/_box/connect.md")
        self.assertEqual(r["code"], 200)
        self.assertTrue(r["ctype"].startswith("text/plain"))
        self.assertIn("User sam", r["body"])
        self.assertIn(f"cloudflared access login {SITE}", r["body"])
        # a top-level visit right after the Cloudflare login arrives cross-site: still fine for a GET
        self.assertEqual(self.request("GET", "/_box/connect.md", (("Sec-Fetch-Site", "cross-site"),))["code"], 200)
        self.assertEqual(self.request("GET", "/_box/connect.md", email="stranger@example.com")["code"], 403)

    def test_dashboard_section(self):
        out = self.web.connect_section("sam")
        self.assertIn("Use the box from your own computer's agent", out)
        self.assertIn('id="connect-copy"', out)
        self.assertIn("User sam", out)
        self.assertIn("/_box/connect.md", out)
        self.assertNotIn("<script>alert", out)

    def test_dashboard_section_when_off(self):
        orig = self.web.live_ssh_host
        self.web.live_ssh_host = lambda: ""
        try:
            out = self.web.connect_section("sam")
        finally:
            self.web.live_ssh_host = orig
        self.assertEqual(out, "")  # hidden until the owner switches it on
        self.assertNotIn("connect-copy", out)

    def test_portal_page_has_it(self):
        w = self.web
        w.portal_call = lambda *a, **k: {"ok": True, "logins": {}, "t3": None}
        who = w.cf_who("friend@example.com", "jwt")
        html_out = w.portal_page("gate", who, "sam", self.friend)
        self.assertIn("connect-prompt", html_out)


email_parser = email.parser.Parser(_class=__import__("http.client").client.HTTPMessage)


class ConnectText(unittest.TestCase):
    def test_complete(self):
        t = sbconnect.connect_text("sam", SITE + "/", "treehouse-ssh.example.com")
        for must in ("brew install cloudflared", "winget install --id Cloudflare.cloudflared",
                     "cloudflared-linux-amd64", "cloudflared --version",
                     'ssh-keygen -t ed25519 -f ~/.ssh/treehouse -N "" -C "sam@treehouse"',
                     f"cloudflared access login {SITE}\n",
                     f"curl -sS -X POST {SITE}/_box/api/ssh-key -H \"X-Treehouse-Agent: 1\"",
                     f'"cf-access-token: $(cloudflared access token -app={SITE})"',
                     '--data-binary @"$HOME/.ssh/treehouse.pub"',  # not @~/...: curl doesn't expand ~
                     "Invoke-RestMethod", "Host treehouse\n  HostName treehouse-ssh.example.com\n  User sam\n",
                     "IdentityFile ~/.ssh/treehouse", "ProxyCommand cloudflared access ssh --hostname %h",
                     "cloudflared.exe\" access ssh --hostname %h", "ServerAliveInterval 30",
                     "ssh -o StrictHostKeyChecking=accept-new treehouse 'box status'", "ssh -t treehouse claude",
                     "Remote-SSH", "box rules", "box serve", "box shared", "no sudo"):
            self.assertIn(must, t)
        self.assertNotIn("{", t.replace("{{", "").replace('@{"X-Treehouse', "").replace('{"ok"', ""))

    def test_off(self):
        t = sbconnect.connect_text("sam", SITE, "")
        self.assertIn("sudo sharedbox-admin cloudflare ssh", t)
        self.assertNotIn("ProxyCommand", t)


class HelpTopic(unittest.TestCase):
    def test_help_has_connect_and_formats(self):
        with open(os.path.join(ROOT, "share", "HELP.md")) as f:
            raw = f.read()
        topics = {c.partition("\n")[0].strip(): c.partition("\n")[2] for c in raw.split("@@ ")[1:]}
        self.assertIn("connect", topics)
        fields = {k: "X" for k in ("portal", "user", "host", "connect", "project_ttl", "warn", "cache_ttl",
                                   "artifact_ttl", "tmp_ttl", "slots", "jobs", "updates", "reboot_time",
                                   "reboot_days", "ports", "shared_dir", "shared_ports",
                                   "serve_public", "shared_public", "archive_days", "t3_idle", "t3_nap", "t3_ports", "docker_nap")}
        for name, body in topics.items():
            body.format(**fields)  # no stray braces


if __name__ == "__main__":
    unittest.main()
