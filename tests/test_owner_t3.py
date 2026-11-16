"""The owner's T3 Code under `box t3` (opt-in switch): the idle check that gates the one-time switch, the
owner's port/env/unit, friends' units staying byte-identical, nightly version order, and the rollback restoring a
database the new version migrated. Stdlib only, no T3, no systemd.
    python3 -m unittest discover -s tests -v"""

import importlib.machinery
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
TMP = tempfile.mkdtemp(prefix="sbownert3-")
CONF = os.path.join(TMP, "sharedbox.conf")
with open(CONF, "w") as f:
    f.write("WEB_PORT=8090\n")
os.environ.update(SHAREDBOX_CONF=CONF, SHAREDBOX_STATE=os.path.join(TMP, "state"), SHAREDBOX_LIB=LIB,
                  SHAREDBOX_SHARE=os.path.join(ROOT, "share"), HOME=TMP)
sys.path.insert(0, LIB)


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    argv, sys.argv = sys.argv, [name]
    try:
        loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


box = load("box_owner_t3", os.path.join(LIB, "box"))
tools = load("tools_owner_t3", os.path.join(LIB, "sharedbox-tools"))


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


SCHEMA = """
CREATE TABLE orchestration_v2_projection_runs (run_id TEXT, status TEXT, requested_at TEXT, completed_at TEXT);
CREATE TABLE orchestration_v2_projection_runtime_requests (runtime_request_id TEXT, status TEXT, resolved_at TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE orchestration_v2_projection_turn_items (turn_item_id TEXT, type TEXT, status TEXT, run_id TEXT DEFAULT 'r-live', updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE orchestration_v2_projection_subagents (subagent_id TEXT, status TEXT, run_id TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE orchestration_v2_thread_launch_workflows (command_id TEXT, status TEXT);
CREATE TABLE scheduled_tasks (task_id TEXT, enabled INTEGER, next_run_at TEXT);
"""


