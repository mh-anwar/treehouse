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
TMP = tempfile.mkdtemp(prefix="sbt3-")
CONF = os.path.join(TMP, "sharedbox.conf")
with open(CONF, "w") as f:
    f.write("WEB_PORT=8090\n")
os.environ.update(SHAREDBOX_CONF=CONF, SHAREDBOX_STATE=os.path.join(TMP, "state"), SHAREDBOX_LIB=LIB,
                  SHAREDBOX_SHARE=os.path.join(ROOT, "share"), HOME=TMP)
sys.path.insert(0, LIB)

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


if __name__ == "__main__":
    unittest.main()


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
            con.execute("CREATE TABLE auth_sessions (session_id TEXT, client_label TEXT, revoked_at TEXT)")
            con.execute("INSERT INTO auth_sessions VALUES ('a', ?, NULL)", (f"{box.ME.pw_name} (join page)",))
            con.execute("INSERT INTO auth_sessions VALUES ('b', ?, '2026-01-01')", (f"{box.ME.pw_name} T3 app",))
            con.commit()
            self.assertFalse(box.t3_app_paired())  # only a browser and a revoked app session
            con.execute("INSERT INTO auth_sessions VALUES ('c', ?, NULL)", (f"{box.ME.pw_name} T3 app",))
            con.commit()
            con.close()
            self.assertTrue(box.t3_app_paired())
        finally:
            if old is None:
                os.environ.pop("T3CODE_HOME", None)
            else:
                os.environ["T3CODE_HOME"] = old
