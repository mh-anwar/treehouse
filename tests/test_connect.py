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
import html
import re
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
TMP = tempfile.mkdtemp(prefix="sbconnect-")
CONF, SITE = sbtestenv.CONF, sbtestenv.SITE

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
        self.assertEqual(cf.ssh_hostname({}, "treehouse.anwar.app", "anwar.app"), "treehouse-ssh.anwar.app")
        self.assertEqual(cf.ssh_hostname({}, "example.com", "example.com"), "ssh.example.com")

    def test_config_wins_and_off(self):
        self.assertEqual(cf.ssh_hostname({"SSH_HOST": "SSH.Treehouse.anwar.app"}, "treehouse.anwar.app", "anwar.app"),
                         "ssh.treehouse.anwar.app")
        self.assertEqual(cf.ssh_hostname({"SSH_HOST": "off"}, "treehouse.anwar.app", "anwar.app"), "")
        with self.assertRaises(cf.CFError):
            cf.ssh_hostname({"SSH_HOST": "not a host"}, "treehouse.anwar.app", "anwar.app")


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
        self.assertIn('id="connect-copy"', out)
        self.assertIn('data-copy="connect-prompt"', out)
        self.assertIn('<textarea id="connect-prompt" readonly hidden>', out)  # copied, not shown inline
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

    def test_portal_page_has_it(self):
        w = self.web
        w.portal_call = lambda *a, **k: {"ok": True, "logins": {}, "t3": None}
        who = w.cf_who("friend@example.com", "jwt")
        html_out = w.portal_page("gate", who, "sam", self.friend)
        self.assertIn("connect-prompt", html_out)


    # ---- the start card on /_box/me ----

    def render(self, logins, t3=True, ts_id="n1", app=False, result=None, welcome=False):
        w = self.web
        rec = dict(self.friend, **({"ts_id": ts_id} if ts_id else {}))

        def portal_call(ip, who, action, **kw):
            if action == "status":
                return {"ok": True, "ssh": True, "servers": [],
                        "logins": {k: {"label": k, "ok": v} for k, v in logins.items()},
                        "t3": {"url": "http://100.64.0.5:31000/", "running": True, "app": app} if t3 else None}
            if action == "shared-ls":
                return {"ok": True, "servers": []}
            return {"ok": False, "error": "unexpected"}
        w.portal_call = portal_call
        who = w.cf_who("friend@example.com", "jwt")
        return w.portal_page("gate", who, "sam", rec, result=result, welcome=welcome)

    @staticmethod
    def start_mains(page):
        """The start card's main buttons: plain <button>s and a.btn links (not secondary, small or text-styled)."""
        start = page[page.index("id='start'"):page.index("</section>")]
        start = re.sub(r"<div class='narrow'>.*?<!--/narrow-->", "", start, flags=re.S)  # phones only
        return re.findall(r"<button(?![^>]*class=)[^>]*>([^<]*)</button>|<a class='btn' [^>]*>([^<]*)</a>", start)

    def test_start_states_one_main_button(self):
        w = self.web
        old = w.SELF_IP
        w.SELF_IP = "100.64.0.5"
        try:
            cases = [  # (logins, t3, ts_id, app, main button, text)
                ({"claude": True}, False, "n1", False, None, "T3 isn't set up on this box yet"),
                ({"claude": False, "codex": False}, True, "n1", False, "Log in with Claude", "Log in with ChatGPT"),
                ({"claude": True}, True, "n1", False, "Make my setup prompt", "No agent on your laptop?"),
                ({"claude": True}, True, "", False, "Make my setup prompt", "No agent on your laptop?"),
                ({"claude": True}, True, "n1", True, "Open T3 in this browser", "App not connecting"),
            ]
            for logins, t3, ts_id, app, main, text in cases:
                page = self.render(logins, t3, ts_id, app)
                mains = [a or b for a, b in self.start_mains(page)]
                self.assertEqual(mains, [main] if main else [], (logins, t3, ts_id, app))
                self.assertIn(text, page)
                self.assertNotIn("Ask the owner to add you to Tailscale", page)
                if main == "Make my setup prompt":  # phones lead with Open T3 instead
                    self.assertRegex(page, r"<div class='narrow'><h2>Open T3</h2>.*<a class='btn' [^>]*>Open T3</a>")
                self.assertNotIn("Link my T3 account", page)  # T3 Connect stays hidden until it's tested
                self.assertNotIn("→", page)
                self.assertNotIn(f"@{w.HOST}<", page)
                self.assertNotIn(f"//{w.HOST}:", page)
            self.assertIn("Welcome, sam", self.render({"claude": False}, welcome=True))
            self.assertIn("ssh sam@100.64.0.5", self.render({"claude": True}))
        finally:
            w.SELF_IP = old

    def test_prompt_result(self):
        w = self.web
        pair = "http://100.64.0.5:31000/pair#token=abc123"
        old_ts = w.sb.tailscale_status
        w.sb.tailscale_status = lambda: {"User": {"7": {"LoginName": "sam@github"}}, "Peer": {}}
        try:
            res = w.app_prompt_result({"ok": True, "pair_url": pair, "apps": [{"os": "Linux", "last": "2026-11-12"}]},
                                      dict(self.friend, ts_id="7"))
        finally:
            w.sb.tailscale_status = old_ts
        self.assertIn("hasn't seen any device of the Tailscale account sam@github", res["app_prompt"])
        self.assertIn("expires at ", res["app_prompt"])
        self.assertIn("A T3 app paired with my T3 earlier (Linux, last connected 2026-11-12)", res["app_prompt"])
        res = w.app_prompt_result({"ok": True, "pair_url": pair})
        self.assertTrue(res["ok"])
        self.assertEqual(res["app_link"], "t3code://pair?pairingUrl=http%3A%2F%2F100.64.0.5%3A31000%2Fpair%23token%3Dabc123")
        page = self.render({"claude": True}, result=res)
        self.assertIn('id="app-prompt"', page)
        self.assertIn(f'value="{pair}"', page)
        self.assertIn('href="t3code://pair?pairingUrl=http%3A%2F%2F100.64.0.5', page)
        self.assertEqual([a or b for a, b in self.start_mains(page)], ["Copy prompt"])
        bad = w.app_prompt_result({"ok": False, "error": "T3 Code is still starting"})
        self.assertEqual(bad["error"], w.PROMPT_FAIL)
        page = self.render({"claude": True}, result=bad)
        self.assertIn(html.escape(w.PROMPT_FAIL), page)
        self.assertIn("note bad", page)
        self.assertNotIn("class='ic' aria-hidden='true'>!<", page)  # an SVG mark, not a font glyph

    def test_prompt_redirects_without_cookie(self):
        w = self.web
        w.portal_call = lambda ip, who, action, **kw: (
            {"ok": True, "pair_url": "http://100.64.0.5:31000/pair#token=t"} if action == "t3app" else {"ok": False})
        who = w.cf_who("friend@example.com", "jwt")
        h = object.__new__(w.GateHandler)
        h.headers = email_parser.parsestr("\r\n")
        sent = {}
        h.redirect = lambda where, headers=(): sent.update(where=where, headers=list(headers))
        h.portal_post("gate", who, {"action": "t3prompt", "key": ""})
        self.assertTrue(sent["where"].startswith("/_box/me?r=") and sent["where"].endswith("#start"), sent)
        self.assertEqual(sent["headers"], [], sent)  # "set up" comes from a real app session, not a cookie

    def test_gate_flags(self):
        r = self.request("GET", "/_box/features")  # any page: who() reads the gate's flags
        self.assertEqual(r["code"], 200)
        h = object.__new__(self.web.GateHandler)
        h.headers = email_parser.parsestr("X-Sharedbox-Cf-Email: friend@example.com\r\nX-Sharedbox-Cf-Jwt: j\r\n"
                                          "X-Sharedbox-Direct: 1\r\n\r\n")
        who = h.who()
        self.assertEqual((who.get("app"), who["direct"]), (None, True))