class IdleCheck(unittest.TestCase):
    """Restarting T3 while anything runs kills the owner's live agent chats: only a fully quiet database is idle."""

    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(dir=TMP), "statev2.sqlite")
        self.con = sqlite3.connect(self.db)
        self.con.executescript(SCHEMA)
        now = time.time()
        # a long history of finished work, and a task due in two days: all of it idle
        self.con.executemany("INSERT INTO orchestration_v2_projection_runs VALUES (?, ?, ?, ?)",
                             [(f"r{i}", s, iso(now - 3600), iso(now - 3000))
                              for i, s in enumerate(("completed", "failed", "cancelled", "interrupted"))])
        self.con.execute("INSERT INTO orchestration_v2_projection_runtime_requests (runtime_request_id, status, resolved_at) VALUES ('q', 'resolved', 'x')")
        self.con.execute("INSERT INTO orchestration_v2_projection_turn_items (turn_item_id, type, status) VALUES ('i', 'command_execution', 'completed')")
        self.con.execute("INSERT INTO orchestration_v2_projection_turn_items (turn_item_id, type, status) VALUES ('t', 'todo_list', 'running')")
        self.con.execute("INSERT INTO scheduled_tasks VALUES ('s', 1, ?)", (iso(now + 2 * 86400),))
        self.con.execute("INSERT INTO orchestration_v2_projection_runs VALUES ('r-live', 'completed', ?, NULL)", (iso(time.time()),))
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def busy(self):
        return box.t3_db_busy(self.db)

    def test_finished_history_is_idle(self):
        self.assertEqual(self.busy(), [])

    def test_every_unfinished_run_state_is_busy(self):
        # nightly has queued/preparing/starting/waiting besides running; a list of "running" states misses them
        for status in ("preparing", "starting", "queued", "waiting", "some-future-state"):
            self.con.execute("DELETE FROM orchestration_v2_projection_runs WHERE run_id = 'x'")
            self.con.execute("INSERT INTO orchestration_v2_projection_runs VALUES ('x', ?, ?, NULL)",
                             (status, iso(time.time())))
            self.con.commit()
            self.assertTrue(self.busy(), status)

    def test_waiting_approval_and_running_command_are_busy(self):
        self.con.execute("INSERT INTO orchestration_v2_projection_runtime_requests (runtime_request_id, status, resolved_at) VALUES ('p', 'pending', NULL)")
        self.con.commit()
        self.assertTrue(self.busy())
        self.con.execute("DELETE FROM orchestration_v2_projection_runtime_requests WHERE status = 'pending'")
        self.con.execute("UPDATE orchestration_v2_projection_runs SET status='running' WHERE run_id='r-live'")
        self.con.execute("INSERT INTO orchestration_v2_projection_turn_items (turn_item_id, type, status) VALUES ('c', 'subagent', 'running')")
        self.con.commit()
        self.assertTrue(self.busy())

    def test_orphan_items_and_subagents_under_failed_run_are_idle(self):
        self.con.execute("INSERT INTO orchestration_v2_projection_turn_items (turn_item_id, type, status, run_id) VALUES ('orphan', 'dynamic_tool', 'running', 'r1')")
        self.con.execute("INSERT INTO orchestration_v2_projection_subagents (subagent_id, status, run_id) VALUES ('orphan', 'running', 'r1')")
        self.con.commit()
        self.assertEqual(self.busy(), [])

    def test_stale_activity_is_idle(self):
        self.con.execute("INSERT INTO orchestration_v2_projection_runs VALUES ('stale', 'running', ?, NULL)", (iso(time.time() - 90000),))
        self.con.execute("INSERT INTO orchestration_v2_projection_runtime_requests VALUES ('stale', 'pending', NULL, ?)", (iso(time.time() - 90000),))
        self.con.commit()
        self.assertEqual(self.busy(), [])

    def test_scheduled_task_due_soon_is_busy(self):
        # T3 skips a fixed-time run it was down for
        self.con.execute("INSERT INTO scheduled_tasks VALUES ('soon', 1, ?)", (iso(time.time() + 600),))
        self.con.commit()
        self.assertTrue(self.busy())

    def test_unknown_layout_is_busy(self):
        self.con.execute("DROP TABLE orchestration_v2_projection_runs")
        self.con.commit()
        self.assertTrue(self.busy())
        self.assertTrue(box.t3_db_busy(os.path.join(TMP, "missing.sqlite")))


class OwnerUnit(unittest.TestCase):
    def setUp(self):
        self.units = tempfile.mkdtemp(dir=TMP)
        self.saved = (box.UNIT_DIR, box.sctl, box.owner_t3, box.is_friend, box.ports)
        box.UNIT_DIR = self.units
        box.sctl = lambda *a, **k: None
        box.is_friend = lambda: False
        box.ports = lambda: None
        box.owner_t3 = lambda: ("100.64.0.10", 3773)
        os.makedirs(os.path.join(TMP, ".t3"), exist_ok=True)
        with open(os.path.join(TMP, ".t3", "service.env"), "w") as f:
            f.write("T3CODE_HOST=127.0.0.1\nT3CODE_PORT=1\nDOCKER_HOST=unix:///run/user/1002/docker.sock\n")

    def tearDown(self):
        box.UNIT_DIR, box.sctl, box.owner_t3, box.is_friend, box.ports = self.saved
        os.unlink(os.path.join(TMP, ".t3", "service.env"))

    def test_owner_port_on_tailscale_and_old_env_kept(self):
        host, port, env = box.t3_env()
        self.assertEqual((host, port), ("100.64.0.10", 3773))
        # the old service's settings can't move it to loopback (revokes every pairing) or another port
        self.assertEqual((env["T3CODE_HOST"], env["T3CODE_PORT"]), ("100.64.0.10", "3773"))
        self.assertEqual(env["T3CODE_HOME"], os.path.join(TMP, ".t3"))  # same home: same chats, same pairings
        self.assertEqual(env["DOCKER_HOST"], "unix:///run/user/1002/docker.sock")
        for bad in (None, "127.0.0.1"):
            box.owner_t3 = lambda: (bad, 3773)
            with self.assertRaises(SystemExit):
                box.t3_env()

    def test_owner_unit_boots_without_socket(self):
        host, _, env = box.t3_env()
        self.assertTrue(box.t3_write_units(host, env))
        self.assertEqual(os.listdir(self.units), ["sb-t3.service"])  # no nap socket or proxy for the owner
        with open(os.path.join(self.units, "sb-t3.service")) as f:
            text = f.read()
        self.assertIn("WantedBy=default.target", text)       # up at boot: scheduled tasks need it
        self.assertIn("StartLimitIntervalSec=0", text)       # a slow Tailscale at boot can't leave it dead
        self.assertIn('grep -qF " 100.64.0.10/"', text)   # waits for its address before binding it
        self.assertIn("Environment=T3CODE_PORT=3773", text)
        self.assertFalse(box.t3_write_units(host, env))      # unchanged: no daemon-reload, nothing restarted


