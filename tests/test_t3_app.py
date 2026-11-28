"""`box t3`: the pairing URL people connect to (public port, Tailscale IP) and the T3 app's link built from it.
Stdlib only, no T3 needed.
    python3 -m unittest discover -s tests -v"""

import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
TMP = tempfile.mkdtemp(prefix="sbt3-")
os.environ["HOME"] = TMP

loader = importlib.machinery.SourceFileLoader("box_t3", os.path.join(LIB, "box"))
box = importlib.util.module_from_spec(importlib.util.spec_from_loader("box_t3", loader))
_argv, sys.argv = sys.argv, ["box"]
loader.exec_module(box)
sys.argv = _argv


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class T3Urls(unittest.TestCase):
    def test_public_port_rewrite(self):
        # `t3 pair` prints T3's own (inner) port; people connect to the block's first port on the Tailscale IP
        got = box.public_t3_url("http://100.64.0.5:31009/pair#token=abc", "100.64.0.5", 31000)
        self.assertEqual(got, "http://100.64.0.5:31000/pair#token=abc")
        self.assertEqual(box.public_t3_url("http://127.0.0.1:31009/pair?token=x", "100.64.0.5", 31000),
                         "http://100.64.0.5:31000/pair?token=x")
        self.assertEqual(box.public_t3_url("not a url", "100.64.0.5", 31000), "not a url")

    def test_app_link_from_rewritten_url(self):
        url = box.public_t3_url("http://100.64.0.5:31009/pair#token=abc", "100.64.0.5", 31000)
        link = box.sbconnect.t3_app_link(url)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
        self.assertEqual(q["pairingUrl"], ["http://100.64.0.5:31000/pair#token=abc"])
        self.assertNotIn("31009", link)

    def test_portal_url_uses_ip(self):
        orig, cfg = box.sb.tailscale_ip4, dict(box.CFG)
        box.sb.tailscale_ip4 = lambda: "100.64.0.5"
        box.CFG.update(PUBLIC_URL="", WEB_PORT="8090")  # no public site: the tailnet page, by IP
        try:
            self.assertEqual(box.portal_url(), "http://100.64.0.5:8090/me")
            self.assertEqual(box.rule_fields()["ip"], "100.64.0.5")
        finally:
            box.sb.tailscale_ip4 = orig
            box.CFG.clear()
            box.CFG.update(cfg)



class AppPaired(unittest.TestCase):
    """The start card leads with "Open T3" only once the app really connected, not when a prompt was made."""

    def test_from_t3_sessions(self):
        import sqlite3
        d = os.path.join(TMP, "t3home", "userdata")
        os.makedirs(d, exist_ok=True)
        db = os.path.join(d, "statev2.sqlite")
        old = os.environ.get("T3CODE_HOME")
        os.environ["T3CODE_HOME"] = os.path.dirname(d)
        try:
            self.assertFalse(box.t3_app_paired())  # no database yet
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE auth_sessions (session_id TEXT, client_label TEXT, client_surface TEXT, "
                        "client_os TEXT, client_ip_address TEXT, client_app_version TEXT, expires_at TEXT, "
                        "last_connected_at TEXT, revoked_at TEXT)")
            me, later = box.ME.pw_name, "2999-01-01T00:00:00.000Z"
            rows = [("a", f"{me} (join page)", "web", "Linux", "100.64.0.9", "0.1.9", later, None, None),
                    ("b", f"{me} T3 app", "desktop", "macOS", "100.64.0.7", "0.1.8", later, None, "2026-01-01"),
                    ("x", f"{me} T3 app", "desktop", "macOS", "100.64.0.8", "0.1.8", "2020-01-01T00:00:00.000Z",
                     "2019-12-30T00:00:00.000Z", None)]
            con.executemany("INSERT INTO auth_sessions VALUES (?,?,?,?,?,?,?,?,?)", rows)
            con.commit()
            self.assertFalse(box.t3_app_paired())  # a browser, a revoked app session and an expired one
            con.execute("INSERT INTO auth_sessions VALUES ('c', ?, 'desktop', 'macOS', '100.64.0.7', '0.1.9', ?, "
                        "'2026-11-12T10:00:00.000Z', NULL)", (f"{me} T3 app", later))
            con.commit()
            con.close()
            self.assertTrue(box.t3_app_paired())
            self.assertEqual(box.t3_app_sessions(), [{"os": "macOS", "ip": "100.64.0.7", "version": "0.1.9",
                                                      "last": "2026-11-12"}])
        finally:
            if old is None:
                os.environ.pop("T3CODE_HOME", None)
            else:
                os.environ["T3CODE_HOME"] = old


class HelpAndRules(unittest.TestCase):
    """`box help` and the agents' rule file fill every {placeholder} from rule_fields(): a placeholder added to
    HELP.md or AGENTS.md without its field (or a stray brace) would crash `box help` / `box rules` for everyone."""

    def test_help_topics_render(self):
        topics = box.help_topics()
        self.assertIn("connect", topics)
        for name, body in topics.items():
            self.assertNotRegex(body, r"\{[a-z_]+\}", name)

    def test_rules_render(self):
        out = box.render_rules(["a notice"])
        self.assertIn("a notice", out)
        self.assertNotRegex(out, r"\{[a-z_]+\}")


