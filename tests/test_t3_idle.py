"""T3 Code on one release channel, idle restarts onto the box's version, sleep, and the owner's move off T3's own
t3code.service (`box _t3-tick`, the janitor tick's t3 pass, the Machines card's version line).
Stdlib only: a synthetic T3 database, fake /proc/net/tcp, every systemctl call recorded instead of run.
    python3 -m unittest tests.test_t3_idle -v"""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
TMP = tempfile.mkdtemp(prefix="sbt3idle-")
os.environ["HOME"] = TMP


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    argv, sys.argv = sys.argv, [name]
    try:
        loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


box = load("box_t3idle", os.path.join(LIB, "box"))
janitor = load("janitor_t3idle", os.path.join(LIB, "sharedbox-janitor"))
sb = box.sb


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def make_db(path, now):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE orchestration_v2_projection_runs(run_id TEXT, status TEXT, requested_at TEXT, completed_at TEXT);
        CREATE TABLE orchestration_v2_projection_turn_items(turn_item_id TEXT, type TEXT, status TEXT, updated_at TEXT);
        CREATE TABLE orchestration_v2_projection_subagents(subagent_id TEXT, status TEXT, completed_at TEXT,
                                                           updated_at TEXT);
        CREATE TABLE scheduled_tasks(task_id TEXT, enabled INTEGER, next_run_at TEXT);
        CREATE TABLE auth_sessions(session_id TEXT, last_connected_at TEXT, revoked_at TEXT);
        CREATE TABLE effect_sql_migrations(migration_id INTEGER);
        INSERT INTO effect_sql_migrations VALUES (119);
    """)
    con.execute("INSERT INTO orchestration_v2_projection_runs VALUES ('a', 'completed', ?, ?)",
                (iso(now - 7200), iso(now - 3600)))
    con.execute("INSERT INTO auth_sessions VALUES ('s', ?, NULL)", (iso(now - 5400),))
    con.commit()
    return con


class Policy(unittest.TestCase):
    def test_stable_policy_refuses_nightly(self):
        doc = sb.t3_policy(dict(sb.load_config(), BRAND="", T3_CHANNEL=""))
        self.assertEqual(doc["schemaVersion"], 1)
        self.assertEqual(doc["organization"], "treehouse")
        self.assertEqual(doc["limits"], {"updates": {"mode": "auto", "allowNightly": False}})
        self.assertEqual(doc["supportUrl"], sbtestenv.SITE + "/_box/me")
        self.assertTrue(sb.t3_policy({"T3_CHANNEL": "nightly"})["limits"]["updates"]["allowNightly"])

    def test_write_keeps_someone_elses_policy(self):
        d = tempfile.mkdtemp(dir=TMP)
        path = os.path.join(d, "t3code", "policy.json")
        cfg = {"T3_CHANNEL": "stable"}
        self.assertIn("wrote", sb.write_t3_policy(cfg, path))
        self.assertEqual(sb.write_t3_policy(cfg, path), "")  # unchanged: nothing to do
        with open(path, "w") as f:
            json.dump({"schemaVersion": 1, "organization": "Example Corp"}, f)
        self.assertIn("left", sb.write_t3_policy(cfg, path))
        self.assertEqual(sb.read_json(path)["organization"], "Example Corp")


class Clients(unittest.TestCase):
    def test_counts_established_on_the_port(self):
        d = tempfile.mkdtemp(dir=TMP)
        hdr = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        with open(os.path.join(d, "tcp"), "w") as f:
            f.write(hdr + "   0: 1700A8C0:0EBE 0100007F:9C40 01 00000000:00000000 00:00000000 00000000  1000 0 1\n"
                          "   1: 1700A8C0:0EBE 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 2\n"
                          "   2: 1700A8C0:1F90 0100007F:9C41 01 00000000:00000000 00:00000000 00000000  1000 0 3\n")
        with open(os.path.join(d, "tcp6"), "w") as f:
            f.write(hdr + "   0: 00000000000000000000000000000000:0EBE 00000000000000000000000001000000:9C42 01 0 0 0 0 0 4\n")
        self.assertEqual(sb.tcp_clients(0x0EBE, d), 2)   # 3774: one IPv4 + one IPv6 established, LISTEN not counted
        self.assertEqual(sb.tcp_clients(8080, d), 1)
        self.assertEqual(sb.tcp_clients(1234, os.path.join(d, "missing")), 0)


class DbWork(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.db = os.path.join(tempfile.mkdtemp(dir=TMP), "statev2.sqlite")
        self.con = make_db(self.db, self.now)
        self.addCleanup(self.con.close)

    def test_quiet_database(self):
        work, last_work, last_client = box.t3_db_work(self.db, self.now)
        self.assertEqual(work, [])
        self.assertAlmostEqual(last_work, self.now - 3600, delta=2)
        self.assertAlmostEqual(last_client, self.now - 5400, delta=2)

    def test_running_turn_subagent_and_due_task(self):
        self.con.execute("INSERT INTO orchestration_v2_projection_runs VALUES ('b', 'running', ?, NULL)",
                         (iso(self.now - 60),))
        self.con.execute("INSERT INTO orchestration_v2_projection_subagents VALUES ('s', 'running', NULL, ?)",
                         (iso(self.now - 30),))
        self.con.execute("INSERT INTO scheduled_tasks VALUES ('t', 1, ?)", (iso(self.now + 600),))
        self.con.commit()
        work, last_work, _ = box.t3_db_work(self.db, self.now)
        self.assertIn("an agent turn is running", work)
        self.assertIn("a subagent is running", work)
        self.assertIn("a scheduled task is due", work)
        self.assertAlmostEqual(last_work, self.now - 60, delta=2)

    def test_crash_leftovers_dont_count(self):
        # a "running" row last touched two days ago is from a crash: it must not keep T3 awake forever
        self.con.execute("INSERT INTO orchestration_v2_projection_runs VALUES ('c', 'running', ?, NULL)",
                         (iso(self.now - 2 * 86400),))
        self.con.execute("INSERT INTO orchestration_v2_projection_turn_items VALUES ('i', 'subagent', 'running', ?)",
                         (iso(self.now - 2 * 86400),))
        self.con.commit()
        self.assertEqual(box.t3_db_work(self.db, self.now)[0], [])

    def test_older_t3_without_some_tables(self):
        # tables this version doesn't have (runtime requests, launch workflows here) count as nothing
        self.assertEqual(box.t3_db_work(self.db, self.now)[0], [])

    def test_unreadable_counts_as_working(self):
        bad = os.path.join(os.path.dirname(self.db), "bad.sqlite")
        with open(bad, "w") as f:
            f.write("not a database at all, but long enough to be read as one" * 20)
        work = box.t3_db_work(bad, self.now)[0]
        self.assertTrue(work and all("can't" in w for w in work))


class Tick(unittest.TestCase):
    """t3_tick's decisions with every outside call replaced."""

    def setUp(self):
        self.now = 1_800_000_000.0
        self.calls = []
        self.st = {"up": True, "socket": "active", "version": "0.1.12", "work": [], "proc": [],
                   "last_work": self.now - 4 * 3600, "last_client": self.now - 4 * 3600, "clients": 0,
                   "scheduled": False, "started": self.now - 70 * 3600, "owner": False, "ports": (20000, 20099)}
        st = self.st
        patches = {
            "shutil": box.shutil, "t3_ports": lambda: st["ports"], "is_t3_owner": lambda: st["owner"],
            "t3_env": lambda: ("100.64.0.5", 20000, {"T3CODE_PORT": "20099"}),
            "t3_up": lambda h, e: st["up"], "t3_db_work": lambda now=None: (list(st["work"]), st["last_work"],
                                                                           st["last_client"]),
            "t3_proc_work": lambda: list(st["proc"]), "t3_db_state": lambda: (False, st["scheduled"]),
            "t3_running_version": lambda: st["version"], "t3_write_units": lambda h, e: False,
            "t3_runtime_info": lambda: {"startedAt": iso(st["started"]), "serverVersion": st["version"]},
            "t3_prune_runtimes": lambda keep: [], "t3_ensure": lambda *a, **k: self.calls.append(("ensure",)),
            "sctl": self.sctl, "T3_TICK_STATE": os.path.join(tempfile.mkdtemp(dir=TMP), "t3-tick.json"),
        }
        for k, v in patches.items():
            self.addCleanup(setattr, box, k, getattr(box, k))
            setattr(box, k, v)
        for k, v in {"tailscale_ip4": lambda: "100.64.0.5", "tcp_clients": lambda port: st["clients"]}.items():
            self.addCleanup(setattr, box.sb, k, getattr(box.sb, k))
            setattr(box.sb, k, v)
        which = box.shutil.which
        box.shutil.which = lambda n, *a, **k: "/usr/local/bin/t3" if n == "t3" else which(n, *a, **k)
        self.addCleanup(setattr, box.shutil, "which", which)

    def sctl(self, *args, capture=False):
        self.calls.append(args)
        if args[0] == "restart":
            self.st["version"] = "0.1.13"
        out = "active" if args[0] == "is-active" and args[1] == box.T3_SOCKET and self.st["socket"] == "active" else ""
        return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()

    def tick(self, **plan):
        p = {"target": "0.1.13", "idle_hours": 3, "quiet_min": 20, "hackathon": False}
        p.update(plan)
        return box.t3_tick(p, now=self.now)

    def acted(self, verb):
        return [c for c in self.calls if c and c[0] == verb]

    def test_restarts_onto_the_box_version_when_quiet(self):
        out = self.tick()
        self.assertEqual(out["action"], "restarted")
        self.assertEqual(out["version"], "0.1.13")
        self.assertEqual(self.acted("restart"), [("restart", box.T3_UNIT)])  # T3 only: the socket stays

    def test_never_restarts_mid_turn(self):
        self.st["work"] = ["an agent turn is running"]
        out = self.tick()
        self.assertEqual(out["action"], "")
        self.assertEqual(self.acted("restart"), [])
        self.st["work"], self.st["proc"] = [], ["commands an agent started are still running"]
        self.assertEqual(self.tick()["action"], "")
        self.assertEqual(self.tick(busy_cpu=True)["action"], "")
        self.assertEqual(self.acted("restart"), [])

    def test_waits_for_the_quiet_stretch(self):
        self.st["last_work"] = self.now - 5 * 60  # a turn ended five minutes ago
        self.assertEqual(self.tick()["action"], "")
        self.now += 10 * 60
        self.assertEqual(self.tick()["action"], "")  # 15 minutes
        self.now += 10 * 60
        self.assertEqual(self.tick()["action"], "restarted")

    def test_same_version_is_left_alone_with_a_client(self):
        self.st["version"], self.st["clients"] = "0.1.13", 1
        out = self.tick()
        self.assertEqual((out["action"], out["clients"]), ("", 1))
        self.assertEqual(self.acted("stop"), [])

    def test_sleeps_after_idle_hours(self):
        self.st["version"] = "0.1.13"
        out = self.tick()
        self.assertEqual((out["action"], out["state"]), ("asleep", "asleep"))
        self.assertEqual(self.acted("stop"), [("stop", box.T3_PROXY, box.T3_UNIT)])  # never the socket
        self.assertEqual([c for c in self.calls if box.T3_SOCKET in c and c[0] != "is-active"], [])

    def test_no_sleep_in_hackathon_with_tasks_or_too_soon(self):
        self.st["version"] = "0.1.13"
        self.assertEqual(self.tick(hackathon=True)["action"], "")
        self.st["scheduled"] = True
        self.assertEqual(self.tick()["action"], "")
        self.st["scheduled"], self.st["last_client"] = False, self.now - 2 * 3600
        self.assertEqual(self.tick()["action"], "")
        self.assertEqual(self.tick(idle_hours=0)["action"], "")
        self.assertEqual(self.acted("stop"), [])

    def test_a_restart_counts_as_activity(self):
        # just woken (started a minute ago): the idle clock starts then, not at the last work in the database
        self.st["version"], self.st["started"] = "0.1.13", self.now - 60
        self.assertEqual(self.tick()["action"], "")

    def test_asleep_wakes_only_for_scheduled_tasks(self):
        self.st["up"] = False
        self.assertEqual(self.tick()["state"], "asleep")
        self.assertEqual(self.acted("ensure"), [])
        self.st["scheduled"] = True
        self.assertEqual(self.tick()["action"], "woke")
        self.assertEqual(len(self.acted("ensure")), 1)

    def test_owner_moves_off_t3code_service_once_quiet(self):
        self.st.update(owner=True, ports=None)
        moved = []
        for k, v in {"t3_old_installed": lambda: True, "t3_old_active": lambda: True,
                     "t3_move_owner": lambda plan: moved.append(plan) or ""}.items():
            self.addCleanup(setattr, box, k, getattr(box, k))
            setattr(box, k, v)
        out = self.tick()  # the tick hasn't watched a quiet stretch itself yet
        self.assertEqual((out["state"], out["action"]), ("legacy", ""))
        self.now += 25 * 60
        self.st["work"] = ["an agent turn is running"]
        self.assertEqual(self.tick()["action"], "")
        self.now += 10 * 60
        self.st["work"] = []
        self.assertEqual(self.tick()["action"], "")  # that turn was seen 10 minutes ago; quiet means 20
        self.now += 15 * 60
        out = self.tick()
        self.assertEqual(out["action"], "moved")
        self.assertEqual(len(moved), 1)

    def test_owner_move_failure_is_reported(self):
        self.st.update(owner=True, ports=None)
        for k, v in {"t3_old_installed": lambda: True, "t3_old_active": lambda: True,
                     "t3_move_owner": lambda plan: "sb-t3.service didn't start"}.items():
            self.addCleanup(setattr, box, k, getattr(box, k))
            setattr(box, k, v)
        self.tick()
        self.now += 3600
        out = self.tick()
        self.assertEqual(out["action"], "move-failed")
        self.assertIn("didn't start", out["why"])
        self.now += 600  # each try restarts the owner's T3: not again every tick
        out = self.tick()
        self.assertEqual(out["action"], "")
        self.assertIn("trying again later", out["why"])
        self.assertEqual(self.tick(target="0.1.14")["action"], "move-failed")  # a new version: try it now
        self.now += box.T3_MOVE_RETRY
        self.assertEqual(self.tick(target="0.1.14")["action"], "move-failed")

    def test_reply_passes_roots_check(self):
        out = self.tick()
        self.assertTrue(janitor.valid_t3_reply(json.loads(json.dumps(out))))
        self.assertFalse(janitor.valid_t3_reply(dict(out, action="rm -rf")))
        self.assertFalse(janitor.valid_t3_reply(dict(out, state="hacked")))