FRIEND_UNITS = {
    "sb-t3.service": "[Unit]\nDescription=T3 Code (box t3; sb-t3.socket starts it)\n"
    "StartLimitIntervalSec=600\nStartLimitBurst=10\n\n[Service]\nWorkingDirectory=%h\n"
    "Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin\nEnvironment=T3CODE_HOST=100.64.0.5\n"
    "Environment=T3CODE_PORT=31009\nEnvironment=T3CODE_PROJECT_ROOT=/home/x/projects\nEnvironment=T3CODE_NO_BROWSER=1\n"
    "ExecStart=/usr/local/bin/t3 serve\nExecStartPost=/bin/bash -c 'for i in $$(seq 600); do c=$$(curl -s -o "
    "/dev/null -w %%{http_code} --max-time 2 http://100.64.0.5:31009/api/auth/session); [ \"$$c\" = 200 ] && exit 0; "
    "sleep 0.5; done; exit 1'\nTimeoutStartSec=330\nSuccessExitStatus=143 SIGTERM\nRestart=on-failure\nRestartSec=5\n",
    "sb-t3-proxy.service": "[Unit]\nDescription=T3 Code wake-up proxy\nRequires=sb-t3.service\nAfter=sb-t3.service\n"
    "BindsTo=sb-t3.service\n\n[Service]\nExecStart=/usr/lib/systemd/systemd-socket-proxyd 100.64.0.5:31009\n",
    "sb-t3.socket": "[Unit]\nDescription=T3 Code port (wakes T3 on the first connection)\n\n[Socket]\n"
    "ListenStream=100.64.0.5:31000\nFreeBind=yes\nService=sb-t3-proxy.service\n\n[Install]\nWantedBy=sockets.target\n",
}


class FriendUnitsUnchanged(unittest.TestCase):
    def test_byte_identical(self):
        # any change makes `box t3`/the T3 button restart a friend's running T3 (t3_ensure), possibly mid-turn
        units = tempfile.mkdtemp(dir=TMP)
        saved = (box.UNIT_DIR, box.sctl, box.ports, box.is_friend)
        box.UNIT_DIR, box.sctl, box.ports, box.is_friend = units, (lambda *a, **k: None), (lambda: (31000, 31009)), (lambda: True)
        try:
            box.t3_write_units("100.64.0.5", {"T3CODE_HOST": "100.64.0.5", "T3CODE_PORT": "31009",
                                              "T3CODE_PROJECT_ROOT": "/home/x/projects", "T3CODE_NO_BROWSER": "1"})
        finally:
            box.UNIT_DIR, box.sctl, box.ports, box.is_friend = saved
        self.assertEqual({n: Path(units, n).read_text() for n in os.listdir(units)}, FRIEND_UNITS)


