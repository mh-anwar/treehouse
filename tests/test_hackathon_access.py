"""Hackathon access (who may use which machines while hackathon mode is on): the table's rules (lib/sbcommon.py),
the primary's signed roster carrying it (lib/sbfed.py), `box hackathon assign|...` edits, the janitor's per-person
plans, the owner's validation in sharedbox-admin and the Settings grid. Stdlib only, no root.
    python3 -m unittest tests.test_hackathon_access -v"""

import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
import sbcommon as sb  # noqa: E402
import sbfed  # noqa: E402

TMP = tempfile.mkdtemp(prefix="sbaccess-")


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    argv, sys.argv = sys.argv, [name]
    try:
        loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


box = load("box_access", os.path.join(LIB, "box"))
janitor = load("janitor_access", os.path.join(LIB, "sharedbox-janitor"))
admin = load("admin_access", os.path.join(LIB, "sharedbox-admin"))
web = load("web_access", os.path.join(LIB, "sharedbox-web"))


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


TABLE = {"v": 1, "open": ["sietchtabr"], "teams": {"A": {"members": ["alice", "bob"], "machines": ["minastirith"]}},
         "people": {"carol": ["nar-shaddaa"]}, "servers": {"web": ["alice"]}}


class Rules(unittest.TestCase):
    def test_machines_per_person(self):
        self.assertEqual(sb.access_machines(TABLE, "alice"), ["minastirith", "sietchtabr"])
        self.assertEqual(sb.access_machines(TABLE, "carol"), ["nar-shaddaa", "sietchtabr"])
        self.assertIsNone(sb.access_machines(TABLE, "dave"))  # not named: every machine
        self.assertIsNone(sb.access_machines(None, "alice"))
        self.assertTrue(sb.access_allowed(TABLE, "alice", "sietchtabr"))
        self.assertFalse(sb.access_allowed(TABLE, "alice", "nar-shaddaa"))
        self.assertTrue(sb.access_allowed(TABLE, "dave", "nar-shaddaa"))
        self.assertTrue(sb.access_server_allowed(TABLE, "alice", "web"))
        self.assertFalse(sb.access_server_allowed(TABLE, "bob", "web"))
        self.assertTrue(sb.access_server_allowed(TABLE, "bob", "other"))

    def test_clean_drops_and_reports(self):
        doc, probs = sb.clean_access({"open": ["sietchtabr", "../etc"], "people": {"carol": ["ghost"], "BAD!": []},
                                      "teams": {"A": {"members": ["alice", "zed"], "machines": ["minastirith"]},
                                                "x" * 80: {}}, "servers": {"web": ["alice"]}, "junk": 1},
                                     machines={"sietchtabr", "minastirith"}, people={"alice", "carol"},
                                     servers={"web"})
        self.assertEqual(doc["open"], ["sietchtabr"])
        self.assertEqual(doc["teams"], {"A": {"members": ["alice"], "machines": ["minastirith"]}})
        self.assertEqual(doc["people"], {"carol": []})
        self.assertNotIn("junk", doc)
        self.assertTrue(any("ghost" in p for p in probs) and any("zed" in p for p in probs))
        self.assertEqual(sb.clean_access("nonsense")[0]["teams"], {})


class Publish(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(dir=TMP)
        self.active = os.path.join(self.d, "active.json")
        for k, v in {"HACKATHON_ACCESS": os.path.join(self.d, "table.json")}.items():
            self.addCleanup(setattr, sb, k, getattr(sb, k))
            setattr(sb, k, v)

    def test_primary_writes_while_on_and_removes_after(self):
        sb.write_json(sb.HACKATHON_ACCESS, TABLE)
        until = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 3600))
        cfg = {"HACKATHON_UNTIL": until, "BOXES": "sietchtabr=100.105.248.23 minastirith=100.94.44.25"}
        self.assertTrue(sb.access_publish(cfg, path=self.active))
        doc = sb.access_active(path=self.active)
        self.assertEqual(doc["teams"], TABLE["teams"])
        self.assertEqual(doc["boxes"]["minastirith"], "100.94.44.25")
        self.assertGreater(doc["until"], time.time())
        self.assertFalse(sb.access_publish(cfg, path=self.active))  # unchanged
        self.assertIsNone(sb.access_active(now=doc["until"] + 1, path=self.active))  # past its end: ignored
        self.assertTrue(sb.access_publish(dict(cfg, HACKATHON_UNTIL=""), path=self.active))
        self.assertFalse(os.path.exists(self.active))

    def test_secondary_follows_the_primary(self):
        cfg = {"BOX_ROLE": "secondary"}
        self.assertFalse(sb.access_publish(cfg, path=self.active))  # on its own it publishes nothing
        self.assertTrue(sb.access_publish(cfg, doc=TABLE, until=int(time.time()) + 600, path=self.active))
        self.assertTrue(sb.access_publish(cfg, doc={}, until=None, path=self.active))
        self.assertFalse(os.path.exists(self.active))

    def test_hackathon_end_clears_the_table(self):
        sb.write_json(sb.HACKATHON_ACCESS, TABLE)
        state = os.path.join(sb.STATE_DIR, "hackathon.json")
        sb.write_json(state, {"active": True})
        orig = sb.HACKATHON_ACCESS_ACTIVE
        self.addCleanup(setattr, sb, "HACKATHON_ACCESS_ACTIVE", orig)
        sb.HACKATHON_ACCESS_ACTIVE = self.active
        self.assertEqual(sb.hackathon_reconcile({"HACKATHON": "", "HACKATHON_UNTIL": ""}), "off")
        self.assertFalse(os.path.exists(sb.HACKATHON_ACCESS))


