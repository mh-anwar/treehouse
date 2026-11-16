"""sbcommon: conf writes (the installer's config migration and every `sharedbox-admin` setting go through
set_config), per-user limits and the memory boost contract. Stdlib only, no root; systemctl is faked.
    python3 -m unittest discover -s tests -v"""

import os
import shutil
import stat
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
import sbcommon as sb  # noqa: E402

G = 1 << 30


class SetConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sbconf-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "sharedbox.conf")
        with open(self.path, "w") as f:
            f.write("# owner's note\nMEMORY_HIGH=8G\n#MEMORY_MAX=1G\nPUBLIC_URL=https://x.example\nMEMORY_HIGH=9G\n")
        os.chmod(self.path, 0o640)

    def read(self):
        with open(self.path) as f:
            return f.read()

    def test_rewrites_in_place_keeps_comments_mode_and_drops_shadowing_duplicates(self):
        sb.set_config({"MEMORY_HIGH": "16G", "BOOST_HOURS": "2"}, self.path)
        lines = self.read().splitlines()
        self.assertEqual(lines[:4], ["# owner's note", "MEMORY_HIGH=16G", "#MEMORY_MAX=1G", "PUBLIC_URL=https://x.example"])
        self.assertEqual(lines.count("MEMORY_HIGH=16G"), 1)
        self.assertNotIn("MEMORY_HIGH=9G", lines)  # a later duplicate would have won over the new value
        self.assertEqual(lines[-1], "BOOST_HOURS=2")
        cfg = sb.load_config(self.path)
        self.assertEqual((cfg["MEMORY_HIGH"], cfg["BOOST_HOURS"], cfg["MEMORY_MAX"]), ("16G", "2", sb.DEFAULTS["MEMORY_MAX"]))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)
        self.assertEqual([n for n in os.listdir(self.tmp) if n != "sharedbox.conf"], [])  # no temp file left

    def test_refuses_values_that_could_inject_lines(self):
        before = self.read()
        for bad in ({"PUBLIC_URL": "x\nADMIN_USERS=mallory"}, {"PUBLIC_URL": "a;rm -rf /"}, {"bad key": "1"}):
            with self.assertRaises(ValueError):
                sb.set_config(bad, self.path)
        self.assertEqual(self.read(), before)


class Limits(unittest.TestCase):
    def test_record_overrides_only_known_limit_keys(self):
        cfg = dict(sb.DEFAULTS)
        rec = {"limits": {"MEMORY_HIGH": "4G", "DISK_BUDGET_GB": "10", "CPU_QUOTA": "999%", "ExecStart": "/bin/sh"}}
        lim = sb.user_limits(cfg, rec)
        self.assertEqual(set(lim), set(sb.LIMIT_KEYS))
        self.assertEqual((lim["MEMORY_HIGH"], lim["DISK_BUDGET_GB"], lim["MEMORY_MAX"]), ("4G", "10", cfg["MEMORY_MAX"]))


class Boost(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sbboost-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls = []
        for name in ("BOOSTS", "RUNTIME_CONTROL", "limited_accounts", "log_event", "_mem_available",
                     "_slice_mem_current"):
            self.addCleanup(setattr, sb, name, getattr(sb, name))
        self.addCleanup(setattr, sb.subprocess, "run", sb.subprocess.run)
        sb.BOOSTS = os.path.join(self.tmp, "boosts.json")
        sb.RUNTIME_CONTROL = os.path.join(self.tmp, "control")
        sb.limited_accounts = lambda cfg: {"sam": (2001, {"status": "active", "limits": {"MEMORY_HIGH": "8G",
                                                                                         "MEMORY_MAX": "9G"}})}
        sb.log_event = lambda *a, **k: None
        sb._slice_mem_current = lambda uid: 0
        sb.subprocess.run = lambda cmd, **k: self.calls.append(cmd) or sb.subprocess.CompletedProcess(cmd, 0, "", "")
        self.cfg = dict(sb.DEFAULTS, BOOST_MEMORY="28G", BOOST_HOURS="4")

    def test_start_keeps_the_high_max_gap_and_expires(self):
        sb._mem_available = lambda: 64 * G
        res = sb.boost_start(self.cfg, "sam", now=1000)
        self.assertEqual(res, {"ok": True, "until": 1000 + 4 * 3600, "error": ""})
        self.assertEqual(self.calls, [["systemctl", "set-property", "--runtime", "user-2001.slice",
                                       f"MemoryHigh={28 * G}", f"MemoryMax={29 * G}"]])
        self.assertEqual(sb.boosts(now=1001), {"sam": 1000 + 4 * 3600})
        self.assertEqual(sb.boosts(now=1000 + 4 * 3600), {})

    def test_refused_without_free_memory_or_account(self):
        sb._mem_available = lambda: 2 * G  # needs 28G - 8G = 20G free
        self.assertFalse(sb.boost_start(self.cfg, "sam")["ok"])
        self.assertFalse(sb.boost_start(self.cfg, "nobody")["ok"])
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.exists(sb.BOOSTS))

    def test_reconcile_ends_expired_boost_with_the_normal_limits_live(self):
        sb.write_json(sb.BOOSTS, {"sam": {"until": time.time() - 1}, "ghost": {"until": time.time() + 99}})
        sb.boost_reconcile(self.cfg)
        self.assertIn(["systemctl", "set-property", "--runtime", "user-2001.slice", "MemoryHigh=8G", "MemoryMax=9G"],
                      self.calls)
        self.assertEqual(sb.read_json(sb.BOOSTS), {})  # expired one ended, unknown account dropped


if __name__ == "__main__":
    unittest.main()