class JanitorPass(unittest.TestCase):
    """The tick's t3 pass: which accounts, what root passes in, what it records."""

    def setUp(self):
        self.home = tempfile.mkdtemp(dir=TMP)
        self.pws = {}
        for name, uid, units in (("alice", 2001, ["sb-t3.socket"]), ("bob", 2002, []),
                                 ("owner", 1000, ["t3code.service"])):
            d = os.path.join(self.home, name, ".config", "systemd", "user")
            os.makedirs(d)
            for u in units:
                open(os.path.join(d, u), "w").close()
            self.pws[name] = type("PW", (), {"pw_name": name, "pw_uid": uid, "pw_dir": os.path.join(self.home, name)})()
        self.plans = []
        self.events = []
        patches = [(janitor.pwd, "getpwnam", lambda n: self.pws[n]),
                   (janitor.sb, "owner_account", lambda cfg: self.pws["owner"]),
                   (janitor.sb, "busy_agents", lambda uids, seconds=0: {99: (2001, "claude")}),
                   (janitor.sb, "t3_box_version", lambda: "0.1.13"),
                   (janitor.sb, "log_event", lambda kind, user=None, **kw: self.events.append((kind, user, kw))),
                   (janitor.sb, "T3_STATE", os.path.join(self.home, "t3.json")),
                   (janitor, "user_pass", self.user_pass)]
        for obj, k, v in patches:
            self.addCleanup(setattr, obj, k, getattr(obj, k))
            setattr(obj, k, v)

    def user_pass(self, pw, plan, cmd, check):
        self.assertEqual(cmd, "_t3-tick")
        self.plans.append((pw.pw_name, plan))
        rep = {"state": "running", "version": "0.1.12" if pw.pw_name == "alice" else "0.1.14-nightly.1",
               "target": plan["target"], "action": "restarted" if pw.pw_name == "alice" else "", "why": "from 0.1.12",
               "clients": 0, "busy": [], "idle_min": 30, "owner": pw.pw_name == "owner"}
        if pw.pw_name == "owner":
            rep["state"] = "legacy"
        self.assertTrue(check(rep))
        return rep

    def test_accounts_plans_state_and_events(self):
        cfg = {"HACKATHON": "1", "T3_IDLE_STOP_HOURS": "3", "T3_RESTART_QUIET_MIN": "20", "T3_CHANNEL": ""}
        janitor.t3_pass(cfg, {"alice": {}, "bob": {}})
        self.assertEqual([n for n, _ in self.plans], ["alice", "owner"])  # bob has no T3 socket
        plans = dict(self.plans)
        self.assertTrue(plans["alice"]["busy_cpu"])
        self.assertFalse(plans["owner"]["busy_cpu"])
        self.assertEqual((plans["alice"]["target"], plans["alice"]["hackathon"], plans["alice"]["idle_hours"]),
                         ("0.1.13", True, 3.0))
        with open(janitor.sb.T3_STATE) as f:
            st = json.load(f)
        self.assertEqual(st["target"], "0.1.13")
        self.assertEqual(st["accounts"]["owner"]["state"], "legacy")
        self.assertEqual(st["accounts"]["alice"]["last"]["action"], "restarted")
        self.assertEqual([(k, u) for k, u, _ in self.events], [("t3_restarted", "alice")])


