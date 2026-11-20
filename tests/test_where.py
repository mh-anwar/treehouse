"""box where: own-status changes, name lookup, expiry, the rounded activity hint, privacy.

Run: python3 -m unittest tests/test_where.py   (no root; a temp presence.json)
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401
import sbmsg  # noqa: E402
import sbwhere  # noqa: E402


class WhereTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "presence.json")
        self.now = time.time()

    def tearDown(self):
        shutil.rmtree(self.d)

    def set(self, user, **req):
        return sbwhere.handle(user, req, now=self.now, p=self.p, as_root=False)

    def test_set_find_describe(self):
        self.set("pippin", name="Mohammad Anwar")
        res = self.set("pippin", status="In class", until=int(self.now + 3600))
        self.assertIn("Mohammad Anwar (pippin): In class, until", res["message"])
        d = sbwhere.load(self.p)
        people = ["pippin", "theclockkeeper"]
        for q in ("mohammad", "Mohammad Anwar", "anwar", "pippin", "PIP"):
            self.assertEqual(sbwhere.find(q, people, d), ["pippin"], q)
        self.assertEqual(sbwhere.find("krish", people, d), [])

    def test_status_expires_and_clears(self):
        self.set("bob", status="Asleep", until=int(self.now + 60))
        rec = sbwhere.load(self.p)["bob"]
        self.assertEqual(sbwhere.view("bob", rec, self.now)["status"], "Asleep")
        self.assertNotIn("status", sbwhere.view("bob", rec, self.now + 120))
        self.set("bob", status="Back")  # a new status drops the old until
        self.assertNotIn("until", sbwhere.load(self.p)["bob"])
        self.set("bob", clear=True)
        self.assertNotIn("status", sbwhere.load(self.p)["bob"])

    def test_refusals_and_scrub(self):
        for req in ({}, {"status": ""}, {"status": "x", "until": self.now - 5}, {"until": self.now + 60},
                    {"status": "token ghp_" + "a" * 36}):
            with self.assertRaises(sbmsg.MsgError, msg=req):
                self.set("bob", **req)
        self.set("bob", status="see https://x.example/y `rm -rf` " + "z" * 200)
        st = sbwhere.load(self.p)["bob"]["status"]
        self.assertNotIn("https://", st)
        self.assertNotIn("`", st)
        self.assertLessEqual(len(st), sbwhere.STATUS_MAX)

    def test_activity_hint_is_rounded_and_private_hides_it(self):
        home = os.path.join(self.d, "home")
        f = os.path.join(home, ".claude", "projects", "proj", "s.jsonl")
        os.makedirs(os.path.dirname(f))
        open(f, "w").close()
        os.utime(f, (self.now - 3 * 3600, self.now - 3 * 3600))
        os.symlink("/etc", os.path.join(home, ".claude", "projects", "link"))  # never followed
        sbwhere.refresh_activity({"bob": home}, now=self.now, p=self.p, as_root=False)
        v = sbwhere.view("bob", sbwhere.load(self.p)["bob"], self.now)
        self.assertEqual((v["activity"], v["active_now"]), ("active 3h ago", False))
        os.utime(f, None)
        sbwhere.refresh_activity({"bob": home}, now=time.time(), p=self.p, as_root=False)
        self.assertTrue(sbwhere.view("bob", sbwhere.load(self.p)["bob"], time.time())["active_now"])
        self.set("bob", private=True)
        sbwhere.refresh_activity({"bob": home}, now=self.now, p=self.p, as_root=False)
        rec = sbwhere.load(self.p)["bob"]
        self.assertNotIn("active", rec)
        self.assertEqual(sbwhere.view("bob", rec, self.now)["activity"], "private")
        sbwhere.refresh_activity({}, now=self.now, p=self.p, as_root=False)  # account gone: entry gone
        self.assertEqual(sbwhere.load(self.p), {})

    def test_bare_times(self):
        base = time.mktime((2026, 11, 18, 14, 0, 0, 0, 0, -1))
        t = time.localtime(sbmsg.parse_due("15:00", base))
        self.assertEqual((t.tm_mday, t.tm_hour), (18, 15))
        t = time.localtime(sbmsg.parse_due("9am", base))  # already past today: tomorrow
        self.assertEqual((t.tm_mday, t.tm_hour), (19, 9))
        t = time.localtime(sbmsg.parse_due("3pm", base))
        self.assertEqual(t.tm_hour, 15)


if __name__ == "__main__":
    unittest.main()
