"""`go install` and `cargo install` land on PATH and the first `cargo` works (box sync_home_defaults), without ever
changing a setting the user already has. Stdlib only.
    python3 -m unittest discover -s tests -v"""

import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)

loader = importlib.machinery.SourceFileLoader("box_home_defaults", os.path.join(LIB, "box"))
box = importlib.util.module_from_spec(importlib.util.spec_from_loader("box_home_defaults", loader))
_argv, sys.argv = sys.argv, ["box"]
loader.exec_module(box)
sys.argv = _argv


def read(p):
    with open(p) as f:
        return f.read()


class HomeDefaults(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="sbhome-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        for p in (patch.object(box, "HOME", self.home),
                  patch.object(box.shutil, "which", lambda n, *a, **k: "/usr/bin/rustup" if n == "rustup" else None)):
            p.start()
            self.addCleanup(p.stop)
        self.p = lambda *parts: os.path.join(self.home, *parts)

    def test_fresh_home(self):
        box.sync_home_defaults()
        self.assertEqual(read(self.p(".config/go/env")), f"GOBIN={self.home}/.local/bin\n")
        self.assertIn(f'root = "{self.home}/.local"', read(self.p(".cargo/config.toml")))
        self.assertIn('default_toolchain = "stable"', read(self.p(".rustup/settings.toml")))
        before = {f: read(self.p(f)) for f in (".config/go/env", ".cargo/config.toml", ".rustup/settings.toml")}
        box.sync_home_defaults()  # idempotent
        self.assertEqual(before, {f: read(self.p(f)) for f in before})

    def test_existing_settings_kept(self):
        os.makedirs(self.p(".config/go"))
        with open(self.p(".config/go/env"), "w") as f:
            f.write("GOPROXY=direct")   # no trailing newline
        os.makedirs(self.p(".cargo"))
        with open(self.p(".cargo/config"), "w") as f:
            f.write("[build]\njobs = 4\n")
        os.makedirs(self.p(".rustup/toolchains"))
        box.sync_home_defaults()
        self.assertEqual(read(self.p(".config/go/env")), f"GOPROXY=direct\nGOBIN={self.home}/.local/bin\n")
        self.assertFalse(os.path.exists(self.p(".cargo/config.toml")))
        self.assertFalse(os.path.exists(self.p(".rustup/settings.toml")))
        with open(self.p(".config/go/env"), "w") as f:
            f.write("GOBIN=/elsewhere\n")
        box.sync_home_defaults()
        self.assertEqual(read(self.p(".config/go/env")), "GOBIN=/elsewhere\n")

    def test_no_rustup_on_the_box(self):
        with patch.object(box.shutil, "which", lambda *a, **k: None):
            box.sync_home_defaults()
        self.assertFalse(os.path.exists(self.p(".rustup")))


if __name__ == "__main__":
    unittest.main()