class Roster(unittest.TestCase):
    def test_only_a_primary_carries_it_and_only_until_its_end(self):
        now = int(time.time())
        a = dict(TABLE, until=now + 600, boxes={"x": "1.2.3.4"})
        doc = sbfed.build_roster("sietchtabr", "primary", {}, 1, now, access=a)
        self.assertEqual(doc["hackathon_access"]["until"], now + 600)
        self.assertNotIn("boxes", doc["hackathon_access"])  # each box uses its own BOXES for links
        self.assertEqual(sbfed.roster_access(doc, now)[1], now + 600)
        self.assertEqual(sbfed.roster_access(doc, now + 601), (None, None))
        sec = sbfed.build_roster("minastirith", "secondary", {}, 1, now, access=a)
        self.assertNotIn("hackathon_access", sec)
        self.assertEqual(sbfed.roster_access(dict(sec, hackathon_access=doc["hackathon_access"]), now), (None, None))
        self.assertTrue(sbfed.check_roster(doc, "sietchtabr", 0, now)[0])


class Cli(unittest.TestCase):
    def change(self, op, args=(), team=None, everyone=False, doc=None):
        return box.access_change(SimpleNamespace(op=op, args=list(args), team=team, everyone=everyone),
                                 doc if doc is not None else TABLE)

    def test_assign_team_and_person(self):
        d = self.change("assign", ["dave,erin", "nar-shaddaa"], team="B")
        self.assertEqual(d["teams"]["B"], {"members": ["dave", "erin"], "machines": ["nar-shaddaa"]})
        d = self.change("assign", ["bob", "nar-shaddaa"], doc=d)  # one place per person: out of team A
        self.assertEqual(d["teams"]["A"]["members"], ["alice"])
        self.assertEqual(d["people"]["bob"], ["nar-shaddaa"])
        d = self.change("assign", ["carol", "minastirith"], team="A", doc=d)
        self.assertNotIn("carol", d["people"])
        self.assertEqual(TABLE["teams"]["A"]["members"], ["alice", "bob"])  # the original is untouched

    def test_unassign_open_server_clear(self):
        d = self.change("unassign", ["alice,bob"])
        self.assertNotIn("A", d["teams"])  # an empty team goes
        self.assertEqual(self.change("unassign", team="A")["teams"], {})
        self.assertEqual(self.change("open", ["none"])["open"], [])
        self.assertEqual(self.change("open", ["sietchtabr,nar-shaddaa"])["open"], ["sietchtabr", "nar-shaddaa"])
        self.assertEqual(self.change("server", ["web"], everyone=True)["servers"], {})
        self.assertEqual(self.change("server", ["web", "bob"])["servers"], {"web": ["bob"]})
        self.assertEqual(self.change("clear"), {"v": 1, "open": [], "teams": {}, "people": {}, "servers": {}})
        with self.assertRaises(SystemExit):
            self.change("assign", ["alice"])

    def test_lines(self):
        text = "\n".join(box.access_lines(TABLE, True, "2026-11-29 18:00"))
        self.assertIn("team A: alice, bob -> minastirith", text)
        self.assertIn("everyone keeps: sietchtabr", text)
        self.assertIn("applies now", text)
        self.assertIn("no limits", box.access_lines({}, False)[0])