class Boxes(unittest.TestCase):
    """The Boxes card: /api/box answers only boxes listed in BOXES, a dead box never slows the owner's page,
    and the old tailnet page (/ and /api/state: everyone's usage) is for the owner only."""
    OWNER = {"id": "111", "login": "me@github", "display": "Me", "node": "laptop"}
    FRIEND = {"id": "222", "login": "sam@github", "display": "Sam", "node": "sam-laptop"}

    @classmethod
    def setUpClass(cls):
        cls.w = load_script("sharedbox-web", "sbweb_boxes")

    def setUp(self):
        w = self.w
        self._orig = (w.sb.load_config, w.peer_url, w.SELF_IP, w.OWNER_TS_ID, w.PEER_TIMEOUT)
        w.SELF_IP, w.OWNER_TS_ID, w.PEER_TIMEOUT = "100.64.0.1", "111", 0.5
        w.ALLOWED_HOSTS.add("100.64.0.1")
        for x in (w.PEERS, w.BOX_LIST, w.PEER_IPS, w._box_cache):
            x.clear()
        self.servers = []

    def tearDown(self):
        w = self.w
        w.sb.load_config, w.peer_url, w.SELF_IP, w.OWNER_TS_ID, w.PEER_TIMEOUT = self._orig
        for x in (w.PEERS, w.BOX_LIST, w.PEER_IPS, w._box_cache):
            x.clear()
        for s in self.servers:
            s.close()

    def set_boxes(self, line):
        cfg = dict(self.w.CFG, BOXES=line)
        self.w.sb.load_config = lambda *a: dict(cfg)

    def get(self, path, ip="100.64.0.9", who=None):
        h = object.__new__(self.w.Handler)
        h.client_address, h.path = (ip, 40000), path
        h.headers = email_parser.parsestr("Host: 100.64.0.1:8090\r\n\r\n")
        h.ident = lambda: (ip, who)
        out = {}

        def send(code, data, ctype="text/html; charset=utf-8", headers=()):
            out.update(code=code, body=data, headers=dict(headers))
        h.send = send
        h.do_GET()
        return out

    def test_api_box_only_for_listed_boxes(self):
        w = self.w
        w.peer_url = lambda ip: "http://127.0.0.1:9/api/box"  # nothing listens: the fetch fails right away
        self.set_boxes("here=100.64.0.1 other=100.64.0.2")
        w.peers_tick()
        r = self.get("/api/box", ip="100.64.0.2")
        self.assertEqual(r["code"], 200)
        self.assertEqual(set(json.loads(r["body"])), {"v", "name", "version", "ncpu", "load1", "level", "mem",
                                                      "disk_free", "dashboard", "t3"})  # nothing per user
        self.assertEqual(self.get("/api/box", ip="::ffff:100.64.0.2")["code"], 200)
        self.assertEqual(self.get("/api/box", ip="100.64.0.3")["code"], 403)  # a friend's laptop on the tailnet
        self.set_boxes("here=100.64.0.1")  # taken out of BOXES: refused from the next poll on, no restart
        w.peers_tick()
        self.assertEqual(self.get("/api/box", ip="100.64.0.2")["code"], 403)

    def serve(self, code, body):
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.servers.append(srv.socket)
        return srv.server_port

    def test_dead_box_never_slows_the_page(self):
        import socket
        import time
        w = self.w
        hole = socket.socket()  # accepts the connection (backlog) but never answers: the worst kind of dead box
        hole.bind(("127.0.0.1", 0))
        hole.listen(8)
        self.servers.append(hole)
        good = self.serve(200, json.dumps({"v": 1, "name": "good", "ncpu": 8, "load1": 0.5, "mem": 40, "disk_free": 70,
                                           "version": "abc1234", "users": {"sam": [1, 2]},
                                           "dashboard": "javascript:alert(1)", "t3": "http://100.64.0.3:3773"}))
        ports = {"100.64.0.2": hole.getsockname()[1], "100.64.0.3": good, "100.64.0.4": self.serve(404, "no")}
        w.peer_url = lambda ip: f"http://127.0.0.1:{ports[ip]}/api/box"
        self.set_boxes("here=100.64.0.1 dead=100.64.0.2 good=100.64.0.3 old=100.64.0.4")
        t0 = time.time()
        w.peers_tick()
        page = w.boxes_section()
        self.assertLess(time.time() - t0, 0.3, "rendering waited for a peer")
        self.assertIn("checking", page)
        while any(p["busy"] for p in w.PEERS.values()) and time.time() - t0 < 5:
            time.sleep(0.05)
        self.assertLess(time.time() - t0, 2, "the dead box's fetch didn't time out")
        w.peers_tick()  # the next poll: nothing piles up, the dead box is asked again
        page = w.boxes_section()
        rows = {m.group(1): m.group(0) for m in re.finditer(r"<tr><td><b>([a-z]+)</b>.*?</tr>", page)}
        self.assertEqual(list(rows), ["here", "dead", "good", "old"])
        self.assertIn("this box", rows["here"])
        self.assertIn("offline", rows["dead"])
        self.assertIn("not reachable", rows["dead"])
        self.assertIn("online", rows["good"])
        self.assertIn("0.50", rows["good"])
        self.assertIn('href="http://100.64.0.3:3773"', rows["good"])
        self.assertNotIn("javascript", page)
        self.assertNotIn("sam", page)
        self.assertIn("needs a treehouse update", rows["old"])

    def test_old_tailnet_page_is_owner_only(self):
        self.assertEqual(self.get("/api/state", who=self.FRIEND)["code"], 403)
        self.assertEqual(self.get("/api/state", ip="100.64.0.1", who=None)["code"], 403)  # local users too
        r = self.get("/", who=self.FRIEND)
        self.assertEqual((r["code"], r["headers"].get("Location")), (303, "/me"))  # their own page instead
        r = self.get("/api/state", who=self.OWNER)
        self.assertEqual(r["code"], 200)
        self.assertIn("rows", json.loads(r["body"]))
        self.assertEqual(self.get("/", who=self.OWNER)["code"], 200)


