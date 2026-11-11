"""sharedbox-tune: the shipped settings parse, every check runs on this machine without crashing, and the
report sorts treehouse fixes from owner suggestions. Stdlib only, no root.
    python3 -m unittest discover -s tests -v"""

import glob
import importlib.machinery
import importlib.util
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
loader = importlib.machinery.SourceFileLoader("sbtune", os.path.join(ROOT, "lib", "sharedbox-tune"))
tune = importlib.util.module_from_spec(importlib.util.spec_from_loader("sbtune", loader))
loader.exec_module(tune)


class Tune(unittest.TestCase):
    def test_shipped_files_parse_with_reasons(self):
        old, tune.SYSCTL_INSTALLED = tune.SYSCTL_INSTALLED, "/nonexistent/*.conf"
        try:
            want, files = tune.shipped_sysctls()
        finally:
            tune.SYSCTL_INSTALLED = old
        self.assertEqual(sorted(files), sorted(glob.glob(os.path.join(ROOT, "etc", "sysctl-*.conf"))))
        for k in ("net.core.rmem_max", "fs.inotify.max_user_instances", "net.ipv4.tcp_congestion_control"):
            self.assertIn(k, want)
            self.assertTrue(want[k][2], f"{k} has no comment saying why")
        self.assertEqual(want["net.ipv4.tcp_wmem"][0], "4096 65536 16777216")  # whitespace normalised

    def test_every_shipped_setting_is_a_real_kernel_key(self):
        want, _ = tune.shipped_sysctls()
        missing = [k for k in want if not os.path.exists(tune.sysctl_path(k))
                   and k != "net.ipv4.tcp_congestion_control"]
        self.assertEqual(missing, [])

    def test_all_checks_run_and_are_well_formed(self):
        res = tune.run_checks()
        self.assertTrue(res)
        for r in res:
            self.assertEqual(set(r), {"name", "ok", "now", "want", "why", "kind"})
            self.assertIn(r["kind"], ("treehouse", "owner", "account"))
            self.assertNotIn("check failed", r["now"], r)

    def test_missing_install_is_reported(self):
        old = (tune.SYSCTL_INSTALLED, tune.SYSCTL_REPO)
        tune.SYSCTL_INSTALLED = tune.SYSCTL_REPO = os.path.join(tempfile.mkdtemp(), "*.conf")
        try:
            res = tune.check_sysctls()
        finally:
            tune.SYSCTL_INSTALLED, tune.SYSCTL_REPO = old
        self.assertEqual(len(res), 1)
        self.assertFalse(res[0]["ok"])


if __name__ == "__main__":
    unittest.main()