class NightlyOrder(unittest.TestCase):
    def test_nightlies_sort_by_build(self):
        # equal keys made the previous nightly look "not older" and the cleanup deleted it right after an update
        a, b = "0.1.10-nightly.20261113.4616", "0.1.10-nightly.20261114.4682"
        self.assertLess(tools.vkey(a), tools.vkey(b))
        self.assertLess(tools.vkey("0.1.9"), tools.vkey(a))
        self.assertLess(tools.vkey(b), tools.vkey("0.1.10"))
        self.assertLess(tools.vkey("0.1.10-nightly.20261114.999"), tools.vkey("0.1.10-nightly.20261114.4682"))


class Rollback(unittest.TestCase):
    def test_restores_database_the_new_version_migrated(self):
        # the old T3 can't open a database a newer version migrated: it must get the backup back, without the WAL
        d = tempfile.mkdtemp(dir=TMP)
        db, backup = os.path.join(d, "statev2.sqlite"), os.path.join(d, "backup.sqlite")
        for path, mig in ((backup, 87), (db, 91)):
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE effect_sql_migrations (migration_id INTEGER)")
            con.execute("INSERT INTO effect_sql_migrations VALUES (?)", (mig,))
            con.commit()
            con.close()
        with open(db + "-wal", "w") as f:
            f.write("new version's log")
        calls = []
        saved = (box.sctl, box.listening, box.t3_http_ok, box.T3_BACKUP)
        box.sctl = lambda *a, **k: calls.append(a) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        box.listening, box.t3_http_ok, box.T3_BACKUP = (lambda h, p: False), (lambda h, p: True), backup
        try:
            with patch.object(box, "t3_db_holders", return_value=[]):
                self.assertTrue(box.t3_rollback("100.64.0.10", 3773, db, 87, None))
        finally:
            box.sctl, box.listening, box.t3_http_ok, box.T3_BACKUP = saved
        self.assertEqual(box.t3_migration(db), 87)
        self.assertFalse(os.path.exists(db + "-wal"))
        self.assertIn(("enable", "--now", "t3code.service"), calls)
        self.assertLess(calls.index(("stop", "sb-t3.service")), calls.index(("enable", "--now", "t3code.service")))


