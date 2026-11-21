"""Temporary reboot hold (`REBOOT_HOLD_UNTIL` / `box reboot hold`): skips scheduled and apt update reboots
without turning on hackathon mode, and expires cleanly. Stdlib only; no root or network."""

import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)

_adm_loader = importlib.machinery.SourceFileLoader("sbadmin_rb", os.path.join(LIB, "sharedbox-admin"))
adm = importlib.util.module_from_spec(importlib.util.spec_from_loader("sbadmin_rb", _adm_loader))
_adm_loader.exec_module(adm)

_jan_loader = importlib.machinery.SourceFileLoader("sbjan_rb", os.path.join(LIB, "sharedbox-janitor"))
jan = importlib.util.module_from_spec(importlib.util.spec_from_loader("sbjan_rb", _jan_loader))
_argv, sys.argv = sys.argv, ["sharedbox-janitor"]
_jan_loader.exec_module(jan)
sys.argv = _argv

sb = adm.sb


class RebootHold(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sbrb-")
        self.apt_dir = os.path.join(self.tmp, "apt.conf.d")
        os.makedirs(self.apt_dir)
        self.apt_file = os.path.join(self.apt_dir, "99sharedbox-no-reboot")
        self.state_file = os.path.join(self.tmp, "reboot-hold.json")
        self._orig = (sb.APT_NO_REBOOT, sb.REBOOT_HOLD_STATE, sb.log_event, sb.pending_shutdown)
        sb.APT_NO_REBOOT = self.apt_file
        sb.REBOOT_HOLD_STATE = self.state_file
        self.events = []
        sb.log_event = lambda kind, user=None, **kw: self.events.append((kind, user, kw))
        sb.pending_shutdown = lambda: None

    def tearDown(self):
        sb.APT_NO_REBOOT, sb.REBOOT_HOLD_STATE, sb.log_event, sb.pending_shutdown = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_next_scheduled_reboot_skips_held_window(self):
        # Sat 2026-11-21 16:00 local -> next scheduled is Sun 2026-11-22 04:00
        now = time.mktime((2026, 11, 21, 16, 0, 0, 0, 0, -1))
        cfg = dict(sb.DEFAULTS, SCHEDULED_REBOOT_DAYS="Wed,Sun", REBOOT_TIME="04:00")
        nxt = sb.next_scheduled_reboot(cfg, now)
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt)), "2026-11-22 04:00")

        held = dict(cfg, REBOOT_HOLD_UNTIL="2026-11-22 12:00")
        nxt_held = sb.next_scheduled_reboot(held, now)
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt_held)), "2026-11-25 04:00")

        info = sb.reboot_info(held, now)
        self.assertEqual(info["at"], nxt_held)
        self.assertIsNotNone(info["hold_until"])

    def test_reconcile_writes_and_cleans_apt_override(self):
        now = time.mktime((2026, 11, 21, 16, 0, 0, 0, 0, -1))
        on = dict(sb.DEFAULTS, REBOOT_HOLD_UNTIL="2026-11-22 12:00")
        self.assertEqual(sb.reboot_hold_reconcile(on, now), "on")
        self.assertTrue(os.path.exists(self.apt_file))
        with open(self.apt_file) as f:
            self.assertIn('Unattended-Upgrade::Automatic-Reboot "false";', f.read())
        self.assertIsNone(sb.reboot_hold_reconcile(on, now))  # idempotent

        after = time.mktime((2026, 11, 22, 12, 5, 0, 0, 0, -1))
        self.assertEqual(sb.reboot_hold_reconcile(on, after), "off")
        self.assertFalse(os.path.exists(self.apt_file))
        self.assertIsNone(sb.reboot_hold_reconcile(on, after))
        self.assertEqual([e[0] for e in self.events], ["reboot_hold_on", "reboot_hold_off"])

    def test_janitor_reboot_skips_when_held_without_hackathon(self):
        cfg = dict(sb.DEFAULTS, HACKATHON="", REBOOT_HOLD_UNTIL="2999-01-01 00:00")
        called = []
        orig_run = jan.subprocess.run
        jan.subprocess.run = lambda *a, **k: called.append(a)
        self.addCleanup(setattr, jan.subprocess, "run", orig_run)
        jan.cmd_reboot(cfg, type("A", (), {"dry_run": False})())
        self.assertEqual(called, [])

    def test_admin_reboot_hold_set_validates_and_cancels_pending(self):
        runs = []
        orig_run = adm.run
        adm.run = lambda cmd, **k: runs.append(cmd) or type("R", (), {"returncode": 0})()
        self.addCleanup(setattr, adm, "run", orig_run)
        sb.pending_shutdown = lambda: time.time() + 3600

        bad = adm.reboot_hold_set(sb.load_config(), {"op": "on", "until": "2000-01-01 00:00"}, "pippin")
        self.assertFalse(bad["ok"])

        until = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 12 * 3600))
        res = adm.reboot_hold_set(sb.load_config(), {"op": "on", "until": until}, "pippin")
        self.assertTrue(res["ok"])
        self.assertTrue(res["active"])
        self.assertTrue(res["cancelled"])
        self.assertIn(["/sbin/shutdown", "-c"], runs)
        self.assertTrue(os.path.exists(self.apt_file))

        off = adm.reboot_hold_set(sb.load_config(), {"op": "off"}, "pippin")
        self.assertTrue(off["ok"])
        self.assertFalse(off["active"])
        self.assertFalse(os.path.exists(self.apt_file))


if __name__ == "__main__":
    unittest.main()
