"""No-freeze: a frozen box restarts by itself. The watchdog/earlyoom steps of `sharedbox-tune apply` (system calls
faked, files in a temp dir), the checks `box tune` shows, health's restart reason and stopped `box serve` services,
and `box serve add` never pinning a service to a folder that gets deleted. Stdlib only, no root.
    python3 -m unittest tests.test_nofreeze -v"""

import importlib.machinery
import importlib.util
import json
import os
import pwd
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    argv, sys.argv = sys.argv, [os.path.basename(path)]
    try:
        loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


tune = load("sbtune_nofreeze", os.path.join(LIB, "sharedbox-tune"))
health = load("sbhealth_nofreeze", os.path.join(LIB, "sharedbox-health"))
box = load("box_nofreeze", os.path.join(LIB, "box"))

ok = subprocess.CompletedProcess


class Sysctls(unittest.TestCase):
    def test_nofreeze_settings_ship_with_reasons(self):
        old, tune.SYSCTL_INSTALLED = tune.SYSCTL_INSTALLED, "/nonexistent/*.conf"
        try:
            want, _ = tune.shipped_sysctls()
        finally:
            tune.SYSCTL_INSTALLED = old
        for k, v in (("kernel.panic", "10"), ("kernel.panic_on_oops", "1"), ("kernel.softlockup_panic", "1"),
                     ("kernel.hardlockup_panic", "1"), ("vm.swappiness", "10")):
            self.assertEqual(want[k][0], v)
            self.assertTrue(want[k][2], f"{k} has no comment saying why")
        # a slow disk stalls programs without the box being frozen: never reboot for that
        self.assertNotIn("kernel.hung_task_panic", want)


class Units(unittest.TestCase):
    def test_usec_seconds(self):
        for v, want in (("0", 0), ("", 0), ("1min", 60), ("30s", 30), ("1min 30s", 90), ("500ms", 0),
                        ("infinity", 1), ("junk", 0)):
            self.assertEqual(tune.usec_seconds(v), want, v)


