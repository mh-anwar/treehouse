"""sharedbox-tune: the shipped settings parse, every check runs without crashing (commands faked), and the
report sorts treehouse fixes from owner suggestions. Stdlib only, no root.
    python3 -m unittest discover -s tests -v"""

import glob
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
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

    @unittest.skipUnless(os.path.isdir("/proc/sys/net/core"), "needs a Linux /proc/sys")
    def test_every_shipped_setting_is_a_real_kernel_key(self):
        want, _ = tune.shipped_sysctls()
        missing = [k for k in want if not os.path.exists(tune.sysctl_path(k))
                   and k != "net.ipv4.tcp_congestion_control"]
        self.assertEqual(missing, [])

    def test_all_checks_run_and_are_well_formed(self):
        # commands (systemctl, tailscale, a login shell) are faked: fast, and the same on any machine
        orig = tune.subprocess.run
        tune.subprocess.run = lambda cmd, **k: tune.subprocess.CompletedProcess(cmd, 1, "", "")
        try:
            res = tune.run_checks()
        finally:
            tune.subprocess.run = orig
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


class Awake(unittest.TestCase):
    """Staying awake: two drop-ins treehouse owns, written once, removed with TUNE_KEEP_AWAKE=0."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = (tune.SLEEP_DROPIN, tune.LOGIND_DROPIN, tune.subprocess.run, tune.keep_awake_setting)
        tune.SLEEP_DROPIN = os.path.join(self.tmp, "sleep.conf.d", "60-sharedbox-awake.conf")
        tune.LOGIND_DROPIN = os.path.join(self.tmp, "logind.conf.d", "60-sharedbox-awake.conf")
        self.cmds = []
        tune.subprocess.run = lambda cmd, **kw: self.cmds.append(cmd) or tune.subprocess.CompletedProcess(cmd, 0, "", "")
        tune.keep_awake_setting = lambda: True

    def tearDown(self):
        tune.SLEEP_DROPIN, tune.LOGIND_DROPIN, tune.subprocess.run, tune.keep_awake_setting = self.saved

    def test_apply_writes_once_and_check_agrees(self):
        self.assertFalse(tune.check_awake()[0]["ok"])
        self.assertIn("just set", tune.apply_awake())
        self.assertIn("AllowSuspend=no", open(tune.SLEEP_DROPIN).read())
        self.assertIn("HandleLidSwitch=ignore", open(tune.LOGIND_DROPIN).read())
        self.assertEqual(len(self.cmds), 1)  # logind told to re-read its config
        self.assertTrue(tune.check_awake()[0]["ok"])
        self.assertNotIn("just set", tune.apply_awake())  # idempotent: nothing rewritten, logind left alone
        self.assertEqual(len(self.cmds), 1)

    def test_off_switch_removes_them(self):
        tune.apply_awake()
        tune.keep_awake_setting = lambda: False
        self.assertIn("TUNE_KEEP_AWAKE=0", tune.apply_awake())
        self.assertFalse(os.path.exists(tune.SLEEP_DROPIN) or os.path.exists(tune.LOGIND_DROPIN))
        r = tune.check_awake()[0]
        self.assertTrue(r["ok"])
        self.assertEqual(r["kind"], "owner")


class NewInstall(unittest.TestCase):
    def test_limits_fit_small_machines_only(self):
        self.assertEqual(tune.fitted_limits(32 << 30), {})
        self.assertEqual(tune.fitted_limits(0), {})
        self.assertEqual(tune.fitted_limits(8 << 30), {"MEMORY_HIGH": "5G", "MEMORY_MAX": "6G", "BOOST_MEMORY": "7G"})
        small = tune.fitted_limits(int(3.8 * (1 << 30)))
        self.assertEqual(small, {"MEMORY_HIGH": "2G", "MEMORY_MAX": "3G", "BOOST_MEMORY": "3G"})
        for mem in (2 << 30, 4 << 30, 12 << 30, 16 << 30, 19 << 30):
            lim = {k: int(v[:-1]) for k, v in tune.fitted_limits(mem).items()}
            self.assertLess(lim["MEMORY_HIGH"], lim["MEMORY_MAX"])
            self.assertLessEqual(lim["MEMORY_MAX"], lim["BOOST_MEMORY"])
            self.assertLessEqual(lim["BOOST_MEMORY"], mem >> 30)

    def test_preflight_notes(self):
        big = tune.preflight_notes("x86_64", 32 << 30, 200 << 30, False, False, False)
        self.assertEqual(big, [])
        levels = [lv for lv, _ in tune.preflight_notes("i686", 2 << 30, 10 << 30, True, True, True)]
        self.assertEqual(levels[0], "stop")
        self.assertIn("warn", levels)
        old_laptop = " ".join(t for _, t in tune.preflight_notes("x86_64", 8 << 30, 100 << 30, True, True, True))
        for words in ("5G soft", "spinning", "plugged in", "Wi-Fi"):
            self.assertIn(words, old_laptop)
        self.assertNotIn("stop", [lv for lv, _ in tune.preflight_notes("aarch64", 4 << 30, 30 << 30, False, False, False)])


if __name__ == "__main__":
    unittest.main()
