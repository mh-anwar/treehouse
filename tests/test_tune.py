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


class Swap(unittest.TestCase):
    def setUp(self):
        self.cmds, self.files = [], {"/proc/swaps": "Filename Type Size Used Priority\n",
                                     "/proc/meminfo": "MemTotal: 32000000 kB\n"}
        self.free = 150 << 30
        saved = (tune.read, tune.subprocess.run, tune.os.statvfs, tune.os.chmod, tune.tune_swap_setting,
                 tune.os.path.exists)
        self.addCleanup(self.restore, saved)
        tune.read = lambda p, d="": self.files.get(p, d)
        tune.tune_swap_setting = lambda: "auto"
        tune.os.chmod = lambda *a: None
        tune.os.path.exists = lambda p: False

        class V:
            f_bavail, f_frsize, f_blocks = self.free, 1, 217 << 30
        tune.os.statvfs = lambda p: V

        def run(cmd, **kw):
            self.cmds.append(cmd)
            out = "ext4\n" if cmd[:2] == ["stat", "-fc"] else ""
            return tune.subprocess.CompletedProcess(cmd, 0, out, "")
        tune.subprocess.run = run

    def restore(self, saved):
        (tune.read, tune.subprocess.run, tune.os.statvfs, tune.os.chmod, tune.tune_swap_setting,
         tune.os.path.exists) = saved

    def test_makes_one_when_none(self):
        msg = tune.apply_swap()
        self.assertIn("made /swap.treehouse (8 GB)", msg)
        self.assertEqual([c[0] for c in self.cmds], ["stat", "fallocate", "mkswap", "swapon"])

    def test_leaves_existing_swap_alone(self):
        self.files["/proc/swaps"] += "/swap.img file 8388604 0 -2\n"
        self.assertIn("/swap.img", tune.apply_swap())
        self.assertEqual(self.cmds, [])

    def test_off_switch_and_small_disk(self):
        tune.tune_swap_setting = lambda: "0"
        self.assertIn("off", tune.apply_swap())
        tune.tune_swap_setting = lambda: "auto"

        class Small:
            f_bavail, f_frsize, f_blocks = 10 << 30, 1, 100 << 30
        tune.os.statvfs = lambda p: Small
        self.assertIn("not enough free disk", tune.apply_swap())
        self.assertEqual(self.cmds, [])


if __name__ == "__main__":
    unittest.main()