class JanitorPlans(unittest.TestCase):
    def test_who_is_closed_here(self):
        home = tempfile.mkdtemp(dir=TMP)
        users = {}
        for n, uid in (("alice", 2001), ("carol", 2003), ("dave", 2004), ("gone", 2005)):
            d = os.path.join(home, n, ".config", "systemd", "user")
            os.makedirs(d)
            if n != "gone":
                open(os.path.join(d, "sb-t3.socket"), "w").close()
            users[n] = {"uid": uid, "home": os.path.join(home, n), "status": "active"}
        cfg = {"FRIEND_UID_MIN": "2000", "PORT_BASE": "20000", "PORT_BLOCK": "100", "BOXES": ""}
        doc = dict(TABLE, until=int(time.time()) + 3600,
                   boxes={"sietchtabr": "100.105.248.23", "minastirith": "100.94.44.25", "nar-shaddaa": "100.70.243.69"})
        plans = janitor.access_plans(cfg, users, doc, "minastirith")
        self.assertNotIn("gone", plans)  # no T3 here
        self.assertFalse(plans["alice"]["closed"])  # team A works here
        self.assertTrue(plans["carol"]["closed"])
        self.assertEqual(plans["carol"]["use"], [{"name": "nar-shaddaa", "url": "http://100.70.243.69:20300/"},
                                                 {"name": "sietchtabr", "url": "http://100.105.248.23:20300/"}])
        self.assertFalse(plans["dave"]["closed"])  # not named: every machine
        self.assertFalse(any(p["closed"] for p in janitor.access_plans(cfg, users, None, "minastirith").values()))


class AdminSet(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(dir=TMP)
        patches = [(admin.sb, "HACKATHON_ACCESS", os.path.join(d, "table.json")),
                   (admin.sb, "HACKATHON_ACCESS_ACTIVE", os.path.join(d, "active.json")),
                   (admin.sb, "friend_users", lambda reg=None: {n: {} for n in ("alice", "bob", "carol")}),
                   (admin.sb, "log_event", lambda *a, **k: None),
                   (admin, "access_apply_soon", lambda: None),
                   (admin.sbshared, "registry", lambda: {"web": {}})]
        for obj, k, v in patches:
            self.addCleanup(setattr, obj, k, getattr(obj, k))
            setattr(obj, k, v)
        self.cfg = {"BOXES": "sietchtabr=100.105.248.23 minastirith=100.94.44.25", "ADMIN_USERS": "pippin",
                    "OWNER_T3_USER": "", "HACKATHON_UNTIL": ""}

    def set(self, doc):
        return admin.hackathon_set(self.cfg, {"op": "access-set", "doc": doc}, "pippin")

    def test_saves_and_refuses_bad_tables(self):
        ok = self.set({"open": ["sietchtabr"], "teams": {"A": {"members": ["alice"], "machines": ["minastirith"]}}})
        self.assertTrue(ok["ok"], ok)
        self.assertFalse(ok["active"])  # hackathon mode is off: saved for later
        self.assertIn("while hackathon mode is on", ok["message"])
        self.assertEqual(sb.read_json(admin.sb.HACKATHON_ACCESS)["set_by"], "pippin")
        bad = self.set({"people": {"alice": ["nar-shaddaa"]}})
        self.assertFalse(bad["ok"])
        self.assertIn("nar-shaddaa", bad["error"])
        self.assertFalse(self.set({"people": {"bob": []}})["ok"])  # no machine at all
        self.assertFalse(self.set({"people": {"pippin": ["sietchtabr"]}})["ok"])  # not a friend (the owner)
        cleared = admin.hackathon_set(self.cfg, {"op": "access-clear"}, "pippin")
        self.assertTrue(cleared["ok"])
        self.assertFalse(os.path.exists(admin.sb.HACKATHON_ACCESS))

    def test_secondary_points_at_the_primary(self):
        res = admin.hackathon_set(dict(self.cfg, BOX_ROLE="secondary"), {"op": "access-set", "doc": {}}, "pippin")
        self.assertFalse(res["ok"])
        self.assertIn("main machine", res["error"])


class Grid(unittest.TestCase):
    def test_form_to_table_keeps_unchanged_teams(self):
        d = tempfile.mkdtemp(dir=TMP)
        patches = [(web.sb, "HACKATHON_ACCESS", os.path.join(d, "t.json")),
                   (web.sb, "load_config", lambda *a: {"BOXES": "sietchtabr=100.105.248.23 minastirith=100.94.44.25"}),
                   (web.sb, "friend_users", lambda reg=None: {n: {} for n in ("alice", "bob", "carol", "dave")})]
        for obj, k, v in patches:
            self.addCleanup(setattr, obj, k, getattr(obj, k))
            setattr(obj, k, v)
        sb.write_json(web.sb.HACKATHON_ACCESS, {"teams": {"A": {"members": ["alice", "bob"], "machines": ["minastirith"]},
                                                         "B": {"members": ["carol"], "machines": ["minastirith"]}},
                                               "servers": {"web": ["alice"]}})
        form = {"ao_sietchtabr": "1", "aw_alice__minastirith": "1", "aw_bob__minastirith": "1",
                "aw_carol__sietchtabr": "1"}
        doc = web.access_from_form(form)
        self.assertEqual(doc["open"], ["sietchtabr"])
        self.assertEqual(list(doc["teams"]), ["A"])  # B's member moved: it becomes per-person
        self.assertEqual(doc["people"], {"carol": ["sietchtabr"]})
        self.assertEqual(doc["servers"], {"web": ["alice"]})


if __name__ == "__main__":
    unittest.main()