class ReviewFixes(unittest.TestCase):
    def rollback_fixture(self):
        d = tempfile.mkdtemp(dir=TMP)
        db, backup = os.path.join(d, "statev2.sqlite"), os.path.join(d, "backup.sqlite")
        for path, mig in ((db, 91), (backup, 87)):
            with sqlite3.connect(path) as con:
                con.execute("CREATE TABLE effect_sql_migrations (migration_id INTEGER)")
                con.execute("INSERT INTO effect_sql_migrations VALUES (?)", (mig,))
            con.close()
        Path(db + "-wal").write_text("keep this WAL")
        return db, backup

    def test_restore_refuses_open_database_and_rearms_old_service(self):
        db, backup = self.rollback_fixture()
        calls = []
        with patch.object(box, "T3_BACKUP", backup), patch.object(box, "t3_db_holders", return_value=["123"]), patch.object(box, "sctl", side_effect=lambda *a, **k: calls.append(a) or type("R", (), {"returncode": 0})()):
            self.assertFalse(box.t3_rollback("100.64.0.10", 3773, db, 87, None))
        self.assertEqual(Path(db + "-wal").read_text(), "keep this WAL")
        self.assertEqual(box.t3_migration(db), 91)
        self.assertLess(calls.index(("enable", "t3code.service")), calls.index(("disable", "sb-t3.service")))
        self.assertIn(("enable", "--now", "t3code.service"), calls)
        self.assertEqual(box.sb.read_json(box.T3_SWITCH_STATE)["state"], "failed-hard")

    def test_failed_copy_keeps_wal_and_still_starts_old_service(self):
        db, backup = self.rollback_fixture()
        calls = []
        with patch.object(box, "T3_BACKUP", backup), patch.object(box.shutil, "copyfile", side_effect=OSError("disk full")), patch.object(box, "sctl", side_effect=lambda *a, **k: calls.append(a) or type("R", (), {"returncode": 0})()):
            self.assertFalse(box.t3_rollback("100.64.0.10", 3773, db, 87, None))
        self.assertTrue(os.path.exists(db + "-wal"))
        self.assertEqual(box.t3_migration(db), 91)
        self.assertIn(("reset-failed", "t3code.service"), calls)
        self.assertIn(("enable", "--now", "t3code.service"), calls)

    def test_proc_scan_finds_open_db_without_listener(self):
        db = os.path.join(TMP, "held.sqlite")
        with open(db, "w"):
            self.assertIn(str(os.getpid()), box.t3_db_holders(db))

    def test_uninspectable_processes_count_only_with_a_lock_on_the_database(self):
        # from inside the switch's user unit, the user's own (sd-pam) and systemd --user can't be inspected
        with tempfile.TemporaryDirectory(dir=TMP) as d:
            db = os.path.join(d, "statev2.sqlite")
            for ext in ("", "-wal", "-shm"):
                open(db + ext, "w").close()
            shm = os.stat(db + "-shm").st_ino
            locks = os.path.join(d, "locks")
            with open(locks, "w") as f:
                f.write(f"1: POSIX  ADVISORY  READ 4242 00:2a:{shm} 128 128\n"
                        f"1: -> POSIX  ADVISORY  WRITE 4343 00:2a:{shm} 0 EOF\n"
                        "2: FLOCK  ADVISORY  WRITE 777 00:2a:1 0 EOF\n")
            self.assertEqual(box.t3_db_locks(db, locks), {"4242", "4343"})
            self.assertIsNone(box.t3_db_locks(db, os.path.join(d, "missing")))
            real_listdir = os.listdir

            def listdir(path):
                if path.startswith("/proc/") and path.endswith("/fd") and path.split("/")[2] in ("4242", "999"):
                    raise PermissionError(path)
                return ["4242", "999"] if path == "/proc" else real_listdir(path)
            with patch.object(box.os, "listdir", listdir), patch.object(box.os, "stat", lambda p, *a, **k: type("S", (), {"st_uid": os.getuid(), "st_ino": shm})()):
                with patch.object(box, "t3_db_locks", return_value={"4242"}):
                    self.assertEqual(box.t3_db_holders(db), ["4242 (cannot inspect)"])
                with patch.object(box, "t3_db_locks", return_value=None):  # no lock table: unknown is a holder
                    self.assertEqual(sorted(box.t3_db_holders(db)), ["4242 (cannot inspect)", "999 (cannot inspect)"])

    def test_offline_backup_is_between_stop_and_start(self):
        events = []
        with tempfile.TemporaryDirectory(dir=TMP) as d, patch.object(box, "T3_OLD_DROPIN", os.path.join(d, "dropin")), patch.object(box, "T3_MANAGED_MARK", os.path.join(d, "mark")), patch.object(box, "t3_backup", side_effect=lambda *a: events.append("backup")), patch.object(box, "t3_verify", return_value=""), patch.object(box, "sctl", side_effect=lambda *a, **k: events.append(a) or self.unit(a, "active", "running")):
            self.assertEqual(box.t3_do_switch("100.64.0.10", 3773), "")
        self.assertLess(events.index(("stop", "t3code.service")), events.index("backup"))
        self.assertLess(events.index("backup"), events.index(("start", "--no-block", "sb-t3.service")))

    @staticmethod
    def unit(args, active, sub, restarts="0", result="success", mainpid="42"):
        out = (f"ActiveState={active}\nSubState={sub}\nNRestarts={restarts}\nResult={result}\nMainPID={mainpid}\n"
               if args[0] == "show" else "")
        return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()

    def test_new_t3_crashing_on_start_fails_instead_of_waiting_forever(self):
        # Restart=always, no start limit: a blocking `systemctl start` would wait through the restarts for good
        for state in (("activating", "auto-restart", "0", "exit-code"), ("activating", "start-post", "1", "success"),
                      ("failed", "failed", "0", "exit-code"), ("activating", "start-post", "0", "success", "0")):
            with patch.object(box, "sctl", side_effect=lambda *a, **k: self.unit(a, *state)), patch.object(box.time, "sleep"):
                self.assertIn("didn't start", box.t3_start_watch(), state)
        seen = iter([("activating", "start-pre", "0", "success", "0"), ("activating", "start-post"), ("active", "running")])
        with patch.object(box, "sctl", side_effect=lambda *a, **k: self.unit(a, *next(seen)) if a[0] == "show" else self.unit(a, "", "")), patch.object(box.time, "sleep"):
            self.assertEqual(box.t3_start_watch(), "")

    def test_friend_upgrade_backup_once_before_changed_launcher(self):
        userdata = Path(TMP, ".t3", "userdata")
        userdata.mkdir(parents=True, exist_ok=True)
        (userdata / "statev2.sqlite").touch()
        runtime = userdata / "server-runtime.json"
        runtime.write_text('{"serverVersion":"0.1.9"}')
        version = "0.1.10-nightly.20261114.4682"
        dest = Path(TMP, ".t3", "sb-backup", "before-" + version + ".sqlite")
        def backup(db, path):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).touch()
        with patch.object(box, "is_friend", return_value=True), patch.object(box, "t3_backup", side_effect=backup) as save:
            box.cmd_t3_backup_upgrade(type("A", (), {"version": version})())
            box.cmd_t3_backup_upgrade(type("A", (), {"version": version})())
            self.assertEqual(save.call_count, 1)
        dest.unlink()
        self.assertIn('box _t3-backup-upgrade', tools.shim_text({}, "t3", "t3"))

    def test_friend_upgrade_backups_keep_newest_two(self):
        userdata = Path(TMP, ".t3", "userdata")
        userdata.mkdir(parents=True, exist_ok=True)
        (userdata / "statev2.sqlite").touch()
        (userdata / "server-runtime.json").write_text('{"serverVersion":"0.1.9"}')
        folder = Path(TMP, ".t3", "sb-backup")
        def backup(db, path):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).touch()
        vers = [f"0.1.10-nightly.2026111{i}.{4600 + i}" for i in range(4)]
        with patch.object(box, "is_friend", return_value=True), patch.object(box, "t3_backup", side_effect=backup):
            for i, v in enumerate(vers):
                box.cmd_t3_backup_upgrade(type("A", (), {"version": v})())
                os.utime(folder / f"before-{v}.sqlite", (1000 + i, 1000 + i))
        left = sorted(p.name for p in folder.glob("before-*.sqlite"))
        self.assertEqual(left, [f"before-{v}.sqlite" for v in vers[-2:]])
        for p in folder.glob("before-*.sqlite"):
            p.unlink()

    def test_t3_downgrade_is_refused(self):
        with patch.object(tools, "current_version", return_value="0.1.10"), patch.object(tools.os, "symlink") as link, patch.object(tools, "log") as log:
            tools.set_current("t3", "0.1.9")
            link.assert_not_called()
            log.assert_called_once()

    def test_gc_protects_recent_switch_and_two_previous(self):
        with tempfile.TemporaryDirectory(dir=TMP) as d:
            root = Path(d, "t3")
            root.mkdir()
            for v in ("0.1.7", "0.1.8", "0.1.9", "0.1.10"):
                (root / v).mkdir()
            (root / "current").symlink_to("0.1.10")
            with patch.object(tools.sb, "AGENTS_DIR", d), patch.object(tools, "in_use", return_value=set()), patch.object(tools, "current_version", return_value="0.1.10"):
                tools.gc_versions(["t3"])
                self.assertTrue((root / "0.1.7").exists())
                os.utime(root / "current", (time.time()-700, time.time()-700), follow_symlinks=False)
                tools.gc_versions(["t3"])
                self.assertFalse((root / "0.1.7").exists())
                self.assertTrue((root / "0.1.8").exists())


if __name__ == "__main__":
    unittest.main()
