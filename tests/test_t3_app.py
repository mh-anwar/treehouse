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
