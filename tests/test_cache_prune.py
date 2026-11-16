"""Cache pruning: old entries of nested caches (one Playwright build at a time) go, ones in use stay.
    python3 -m unittest discover -s tests -v"""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
import sbclean  # noqa: E402

DAY = 86400


class NestedCaches(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="sbcache-")
        pw = os.path.join(self.home, ".cache", "ms-playwright")
        for name, age in (("chromium-1248", 30), ("chromium-1254", 1)):
            d = os.path.join(pw, name)
            os.makedirs(d)
            f = os.path.join(d, "chrome")
            open(f, "w").write("x" * 1000)
            t = time.time() - age * DAY
            os.utime(f, (t, t))
            os.utime(d, (t, t))
        t = time.time() - DAY  # the folder itself was touched recently (the new build went in)
        os.utime(pw, (t, t))

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_old_build_goes_new_one_stays(self):
        plan = {"cache_ttl_days": 7, "tmp_ttl_days": 3, "tmp_dirs": []}
        rows = sbclean.clean_caches(plan, self.home, time.time(), set(), False, False)
        gone = {os.path.basename(r["path"]) for r in rows}
        self.assertIn("chromium-1248", gone)
        self.assertNotIn("chromium-1254", gone)
        self.assertNotIn("ms-playwright", gone)
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".cache", "ms-playwright", "chromium-1254")))


class T3Versions(unittest.TestCase):
    """Old T3 runtimes go, except ones a unit or T3's own state still points at (the rollback launcher)."""

    def test_named_versions_survive(self):
        home = tempfile.mkdtemp(prefix="sbt3-")
        self.addCleanup(shutil.rmtree, home, True)
        vdir = os.path.join(home, ".t3", "runtime", "versions")
        ages = {"0.1.2": 40, "0.1.10-nightly.20261113.4564": 30, "0.1.10-nightly.20261113.4616": 20,
                "0.1.10-nightly.20261114.4682": 2, "0.1.9": 1}
        for v, age in ages.items():
            os.makedirs(os.path.join(vdir, v))
            t = time.time() - age * DAY
            os.utime(os.path.join(vdir, v), (t, t))
        units = os.path.join(home, ".config", "systemd", "user")
        os.makedirs(units)
        with open(os.path.join(units, "t3code.service"), "w") as f:
            f.write("[Service]\nExecStart=%h/.t3/runtime/versions/0.1.2/t3 __service-launcher\n")
        with open(os.path.join(home, ".t3", "runtime", "service-state.json"), "w") as f:
            f.write('{"activeVersion": "0.1.10-nightly.20261113.4616"}')
        plan = {"cache_ttl_days": 7, "tmp_ttl_days": 3, "tmp_dirs": []}
        rows = sbclean.clean_caches(plan, home, time.time(), set(), False, False)
        gone = {os.path.basename(r["path"]) for r in rows if "T3" in r["reason"]}
        self.assertEqual(gone, {"0.1.10-nightly.20261113.4564"})


if __name__ == "__main__":
    unittest.main()