class Watchdog(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cmds = []
        self.sys = os.path.join(self.d, "sys-watchdog0")
        self.ident = "iTCO_wdt"
        self.secs = 0
        self.modprobe_ok = True
        saved = {k: getattr(tune, k) for k in ("WATCHDOG_SYS", "WD_MODULES_LOAD", "WD_MODPROBE", "WD_SYSTEMD",
                                               "WD_FILES", "HAND_MADE", "virt", "systemd_active", "watchdog_users",
                                               "systemd_runtime_watchdog", "wait_for", "tune_setting")}
        saved["run"] = tune.subprocess.run
        self.addCleanup(self.restore, saved)
        p = lambda n: os.path.join(self.d, n)  # noqa: E731
        tune.WATCHDOG_SYS = self.sys
        tune.WD_MODULES_LOAD, tune.WD_MODPROBE, tune.WD_SYSTEMD = p("ml/sb.conf"), p("mp/zz.conf"), p("sd/60.conf")
        tune.WD_FILES = {tune.WD_MODULES_LOAD: saved["WD_FILES"][saved["WD_MODULES_LOAD"]],
                         tune.WD_MODPROBE: saved["WD_FILES"][saved["WD_MODPROBE"]],
                         tune.WD_SYSTEMD: saved["WD_FILES"][saved["WD_SYSTEMD"]]}
        self.hand = {p("hand-ml.conf"): ["iTCO_wdt"],
                     p("hand-sd.conf"): ["[Manager]", "RuntimeWatchdogSec=60s", "RebootWatchdogSec=5min"],
                     p("hand-sysctl.conf"): ["kernel.panic = 10", "kernel.panic_on_oops = 1"]}
        tune.HAND_MADE = self.hand
        tune.virt = lambda: ""
        tune.systemd_active = lambda u: False
        tune.watchdog_users = lambda: []
        tune.systemd_runtime_watchdog = lambda: self.secs
        tune.wait_for = lambda path, secs=5.0: os.path.exists(path)
        tune.tune_setting = lambda k, d="auto": "auto"

        def run(cmd, **kw):
            self.cmds.append(cmd)
            if cmd[0] == "modprobe":
                if self.modprobe_ok:
                    self.make_device()
                return ok(cmd, 0 if self.modprobe_ok else 1, "", "")
            if cmd[0] == "busctl" and cmd[5] == "RuntimeWatchdogUSec":
                self.secs = int(cmd[-1]) // 10**6
            if cmd[0] == "systemd-detect-virt":
                return ok(cmd, 1, "", "")
            return ok(cmd, 0, "", "")
        tune.subprocess.run = run

    def make_device(self):
        os.makedirs(self.sys, exist_ok=True)
        with open(os.path.join(self.sys, "identity"), "w") as f:
            f.write(self.ident + "\n")

    def restore(self, saved):
        tune.subprocess.run = saved.pop("run")
        for k, v in saved.items():
            setattr(tune, k, v)

    def test_loads_itco_writes_files_and_arms_live(self):
        msg = tune.apply_watchdog()
        self.assertIn("armed (systemd pings it every 60s)", msg)
        self.assertIn(["modprobe", "--ignore-install", "iTCO_wdt"], self.cmds)
        for path in (tune.WD_MODULES_LOAD, tune.WD_MODPROBE, tune.WD_SYSTEMD):
            self.assertTrue(os.path.exists(path), path)
        with open(tune.WD_SYSTEMD) as f:
            self.assertIn("RuntimeWatchdogSec=60s", f.read())
        busctl = [c for c in self.cmds if c[0] == "busctl"]
        self.assertEqual([c[5] for c in busctl], ["RuntimeWatchdogUSec", "RebootWatchdogUSec"])
        self.assertNotIn(["systemctl", "daemon-reexec"], self.cmds)
        # second run: nothing to write, nothing to arm
        self.cmds.clear()
        msg = tune.apply_watchdog()
        self.assertNotIn("wrote", msg)
        self.assertFalse([c for c in self.cmds if c[0] in ("busctl", "modprobe")])

    def test_no_hardware_cleans_up_and_skips(self):
        self.modprobe_ok = False
        os.makedirs(os.path.dirname(tune.WD_MODULES_LOAD))
        open(tune.WD_MODULES_LOAD, "w").close()   # left from an older run: would fail a module load every boot
        msg = tune.apply_watchdog()
        self.assertIn("none on this machine", msg)
        self.assertFalse(os.path.exists(tune.WD_MODULES_LOAD))
        self.assertFalse(os.path.exists(tune.WD_SYSTEMD))

    def test_other_daemon_or_container_left_alone(self):
        tune.systemd_active = lambda u: u == "watchdog.service"
        self.assertIn("watchdog.service already runs it", tune.apply_watchdog())
        tune.systemd_active = lambda u: False
        tune.watchdog_users = lambda: [4242]
        self.assertIn("another program already runs it", tune.apply_watchdog())
        tune.watchdog_users = lambda: []
        tune.virt = lambda: "lxc"
        orig = tune.subprocess.run
        tune.subprocess.run = lambda cmd, **kw: ok(cmd, 0, "", "") if cmd[0] == "systemd-detect-virt" else orig(cmd)
        self.assertIn("inside a container", tune.apply_watchdog())
        self.assertFalse(os.path.exists(tune.WD_SYSTEMD))

    def test_other_driver_only_gets_the_systemd_part(self):
        self.ident = "Software Watchdog"
        self.make_device()
        tune.apply_watchdog()
        self.assertTrue(os.path.exists(tune.WD_SYSTEMD))
        self.assertFalse(os.path.exists(tune.WD_MODULES_LOAD))
        self.assertNotIn("modprobe", [c[0] for c in self.cmds])

    def test_off_switch(self):
        tune.tune_setting = lambda k, d="auto": "0" if k == "TUNE_WATCHDOG" else d
        self.assertIn("off", tune.apply_watchdog())
        self.assertEqual(self.cmds, [])

    def test_hand_made_copies_replaced_only_when_identical(self):
        files = list(self.hand)
        with open(files[0], "w") as f:
            f.write("# treehouse: hardware watchdog reboots a frozen box\niTCO_wdt\n")
        with open(files[1], "w") as f:
            f.write("[Manager]\nRuntimeWatchdogSec=60s\nRebootWatchdogSec=5min\n")
        with open(files[2], "w") as f:   # the owner added a setting of their own: keep the file
            f.write("kernel.panic=10\nkernel.panic_on_oops=1\nvm.dirty_ratio=5\n")
        msg = tune.apply_watchdog()
        self.assertIn("replaced hand-made", msg)
        self.assertFalse(os.path.exists(files[0]))
        self.assertFalse(os.path.exists(files[1]))
        self.assertTrue(os.path.exists(files[2]))
        # spacing differences don't matter
        with open(files[2], "w") as f:
            f.write("kernel.panic=10\nkernel.panic_on_oops = 1\n")
        tune.remove_hand_made()
        self.assertFalse(os.path.exists(files[2]))


class Earlyoom(unittest.TestCase):
    def test_which_files_treehouse_may_write(self):
        self.assertTrue(tune.earlyoom_conf_ours(None))
        self.assertTrue(tune.earlyoom_conf_ours('# Default settings\nEARLYOOM_ARGS="-r 3600"\n'))
        self.assertTrue(tune.earlyoom_conf_ours(tune.EARLYOOM_BODY))
        # the fleet guard put in by hand on some boxes: same settings, so treehouse takes it over
        self.assertTrue(tune.earlyoom_conf_ours(f'# Fleet OOM guard\nEARLYOOM_ARGS="{tune.EARLYOOM_ARGS}"\n'))
        self.assertFalse(tune.earlyoom_conf_ours('EARLYOOM_ARGS="-m 3 -s 5"\n'))

    def test_args_never_target_agents(self):
        avoid = tune.EARLYOOM_ARGS.split("--avoid ", 1)[1]
        for name in ("claude", "codex", "t3", "sshd", "tailscaled", "dockerd", "systemd"):
            self.assertIn(name, avoid)
        self.assertEqual(tune.earlyoom_args(tune.EARLYOOM_BODY), tune.EARLYOOM_ARGS)

    def test_apply_writes_and_restarts_once(self):
        d = tempfile.mkdtemp()
        cmds = []
        saved = (tune.EARLYOOM_CONF, tune.subprocess.run, tune.systemd_active, tune.tune_setting)
        try:
            tune.EARLYOOM_CONF = os.path.join(d, "earlyoom")
            with open(tune.EARLYOOM_CONF, "w") as f:
                f.write('EARLYOOM_ARGS="-r 3600"\n')
            tune.subprocess.run = lambda cmd, **kw: (cmds.append(cmd), ok(cmd, 0, "", ""))[1]
            tune.systemd_active = lambda u: True
            tune.tune_setting = lambda k, d="auto": "auto"
            import shutil
            if not shutil.which("earlyoom"):
                self.assertIn("not installed", tune.apply_earlyoom(install=False))
                return
            self.assertIn("updated", tune.apply_earlyoom())
            self.assertIn(["systemctl", "restart", "earlyoom.service"], cmds)
            cmds.clear()
            self.assertNotIn("updated", tune.apply_earlyoom())
            self.assertNotIn(["systemctl", "restart", "earlyoom.service"], cmds)
        finally:
            tune.EARLYOOM_CONF, tune.subprocess.run, tune.systemd_active, tune.tune_setting = saved


class Checks(unittest.TestCase):
    def test_nofreeze_check_well_formed(self):
        orig = tune.subprocess.run
        tune.subprocess.run = lambda cmd, **k: ok(cmd, 1, "", "")
        try:
            res = tune.check_nofreeze()
        finally:
            tune.subprocess.run = orig
        names = [r["name"] for r in res]
        self.assertTrue(any("watchdog" in n for n in names))
        self.assertTrue(any("earlyoom" in n for n in names))
        for r in res:
            self.assertEqual(set(r), {"name", "ok", "now", "want", "why", "kind"})
        self.assertIn(tune.check_nofreeze, tune.CHECKS)

    def test_last_restart_reported_for_owner(self):
        d = tempfile.mkdtemp()
        old = tune.LAST_BOOT
        tune.LAST_BOOT = os.path.join(d, "last-boot.json")
        orig = tune.subprocess.run
        tune.subprocess.run = lambda cmd, **k: ok(cmd, 1, "", "")
        try:
            with open(tune.LAST_BOOT, "w") as f:
                json.dump({"reason": "watchdog", "when": "2026-11-27 03:12"}, f)
            r = [x for x in tune.check_nofreeze() if x["name"] == "last restart"]
            self.assertEqual(len(r), 1)
            self.assertFalse(r[0]["ok"])
            self.assertEqual(r[0]["kind"], "owner")
            with open(tune.LAST_BOOT, "w") as f:
                json.dump({"reason": "clean"}, f)
            self.assertFalse([x for x in tune.check_nofreeze() if x["name"] == "last restart"])
        finally:
            tune.LAST_BOOT, tune.subprocess.run = old, orig


class BootReason(unittest.TestCase):
    def test_reasons(self):
        clean = {"MESSAGE": "Journal stopped", "SYSLOG_IDENTIFIER": "systemd-journald"}
        mid = {"MESSAGE": "Accepted publickey for pippin", "SYSLOG_IDENTIFIER": "sshd"}
        self.assertEqual(health.boot_reason("32", clean, []), "watchdog")   # WDIOF_CARDRESET wins
        self.assertEqual(health.boot_reason("0", mid, ["/var/lib/systemd/pstore/x/dmesg.txt"]), "panic")
        self.assertEqual(health.boot_reason("0", clean, []), "clean")
        self.assertEqual(health.boot_reason("", {"SYSLOG_IDENTIFIER": "systemd-shutdown", "MESSAGE": "x"}, []),
                         "clean")
        self.assertEqual(health.boot_reason("0", mid, []), "unclean")
        self.assertEqual(health.boot_reason("", None, []), "unknown")
        self.assertEqual(health.boot_reason("junk", mid, []), "unclean")


def base_g(**kw):
    g = {"disk_free_pct": 50, "disk_free": 1, "failed_units": [], "oom": [], "disk_errors": [], "smart": {},
         "temp": None, "samples": 10, "stopped_services": [], "last_boot": {}}
    g.update(kw)
    return g


class HealthProblems(unittest.TestCase):
    def test_treehouse_units_and_box_services_are_urgent(self):
        g = base_g(failed_units=["sharedbox-janitor.service", "cups.service"],
                   stopped_services=[{"user": "pippin", "name": "agent-guard", "folder_gone": True,
                                      "why": "its folder /home/pippin/.t3/worktrees/x no longer exists"}])
        p = health.problems(sbtestenv_cfg(), g, [])
        red = [m for l, m in p if l == "red"]
        self.assertTrue(any("sharedbox-janitor" in m for m in red))
        self.assertTrue(any("agent-guard" in m and "box serve rm" in m for m in red))
        self.assertIn(("yellow", "failed services: cups.service"), p)

    def test_restart_by_itself_alerts_for_two_days(self):
        lb = {"reason": "watchdog", "when": "2026-11-27 03:12", "boot_ts": time.time() - 3600}
        red = [m for l, m in health.problems(sbtestenv_cfg(), base_g(last_boot=lb), []) if l == "red"]
        self.assertTrue(any("restarted by itself at 2026-11-27 03:12" in m for m in red))
        for reason, ts in (("clean", time.time()), ("watchdog", time.time() - 3 * 86400)):
            lb = {"reason": reason, "when": "x", "boot_ts": ts}
            self.assertFalse([m for _, m in health.problems(sbtestenv_cfg(), base_g(last_boot=lb), [])
                              if "restarted by itself" in m])
        self.assertTrue(all(any(k in m for k in health.REMIND_WEEKLY) for m in red if "restarted" in m))

    def test_stopped_box_services_from_unit_files(self):
        home = tempfile.mkdtemp()
        udir = os.path.join(home, ".config", "systemd", "user")
        os.makedirs(os.path.join(udir, "default.target.wants"))
        live = os.path.join(home, "proj")
        os.makedirs(live)
        units = {"sb-gone.service": os.path.join(home, ".t3", "worktrees", "deleted"),
                 "sb-ok.service": live, "sb-optional.service": "-" + os.path.join(home, "nope"),
                 "sb-gone-disabled.service": os.path.join(home, "also-deleted"), "sb-t3.service": "/nowhere"}
        for fn, wd in units.items():
            with open(os.path.join(udir, fn), "w") as f:
                f.write(f"[Service]\nWorkingDirectory={wd}\nExecStart=/bin/true\n")
            if fn != "sb-gone-disabled.service":
                os.symlink(os.path.join(udir, fn), os.path.join(udir, "default.target.wants", fn))
        me = pwd.struct_passwd(("friend", "x", 1500, 1500, "", home, "/bin/bash"))
        saved = (health.pwd.getpwall, health.run)
        try:
            health.pwd.getpwall = lambda: [me]
            health.run = lambda cmd, timeout=60: ""
            out = health.stopped_box_services()
        finally:
            health.pwd.getpwall, health.run = saved
        self.assertEqual([(x["user"], x["name"], x["folder_gone"]) for x in out], [("friend", "gone", True)])


def sbtestenv_cfg():
    return health.sb.load_config()


class ServeWorkdir(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="sbhome-")
        self.old = box.HOME
        box.HOME = self.home
        self.wt = os.path.join(self.home, ".t3", "worktrees", "proj", "t3-abc")
        os.makedirs(self.wt)
        self.keep = os.path.join(self.home, "keep", "boxcap")
        os.makedirs(self.keep)
        open(os.path.join(self.keep, "agent-guard"), "w").close()
        open(os.path.join(self.wt, "app.py"), "w").close()

    def tearDown(self):
        box.HOME = self.old

    def test_project_folder_kept(self):
        proj = os.path.join(self.home, "proj")
        self.assertEqual(box.serve_workdir(proj, ["/usr/bin/python3", "app.py"]), (proj, ""))

    def test_worktree_runs_from_command_folder_or_home(self):
        wd, note = box.serve_workdir(self.wt, [os.path.join(self.keep, "agent-guard")])
        self.assertEqual(wd, self.keep)
        self.assertIn("temporary folder", note)
        wd, _ = box.serve_workdir(self.wt, ["/usr/bin/sleep", "infinity"])
        self.assertEqual(wd, self.home)
        wd, _ = box.serve_workdir("/tmp", ["/usr/bin/python3", "-m", "http.server"])
        self.assertEqual(wd, self.home)

    def test_code_inside_the_worktree_is_refused(self):
        wd, err = box.serve_workdir(self.wt, ["/usr/bin/python3", "app.py"])
        self.assertIsNone(wd)
        self.assertIn("app.py", err)
        wd, err = box.serve_workdir(self.wt, [os.path.join(self.wt, "run.sh")])
        self.assertIsNone(wd)
        self.assertIn("temporary folder", err)

    def test_broken_services_found(self):
        udir = os.path.join(self.home, ".config", "systemd", "user")
        os.makedirs(udir)
        old = box.UNIT_DIR
        box.UNIT_DIR = udir
        try:
            for n, wd in (("dead", os.path.join(self.home, "gone")), ("fine", self.keep)):
                with open(os.path.join(udir, f"sb-{n}.service"), "w") as f:
                    f.write(f"[Service]\nWorkingDirectory={wd}\n")
            self.assertEqual([x["name"] for x in box.broken_services()], ["dead"])
        finally:
            box.UNIT_DIR = old


if __name__ == "__main__":
    unittest.main()
