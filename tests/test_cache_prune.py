"""Cache pruning: old entries of nested caches (one Playwright build at a time) go, ones in use stay.
    python3 -m unittest discover -s tests -v"""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
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


if __name__ == "__main__":
    unittest.main()