class AppLink(unittest.TestCase):
    def test_round_trip(self):
        for pair in ("http://100.105.248.23:20100/pair#token=AbC-12_x", "http://100.64.0.5:31000/pair#token=a%2Bb&c=d"):
            link = sbconnect.t3_app_link(pair)
            u = urllib.parse.urlsplit(link)
            self.assertEqual((u.scheme, u.netloc), ("t3code", "pair"))
            self.assertEqual(urllib.parse.parse_qs(u.query)["pairingUrl"], [pair])
            self.assertNotIn("#", link)  # the token must not turn into the link's own fragment
        self.assertEqual(sbconnect.t3_app_link(""), "")

    def test_prompt(self):
        pair = "http://100.105.248.23:20100/pair#token=tok"
        p = sbconnect.app_prompt(pair, "https://treehouse.example.com/_box/me")
        self.assertIn("t3code://pair?pairingUrl=http%3A%2F%2F100.105.248.23%3A20100", p)
        # reachability = T3's own unauthenticated descriptor, past any HTTP proxy (a proxy's 403 isn't "reachable")
        self.assertIn("curl -s --noproxy '*' --max-time 15 http://100.105.248.23:20100/.well-known/t3/environment", p)
        self.assertIn("environmentId", p)
        self.assertIn("Add the backend at http://100.105.248.23:20100? Click Confirm.", p)
        self.assertIn("Settings, Environments, Add environment, pick Remote link, and paste this into Host: " + pair, p)
        self.assertIn("Start-Process", p)
        self.assertIn("If it still times out, stop and tell me", p)
        self.assertNotIn("invite", p)
        self.assertIn("make a new prompt at https://treehouse.example.com/_box/me", p)
        self.assertIsNone(re.search(r"\{[^}]*\}", p))  # every placeholder filled
        self.assertEqual(sbconnect.app_prompt("not a url", "x"), "")
        self.assertNotIn("What the box knows", p)  # no facts given: none made up
        self.assertNotIn("paired with my T3 earlier", p)

    def test_prompt_invariants(self):
        pair = "http://100.105.248.23:20100/pair#token=tok"
        p = sbconnect.app_prompt(pair, "https://t.example.com/_box/me", "sietchtabr")
        # an app that already works: stop before touching anything
        self.assertLess(p.index("0. Already working?"), p.index("1. Look, read-only"))
        self.assertIn("If yes, stop: it's done, change nothing.", p)
        self.assertIn("If that entry connects, stop: nothing to fix.", p)
        # diagnose (read-only) before any install or change
        first_change = min(p.index(c) for c in ("winget install", "curl -fLo", "tailscale up", "brew install"))
        self.assertLess(p.index("tailscale status --json"), first_change)
        self.assertLess(p.index("--max-time 15"), first_change)
        # no blind retry loops; the fix chain re-diagnoses instead of re-curling
        self.assertIn("Never repeat a step that failed unless you changed something since", p)
        self.assertIn("after two rounds that change nothing, stop", p)
        self.assertIn("then run 1b again and handle the new state", p)
        # the agent can't see the app: it must ask what appeared, and never burn the token in a browser
        self.assertIn("ask me what the app showed", p)
        self.assertIn("Never open the link in a web browser", p)
        self.assertIn("reopen the same link (a refused link isn't used up)", p)
        # the owner is only asked to share when this account can't see the box, with the login that will be used
        self.assertIn("Only when this account can't see the box and no share is waiting for me, add this for the "
                      "owner: \"Please share sietchtabr with my Tailscale account <login>.\"", p)
        self.assertIn("--reset", p)  # forbidden, not suggested
        self.assertIn("never use --reset or --force-reauth", p)
        # every address is the IP (or a public site), never the box's name
        for url in re.findall(r"[a-z0-9]+://[^\s'\")]+", p):
            self.assertNotIn("sietchtabr", urllib.parse.urlsplit(url).netloc, url)
        for cmd in re.findall(r"curl [^\n(]*|tailscale ping [^\n.]*|'t3code://[^']*'", p):
            self.assertNotIn("sietchtabr", cmd)

    def test_prompt_facts(self):
        pair = "http://100.105.248.23:20100/pair#token=tok"
        p = sbconnect.app_prompt(pair, "https://t.example.com/_box/me", "sietchtabr", ts_login="sam@github",
                                 ts_mode="reverse", apps=[dict(os="macOS", ip="100.68.1.2", version="0.1.9",
                                                               last="2026-11-12")], expires="2026-11-14 18:42 UTC")
        self.assertIn("NOT shared with the Tailscale account sam@github", p)
        self.assertNotIn("sign in as sam@github", p)  # not proven to have the box: don't push the human to it
        self.assertIn("go straight to the message for the owner", p)
        self.assertIn("A T3 app paired with my T3 earlier (macOS, Tailscale IP 100.68.1.2, app 0.1.9, "
                      "last connected 2026-11-12)", p)
        self.assertIn("An app paired earlier (see above) makes this likely.", p)
        self.assertIn("expires at 2026-11-14 18:42 UTC (check the clock with date -u)", p)
        for mode in ("sharee", "member"):
            q = sbconnect.app_prompt(pair, "d", ts_login="x@y", ts_mode=mode)
            self.assertIn("sign in as x@y (the account that has the box)", q)
            self.assertIn("if this computer's account is another one, that is the cause; tell me both", q)
        for mode, text in (("sharee", "is shared with the Tailscale account"), ("member", "is a member"),
                           ("none", "That's normal if Tailscale isn't set up on any computer yet"),
                           ("", "may have shared the box with the Tailscale account x@y (not confirmed)")):
            self.assertIn(text, sbconnect.app_prompt(pair, "d", ts_login="x@y", ts_mode=mode))
        for q in (p, sbconnect.app_prompt(pair, "d", "sietchtabr")):
            self.assertIsNone(re.search(r"\{[^}]*\}", q))
            self.assertNotIn("invite", q)
            self.assertIsNone(re.search(r"//sietchtabr|@sietchtabr", q))  # addresses use the IP
            self.assertIsNone(re.search(r"\bsudo\b|\b(None|null|undefined|NaN)\b|Error:|Exception", q))
            self.assertTrue(q.isascii())

    def test_pair_expires(self):
        self.assertEqual(sbconnect.pair_expires(0), "1970-01-01 00:29 UTC")

    def test_ts_facts(self):
        st = {"CurrentTailnet": {"MagicDNSSuffix": "owner.ts.net"},
              "User": {"1": {"LoginName": "owner@x"}, "2": {"LoginName": "sam@github"}, "3": {"LoginName": "kim@x"},
                       "4": {"LoginName": "lee@x"}},
              "Peer": {"a": {"UserID": 2, "DNSName": "aether.tail99.ts.net."},
                       "b": {"UserID": 3, "ShareeNode": True, "DNSName": ""},
                       "c": {"UserID": 4, "DNSName": "lap.owner.ts.net."}}}
        f = sbconnect.ts_facts
        self.assertEqual(f(st, {"ts_id": "2", "login": "old"}), {"login": "sam@github", "mode": "reverse"})
        self.assertEqual(f(st, {"ts_id": 3}), {"login": "kim@x", "mode": "sharee"})
        self.assertEqual(f(st, {"ts_id": "4"}), {"login": "lee@x", "mode": "member"})
        self.assertEqual(f(st, {"email": "Owner@X"})["mode"], "none")  # matched by email, no peers of theirs
        self.assertEqual(f(st, {"email": "nobody@x"}), {"login": "", "mode": ""})
        self.assertEqual(f(None, {"ts_id": "2", "login": "sam@github"}), {"login": "sam@github", "mode": ""})


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


if __name__ == "__main__":
    unittest.main()
