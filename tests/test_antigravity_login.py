"""`box`'s Antigravity sign-in check: the token file must have something in it, and `antigravity models` must not ask
to sign in (run once per version of the file). A fake `antigravity` on the account's PATH, no network."""

import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
TMP = tempfile.mkdtemp(prefix="sbagy-")
_home, os.environ["HOME"] = os.environ.get("HOME"), TMP

loader = importlib.machinery.SourceFileLoader("box_agy", os.path.join(LIB, "box"))
box = importlib.util.module_from_spec(importlib.util.spec_from_loader("box_agy", loader))
_argv, sys.argv = sys.argv, ["box"]
loader.exec_module(box)
sys.argv = _argv
if _home is not None:
    os.environ["HOME"] = _home


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class AntigravityLoggedIn(unittest.TestCase):
    def setUp(self):
        self.bin = os.path.join(TMP, ".local", "bin")
        os.makedirs(self.bin, exist_ok=True)
        self.runs = os.path.join(TMP, "runs")
        for p in (box.AGY_TOKEN, box.AGY_CHECK, self.runs):
            if os.path.exists(p):
                os.unlink(p)

    def fake(self, code, text):
        path = os.path.join(self.bin, "antigravity")
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\necho "$1:$AGY_CLI_DISABLE_AUTO_UPDATE" >> "{self.runs}"\necho "{text}"\nexit {code}\n')
        os.chmod(path, 0o755)

    def token(self, text):
        os.makedirs(os.path.dirname(box.AGY_TOKEN), exist_ok=True)
        with open(box.AGY_TOKEN, "w") as f:
            f.write(text)

    def runs_seen(self):
        try:
            with open(self.runs) as f:
                return f.read().split()
        except FileNotFoundError:
            return []

    def test_no_or_empty_token_file(self):
        self.fake(0, "gemini-3-pro")
        self.assertFalse(box.portal_logged_in("antigravity"))
        self.token("")
        self.assertFalse(box.portal_logged_in("antigravity"))
        self.assertEqual(self.runs_seen(), [])   # nothing to check: the CLI isn't even started

    def test_models_decides_once_per_token_version(self):
        self.token('{"refresh_token": "r"}')
        self.fake(1, "Error: Please sign in to view available models.")
        self.assertFalse(box.portal_logged_in("antigravity"))
        self.assertFalse(box.portal_logged_in("antigravity"))
        self.assertEqual(self.runs_seen(), ["models:true"])   # cached; the self-updater is off
        self.token('{"refresh_token": "new one"}')
        self.fake(0, "gemini-3-pro")
        self.assertTrue(box.portal_logged_in("antigravity"))
        self.assertEqual(len(self.runs_seen()), 2)

    def test_other_failures_trust_the_file(self):
        self.token('{"refresh_token": "r"}')
        self.fake(1, "Error: network unreachable")
        self.assertTrue(box.portal_logged_in("antigravity"))

    def test_login_entry(self):
        label, cmd, paste = box.PORTAL_LOGINS["antigravity"]
        self.assertEqual((cmd, paste), (["antigravity", "acp", "--login"], True))
        self.assertTrue(box.PASTE_RE.fullmatch("4/0AVGzR1Ab-cd_EF.gh"))


if __name__ == "__main__":
    unittest.main()
