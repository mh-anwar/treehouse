"""box team: board appends, claims, concurrency, inotify wait latency, relay routing, crew triggers.

Run: python3 -m unittest tests/test_team.py   (no root; uses a temp dir as the team folder)
"""

import multiprocessing
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
import sbteam  # noqa: E402

ME = sbteam.me()


def _hammer(tdir, n, tag):
    for i in range(n):
        sbteam.append(tdir, {"type": "msg", "by": ME, "as": tag, "text": f"{tag}-{i}"})


class TeamTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHAREDBOX_TEAMS"] = self.tmp.name
        self.tdir = os.path.join(self.tmp.name, "alpha")
        os.mkdir(self.tdir)
        open(os.path.join(self.tdir, sbteam.BOARD), "w").close()

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("SHAREDBOX_TEAMS", None)

    def post(self, **ev):
        ev.setdefault("by", ME)
        return sbteam.append(self.tdir, ev)

    def test_post_claim_done_board(self):
        t = self.post(type="task", text="build login page", **{"as": "a1"})
        self.assertEqual(t["id"], 1)
        self.post(type="claim", task=1, **{"as": "a1"})
        self.post(type="done", task=1, text="branch login", **{"as": "a1"})
        st = sbteam.fold(sbteam.read_events(self.tdir)[0])
        self.assertEqual(st["tasks"][1]["state"], "done")
        self.assertEqual(st["tasks"][1]["result"], "branch login")
        self.assertIn("alpha", sbteam.board_text("alpha", self.tdir))
        s = sbteam.summary("alpha")
        self.assertEqual(s["last_id"], 3)
        self.assertEqual(len(s["done"]), 1)
        self.assertEqual(sbteam.my_teams(), ["alpha"])

    def test_claim_conflict(self):
        self.post(type="task", text="api", **{"as": "a1"})
        self.post(type="claim", task=1, **{"as": "a1"})
        with self.assertRaises(ValueError) as cm:
            self.post(type="claim", task=1, **{"as": "a2"})
        self.assertIn("claimed by", str(cm.exception))
        self.post(type="claim", task=1, steal=True, **{"as": "a2"})  # explicit steal works
        with self.assertRaises(ValueError):
            self.post(type="claim", task=99)
        self.post(type="done", task=1, **{"as": "a2"})
        with self.assertRaises(ValueError):
            self.post(type="done", task=1, **{"as": "a2"})

    def test_lock_conflict(self):
        self.post(type="lock", path="src/app.ts", text="refactor", **{"as": "a1"})
        with self.assertRaises(ValueError):
            self.post(type="lock", path="src/app.ts", **{"as": "a2"})
        self.post(type="unlock", path="src/app.ts", **{"as": "a1"})
        self.post(type="lock", path="src/app.ts", **{"as": "a2"})

    def test_concurrent_appends_flock(self):
        procs = [multiprocessing.Process(target=_hammer, args=(self.tdir, 50, f"p{i}")) for i in range(6)]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
        evs = sbteam.read_events(self.tdir)[0]
        self.assertEqual(len(evs), 300)
        self.assertEqual([e["id"] for e in evs], list(range(1, 301)))  # sequential, no gaps or dupes

    def test_wait_latency(self):
        lat = []
        for _ in range(5):
            res = {}

            def waiter():
                res["hits"] = sbteam.wait_for(self.tdir, "bob", "", "me", None, 10)
                res["t"] = time.monotonic()

            th = threading.Thread(target=waiter)
            th.start()
            time.sleep(0.15)  # let it block in inotify
            t0 = time.monotonic()
            self.post(type="msg", to="bob", text="your turn")
            th.join(5)
            self.assertTrue(res.get("hits"))
            lat.append(res["t"] - t0)
        print(f"\nwait latency ms: {[round(x * 1000, 1) for x in lat]}", file=sys.stderr)
        self.assertLess(max(lat), 0.2)

    def test_wait_filters_and_task_mode(self):
        self.post(type="task", text="x", to="bob", **{"as": "lead"})
        res = {}
        th = threading.Thread(target=lambda: res.update(h=sbteam.wait_for(self.tdir, ME, "lead", "task:1", None, 5)))
        th.start()
        time.sleep(0.1)
        self.post(type="msg", to="carol", text="unrelated")  # must not wake it
        self.post(type="claim", task=1, by="bob")
        time.sleep(0.1)
        self.assertTrue(th.is_alive())
        self.post(type="done", task=1, by="bob", text="shipped")
        th.join(5)
        self.assertEqual(res["h"][0]["text"], "shipped")
        self.assertEqual(sbteam.wait_for(self.tdir, "bob", "", "me", None, 0.05), [])  # timeout

    def test_relevant_routing(self):
        e = {"id": 1, "type": "msg", "by": "alice", "to": "bob", "text": "hi"}
        self.assertTrue(sbteam.relevant(e, "bob"))
        self.assertFalse(sbteam.relevant(e, "carol"))
        self.assertTrue(sbteam.relevant({"id": 2, "type": "task", "by": "alice"}, "bob"))  # unassigned task
        self.assertFalse(sbteam.relevant({"id": 3, "type": "msg", "by": "bob", "to": "bob"}, "bob"))  # own

    def test_relay_once(self):
        os.environ["HOME"], home = self.tmp.name, os.environ.get("HOME")
        try:
            with open(sbteam.inbox_path("alpha"), "w") as f:
                f.write('{"threads": {"": "thread-default-1234"}}')
            sent = []
            th = threading.Thread(target=lambda: sent.append(
                sbteam.run_relay("alpha", self.tdir, send=lambda t, x: sent.append((t, x)), once=True)))
            th.start()
            time.sleep(0.15)
            self.post(type="task", text="review PR", to=ME, by="alice")
            th.join(10)
            self.assertEqual(sent[0][0], "thread-default-1234")
            self.assertIn("review PR", sent[0][1])
        finally:
            os.environ["HOME"] = home or ""

    def test_crew_triggers(self):
        old = time.time() - 3600
        evs = [{"id": 1, "ts": old, "type": "task", "by": "a", "text": "build the login page"},
               {"id": 2, "ts": old, "type": "claim", "by": "a", "task": 1},
               {"id": 3, "ts": time.time(), "type": "task", "by": "b", "text": "build login page"}]
        why = sbteam.crew_triggers("foreman", evs, 0)
        self.assertTrue(any("no update" in w for w in why))
        self.assertTrue(any("same work" in w for w in why))
        self.assertEqual(sbteam.crew_triggers("comms", evs, 0), [])
        evs.append({"id": 4, "ts": time.time(), "type": "done", "by": "a", "task": 1})
        self.assertTrue(sbteam.crew_triggers("comms", evs, 0))
        self.assertIn("haiku", sbteam.crew_cmd("claude", None, "p", "/x"))


if __name__ == "__main__":
    unittest.main()