class OwnerPorts(unittest.TestCase):
    def test_defaults_and_config(self):
        cfg = dict(box.CFG)
        self.addCleanup(lambda: (box.CFG.clear(), box.CFG.update(cfg)))
        box.CFG.update(OWNER_T3_PORT="", OWNER_T3_INNER_PORT="")
        self.assertEqual(box.owner_t3_ports(), (3773, 3774))
        box.CFG.update(OWNER_T3_PORT="3773", OWNER_T3_INNER_PORT="3790")
        self.assertEqual(box.owner_t3_ports(), (3773, 3790))


class Versions(unittest.TestCase):
    def test_machines_line(self):
        os.environ.setdefault("SHAREDBOX_WEB_NO_START", "1")
        web = load("web_t3idle", os.path.join(LIB, "sharedbox-web"))
        d = web.clean_summary({"v": 1, "t3_target": "0.1.13", "t3_accounts": {
            "alice": {"v": "0.1.13", "s": "asleep"}, "bob": {"v": "0.1.12", "s": "running"},
            "pippin": {"v": "0.1.14-nightly.20261128.5372", "s": "legacy"}, "../x": {"v": "1.0.0", "s": "running"},
            "eve": {"v": "<script>", "s": "pwned"}}})
        self.assertNotIn("../x", d["t3_accounts"])
        self.assertEqual(d["t3_accounts"]["eve"], {"v": "", "s": ""})
        line = web.t3_versions_line(d, {"bob": "Bob"})
        self.assertIn("0.1.13", line)
        self.assertIn("Bob <code>0.1.12</code>", line)
        self.assertIn("T3's own service", line)
        self.assertIn("1 of 4 asleep", line)
        same = web.t3_versions_line({"t3_target": "0.1.13", "t3_accounts": {"a": {"v": "0.1.13", "s": "running"}}}, {})
        self.assertIn("on every account.", same)


if __name__ == "__main__":
    unittest.main()
