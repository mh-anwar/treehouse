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