if __name__ == "__main__":
    unittest.main()


class Fleet(unittest.TestCase):
    """`box t3 app-link --all`: one pairing link per machine, fetched from the others over the federation's SSH."""

    def peer(self, answers):
        calls = []

        def fake_ssh(dest, cmd, input_text=None, timeout=120):
            calls.append(cmd)
            rc, out, err = answers[cmd]
            return box.subprocess.CompletedProcess([], rc, out, err)
        orig, box.fed_ssh = box.fed_ssh, fake_ssh
        try:
            return box.fleet_peer_link("minastirith", "minastirith", {"email": "k@x"}), calls
        finally:
            box.fed_ssh = orig

    def test_new_peer(self):
        pair = "http://100.94.44.25:20300/pair#token=T"
        row, calls = self.peer({"box _portal t3-app-here": (0, '{"ok": true, "pair_url": "%s", "ts_mode": "sharee", '
                                                               '"ts_login": "k@github", "apps": []}\n' % pair, "")})
        self.assertEqual(calls, ["box _portal t3-app-here"])
        self.assertEqual((row["pair_url"], row["ts_mode"], row["ts_login"]), (pair, "sharee", "k@github"))

    def test_older_peer_falls_back(self):
        pair = "http://100.94.44.25:20300/pair#token=T"
        row, calls = self.peer({"box _portal t3-app-here": (0, '{"ok": false, "error": "unknown action"}\n', ""),
                                "box _portal t3-pair app": (0, '{"ok": true, "pair_url": "%s"}\n' % pair, "")})
        self.assertEqual(calls, ["box _portal t3-app-here", "box _portal t3-pair app"])
        self.assertEqual((row["pair_url"], row["ts_mode"]), (pair, ""))

    def test_unreachable_peer_says_why(self):
        row, _ = self.peer({"box _portal t3-app-here": (255, "", "frodo@100.94.44.25: Permission denied (publickey).")})
        self.assertNotIn("pair_url", row)
        self.assertIn("isn't connected to this machine yet", row["error"])
        row, _ = self.peer({"box _portal t3-app-here": (255, "", "ssh: connect to host 100.94.44.25: No route")})
        self.assertIn("offline or not reachable", row["error"])
        row, _ = self.peer({"box _portal t3-app-here": (0, '{"ok": false, "error": "T3 is still starting"}\n', "")})
        self.assertEqual(row["error"], "T3 is still starting")


class FedT3Link(unittest.TestCase):
    def test_magicdns_name_for_peer_links(self):
        orig = box.sb.tailscale_status
        box.sb.tailscale_status = lambda: {"Peer": {"a": {"TailscaleIPs": ["100.94.44.25", "fd7a:115c:a1e0::1"],
                                                          "DNSName": "minastirith.tail-x.ts.net."}}}
        try:
            self.assertEqual(box.ts_dns_name("100.94.44.25"), "minastirith.tail-x.ts.net")
            self.assertEqual(box.ts_dns_name("100.94.44.26"), "")
        finally:
            box.sb.tailscale_status = orig

    def test_error_line_skips_stack(self):
        out = ("ERROR\n  Linked environments is off on this machine. Turn it on in Settings → Labs.\n"
               "    at environment.runWithPeerLinks (definition) (file:///home/x/.t3/runtime/versions/0.1.13/t3:1:2)\n")
        self.assertEqual(box.t3_error_line(out), "Linked environments is off on this machine. Turn it on in Settings → Labs.")


class PairVersionSkew(unittest.TestCase):
    """A T3 started before the nightly update keeps its old version; a newer `t3 pair` then refuses its database."""

    def run_pair(self, outs, busy):
        calls = {"restart": 0}
        seq = list(outs)
        saved = (box.t3_pair_once, box.t3_db_state, box.sctl, box.t3_up, box.time.sleep)
        box.t3_pair_once = lambda ttl, label, env: seq.pop(0)
        box.t3_db_state = lambda: (busy, False)
        box.sctl = lambda *a, **k: calls.__setitem__("restart", calls["restart"] + (a[0] == "restart"))
        box.t3_up = lambda host, env: True
        box.time.sleep = lambda s: None
        try:
            return box.t3_pair_run("100.64.0.5", {"T3CODE_PORT": "31009"}, "30m", "x", {}), calls["restart"]
        finally:
            box.t3_pair_once, box.t3_db_state, box.sctl, box.t3_up, box.time.sleep = saved

    SKEW = (None, "ERROR (#2): ServerOwnsDatabaseError: Another T3 Code process ... owns /home/k/.t3/userdata")

    def test_idle_restarts_and_retries(self):
        got, restarts = self.run_pair([self.SKEW, ("http://100.64.0.5:31009/pair#token=a", "")], busy=False)
        self.assertEqual((got, restarts), (("http://100.64.0.5:31009/pair#token=a", ""), 1))

    def test_never_restarts_under_a_working_agent(self):
        (url, why), restarts = self.run_pair([self.SKEW], busy=True)
        self.assertEqual((url, restarts), (None, 0))
        self.assertIn("an agent is working in it", why)

    def test_other_failures_untouched(self):
        (url, why), restarts = self.run_pair([(None, "something else")], busy=False)
        self.assertEqual((url, why, restarts), (None, "", 0))
