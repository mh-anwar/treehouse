"""Agents' rule files get the reboot picture right: refreshed right after a reboot and when one gets scheduled,
not only at the 6-hourly janitor pass. Stdlib only; user switching and the reboot state are faked.
    python3 -m unittest discover -s tests -v"""

import importlib.machinery
import importlib.util
import os
import pwd
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
TMP = tempfile.mkdtemp(prefix="sbrules-")
os.environ["HOME"] = TMP

loader = importlib.machinery.SourceFileLoader("janitor_rules", os.path.join(LIB, "sharedbox-janitor"))
jan = importlib.util.module_from_spec(importlib.util.spec_from_loader("janitor_rules", loader))
_argv, sys.argv = sys.argv, ["sharedbox-janitor"]
loader.exec_module(jan)
sys.argv = _argv

ME = pwd.getpwuid(os.getuid()).pw_name


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class RulesRefresh(unittest.TestCase):
    def setUp(self):
        self.calls, self.boot, self.rb = [], "boot-1", {"at": None, "why": None, "postponed": False}
        self._orig = (jan.user_pass, jan.boot_id, jan.sb.reboot_info, jan.sb.admin_users, jan.sb.owner_account)
        jan.user_pass = lambda pw, plan, cmd, check=None: self.calls.append((pw.pw_name, cmd)) or {"synced": True}
        jan.boot_id = lambda: self.boot
        jan.sb.reboot_info = lambda cfg: dict(self.rb, latest=None, schedule="")
        jan.sb.admin_users = lambda cfg: []
        jan.sb.owner_account = lambda cfg: None

    def tearDown(self):
        jan.user_pass, jan.boot_id, jan.sb.reboot_info, jan.sb.admin_users, jan.sb.owner_account = self._orig

    def run_once(self, state):
        self.calls.clear()
        jan.refresh_rules({}, {ME: {}}, state)
        return list(self.calls)

    def test_refreshes_after_boot_and_on_schedule_changes_only(self):
        state = {}
        self.assertEqual(self.run_once(state), [(ME, "_sync-rules")])  # first run after boot
        self.assertEqual(self.run_once(state), [])  # nothing changed: nothing runs
        self.rb = {"at": time.time() + 3600, "why": "updates", "postponed": False}  # updates scheduled a reboot
        self.assertEqual(len(self.run_once(state)), 1)
        self.assertEqual(self.run_once(state), [])
        self.boot = "boot-2"  # it rebooted: the 'will reboot' notice must go
        self.rb = {"at": None, "why": None, "postponed": False}
        self.assertEqual(len(self.run_once(state)), 1)

    def test_scheduled_reboot_enters_12h_window(self):
        state = {}
        self.rb = {"at": time.time() + 3 * 86400, "why": "scheduled", "postponed": False}
        self.run_once(state)
        self.rb = {"at": time.time() + 3 * 86400, "why": "scheduled", "postponed": False}
        self.assertEqual(self.run_once(state), [])  # far off: same picture
        self.rb = {"at": time.time() + 3600, "why": "scheduled", "postponed": False}
        self.assertEqual(len(self.run_once(state)), 1)


if __name__ == "__main__":
    unittest.main()
