"""The clickable demo (tests/demo_site.py) still starts and works: every person's dashboard and tabs load through
the real gate and web with the demo banner, a button changes state and Reset demo puts it back, outside links go
to the demo's explanation page. Stdlib only (no browser); tests/demo_walk.py is the full browser walk."""
import http.cookiejar
import os
import re
import socket
import subprocess
import sys
import time
import unittest
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, "demo_site.py")
TABS = {"home": "data-card='people'", "messages": "data-card='messages'", "servers": "data-card='servers'",
        "machines": "Machines", "account": "data-card='accounts'"}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class DemoSite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        port = free_port()
        cls.base = f"http://127.0.0.1:{port}"
        cls.proc = subprocess.Popen([sys.executable, DEMO, "--host", "127.0.0.1", "--port", str(port)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(300):
            try:
                urllib.request.urlopen(cls.base + "/_demo", timeout=2)
                return
            except OSError:
                if cls.proc.poll() is not None:
                    raise RuntimeError("demo didn't start: " + cls.proc.stderr.read().decode()[-3000:])
                time.sleep(0.1)
        cls.proc.kill()
        raise RuntimeError("demo didn't start in 30 s")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(30)
        cls.proc.stderr.close()

    def browser(self):
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, op, path):
        with op.open(self.base + path, timeout=60) as r:
            return r.status, r.geturl(), r.read().decode()

    def as_(self, persona):
        op = self.browser()
        self.get(op, f"/_demo/as/{persona}")
        return op

    def assertPage(self, body, where):
        self.assertIn("Demo: fake data, nothing here is real.", body, where)
        for bad in ("Traceback", "bad origin", "treehouse.demo", "https://claude.ai", "https://t.me/"):
            self.assertNotIn(bad, body, where)

    def test_picker_lists_everyone(self):
        _, _, body = self.get(self.browser(), "/_demo")
        for name in ("Alice", "Bob", "Dave", "Erin", "Owner", "Stranger"):
            self.assertIn(f"Continue as {name}", body)
        self.assertIn("Reset demo", body)

    def test_no_persona_goes_to_the_picker(self):
        _, url, _ = self.get(self.browser(), "/_box/me")
        self.assertTrue(url.endswith("/_demo"), url)

    def test_every_person_and_tab_loads(self):
        for persona in ("alice", "dave", "erin", "owner"):
            op = self.as_(persona)
            for tab, marker in TABS.items():
                code, _, body = self.get(op, f"/_box/me?tab={tab}")
                self.assertEqual(code, 200, (persona, tab))
                self.assertPage(body, (persona, tab))
                self.assertIn(marker, body, (persona, tab))
        bob = self.browser()
        _, _, body = self.get(bob, "/_demo/as/bob")  # his first visit: the welcome page
        self.assertIn("Welcome", body)
        _, _, body = self.get(bob, "/_box/me")  # nothing else until he signs in to an AI
        self.assertPage(body, "bob")
        self.assertIn("Log in with Claude", body)
        _, _, body = self.get(self.as_("stranger"), "/")
        self.assertPage(body, "stranger")
        self.assertIn("no account", body)

    def test_status_changes_and_reset_restores(self):
        op = self.as_("alice")
        _, _, body = self.get(op, "/_box/me")
        tok = re.search(r"""name=["']token["'] value=["']([0-9a-f]+)""", body).group(1)
        req = urllib.request.Request(self.base + "/_box/me", data=urllib.parse.urlencode(
            {"token": tok, "action": "where", "op": "set", "status": "Unit testing the demo"}).encode(),
            headers={"Origin": self.base})
        with op.open(req, timeout=60) as r:
            self.assertIn("Status saved", r.read().decode())
        _, _, body = self.get(op, "/_box/me")
        self.assertIn("Unit testing the demo", body)
        self.get(op, "/_demo/reset")
        _, _, body = self.get(op, "/_box/me")
        self.assertNotIn("Unit testing the demo", body)
        self.assertIn("At the hackathon", body)

    def test_outside_page_explains(self):
        _, _, body = self.get(self.as_("alice"), "/_demo/outside?to=" + urllib.parse.quote("https://t.me/x?start=1"))
        self.assertIn("In the demo this would open Telegram", body)
        self.assertIn("Pretend I pressed Start", body)

    def test_refuses_every_address(self):
        r = subprocess.run([sys.executable, DEMO, "--host", "0.0.0.0"], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing", r.stderr)


if __name__ == "__main__":
    unittest.main()
