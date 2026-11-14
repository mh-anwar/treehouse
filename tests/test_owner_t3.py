"""The owner's T3 Code under `box t3` (nightly for everyone): the idle check that gates the one-time switch, the
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
CREATE TABLE orchestration_v2_projection_runtime_requests (runtime_request_id TEXT, status TEXT, resolved_at TEXT);
CREATE TABLE orchestration_v2_projection_turn_items (turn_item_id TEXT, type TEXT, status TEXT);
CREATE TABLE orchestration_v2_projection_subagents (subagent_id TEXT, status TEXT);
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
        self.con.execute("INSERT INTO orchestration_v2_projection_runtime_requests VALUES ('q', 'resolved', 'x')")
        self.con.execute("INSERT INTO orchestration_v2_projection_turn_items VALUES ('i', 'command_execution', 'completed')")
        self.con.execute("INSERT INTO orchestration_v2_projection_turn_items VALUES ('t', 'todo_list', 'running')")
        self.con.execute("INSERT INTO scheduled_tasks VALUES ('s', 1, ?)", (iso(now + 2 * 86400),))
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
        self.con.execute("INSERT INTO orchestration_v2_projection_runtime_requests VALUES ('p', 'pending', NULL)")
        self.con.commit()
        self.assertTrue(self.busy())
        self.con.execute("DELETE FROM orchestration_v2_projection_runtime_requests WHERE status = 'pending'")
        self.con.execute("INSERT INTO orchestration_v2_projection_turn_items VALUES ('c', 'subagent', 'running')")
        self.con.commit()
        self.assertTrue(self.busy())

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
    "sb-t3.service": "[Unit]\nDescription=T3 Code (box t3; naps when unused, sb-t3.socket wakes it)\n"
    "StartLimitIntervalSec=600\nStartLimitBurst=10\n\n[Service]\nWorkingDirectory=%h/projects\n"
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
        self.assertEqual({n: open(os.path.join(units, n)).read() for n in os.listdir(units)}, FRIEND_UNITS)


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
            self.assertTrue(box.t3_rollback("100.64.0.10", 3773, db, 87, None))
        finally:
            box.sctl, box.listening, box.t3_http_ok, box.T3_BACKUP = saved
        self.assertEqual(box.t3_migration(db), 87)
        self.assertFalse(os.path.exists(db + "-wal"))
        self.assertIn(("enable", "--now", "t3code.service"), calls)
        self.assertLess(calls.index(("stop", "sb-t3.service")), calls.index(("enable", "--now", "t3code.service")))


if __name__ == "__main__":
    unittest.main()
