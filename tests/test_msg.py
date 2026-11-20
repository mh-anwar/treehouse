"""box msg: scrubbing, due dates, delivery rules (sender identity, replies, dedup, limits) and the CLI.

Run: python3 -m unittest tests/test_msg.py   (no root; delivery runs with as_root=False in a temp state dir)
"""

import argparse
import contextlib
import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
import sbcommon as sb  # noqa: E402
import sbmsg  # noqa: E402

PEOPLE = {"alice", "bob", "carol"}
CTX = {"context": "CI on main fails since the last merge"}


class ScrubTest(unittest.TestCase):
    def test_title_is_one_plain_short_line(self):
        t, n = sbmsg.scrub_title("Fix `CI`\nnow: see https://evil.example/x?y=1\x1b[31m and more " + "x" * 80)
        self.assertNotIn("\n", t)
        self.assertNotIn("`", t)
        self.assertNotIn("https://", t)
        self.assertNotIn("\x1b", t)
        self.assertIn("[link]", t)
        self.assertLessEqual(len(t), sbmsg.TITLE_MAX)
        self.assertTrue(t.endswith("..."))
        self.assertEqual(n, 0)

    def test_secrets_are_masked(self):
        for secret in ("ghp_" + "a1B2" * 9, "sk-ant-" + "x" * 40, "AKIA" + "ABCDEFGHIJKLMNOP",
                       "123456789:" + "A" * 35, "eyJhbGciOiJI.eyJzdWIiOiIx.SflKxwRJSMeKKF2"):
            out, n = sbmsg.mask(f"use {secret} please")
            self.assertNotIn(secret, out, secret)
            self.assertEqual(n, 1, secret)
        out, n = sbmsg.mask("password=hunter2hunter2 and https://bob:pw123@git.example.com/r")
        self.assertNotIn("hunter2", out)
        self.assertNotIn("pw123", out)
        self.assertEqual(n, 2)

    def test_git_shas_and_plain_text_survive(self):
        text = "broken since 9e85354a1b2c3d4e5f60718293a4b5c6d7e8f901, see tests/test_x.py"
        self.assertEqual(sbmsg.mask(text), (text, 0))

    def test_brief_is_checked_and_masked(self):
        b, n = sbmsg.clean_brief({"context": "token: abcdef123456", "links": ["a", "b"], "n": 3})
        self.assertEqual(n, 1)
        self.assertNotIn("abcdef123456", b["context"])
        for bad in ({"bad key!": 1}, {"x": {"y": {"z": {"w": {"v": {"u": 1}}}}}}, {"l": list(range(51))},
                    {"t": "x" * (sbmsg.TEXT_MAX + 1)}):
            with self.assertRaises(sbmsg.MsgError):
                sbmsg.clean_brief(bad)


class DueTest(unittest.TestCase):
    NOW = datetime.datetime(2026, 11, 18, 22, 0).astimezone().timestamp()  # a Wednesday

    def at(self, s):
        return datetime.datetime.fromtimestamp(sbmsg.parse_due(s, self.NOW)).astimezone()

    def test_forms(self):
        self.assertIsNone(sbmsg.parse_due("", self.NOW))
        self.assertEqual(sbmsg.parse_due("+2h", self.NOW), int(self.NOW + 7200))
        self.assertEqual(sbmsg.parse_due("in 3 days", self.NOW), int(self.NOW + 3 * 86400))
        self.assertEqual(sbmsg.parse_due("45m", self.NOW), int(self.NOW + 2700))
        t = self.at("tomorrow 18:00")
        self.assertEqual((t.day, t.hour, t.minute), (19, 18, 0))
        t = self.at("tomorrow 6pm")
        self.assertEqual((t.day, t.hour), (19, 18))
        t = self.at("fri")
        self.assertEqual((t.day, t.hour, t.minute), (20, 23, 59))
        t = self.at("wed")  # today is Wednesday: next week's
        self.assertEqual(t.day, 25)
        t = self.at("2026-12-01")
        self.assertEqual((t.month, t.day, t.hour), (12, 1, 23))
        t = self.at("2026-12-01 09:30")
        self.assertEqual((t.hour, t.minute), (9, 30))
        self.assertIsInstance(sbmsg.parse_due("2026-12-01T09:30:00Z", self.NOW), int)

    def test_bad_input(self):
        for s in ("someday", "tomorrow 25:00", "2026-13-01"):
            with self.assertRaises(ValueError):
                sbmsg.parse_due(s, self.NOW)

    def test_fmt(self):
        self.assertEqual(sbmsg.fmt_due(sbmsg.parse_due("tomorrow 18:00", self.NOW), self.NOW), "tomorrow 18:00")
        self.assertEqual(sbmsg.fmt_due(self.NOW - 7200, self.NOW), "overdue 2h")


class DeliverTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.now = time.time()

    def tearDown(self):
        shutil.rmtree(self.root)

    def send(self, sender, **req):
        req.setdefault("brief", dict(CTX))
        return sbmsg.deliver(sender, req, PEOPLE, now=self.now, root=self.root, as_root=False)

    def inbox(self, user):
        return sbmsg.list_dir(os.path.join(self.root, user))

    def test_send_files_message_and_sent_copy(self):
        res = self.send("alice", to="bob", title="Fix legacy-siege CI", level="high", due=int(self.now + 3600))
        self.assertTrue(res["ok"])
        [m] = self.inbox("bob")
        self.assertEqual((m["from"], m["to"], m["level"], m["thread"]), ("alice", "bob", "high", m["id"]))
        self.assertEqual(sbmsg.list_dir(os.path.join(self.root, "alice", "sent"))[0]["id"], m["id"])
        self.assertEqual(self.inbox("alice"), [])

    def test_sender_cant_be_faked(self):
        self.send("alice", to="bob", title="hi", **{"from": "carol"})
        self.assertEqual(self.inbox("bob")[0]["from"], "alice")

    def test_refusals(self):
        cases = [dict(to="mallory", title="x"), dict(to="../bob", title="x"), dict(to="bob", title=""),
                 dict(to="bob", title="x", level="urgent!!"), dict(to="bob", title="x", brief={}),
                 dict(to="bob", title="x", brief="text"), dict(to="bob", title="x", due=self.now + 900 * 86400),
                 dict(to="bob", title="x", status="done")]
        for req in cases:
            with self.assertRaises(sbmsg.MsgError, msg=req):
                self.send("alice", **req)
        with self.assertRaises(sbmsg.MsgError):
            sbmsg.deliver("mallory", {"to": "bob", "title": "x", "brief": CTX}, PEOPLE, root=self.root, as_root=False)

    def test_reply_goes_back_to_sender_only_from_recipient(self):
        mid = self.send("alice", to="bob", title="Fix CI")["id"]
        with self.assertRaises(sbmsg.MsgError):  # carol never got it
            self.send("carol", reply_to=mid, status="done")
        with self.assertRaises(sbmsg.MsgError):  # nor can alice reply to her own sent copy
            self.send("alice", reply_to=mid)
        res = self.send("bob", reply_to=mid, status="done", brief={"pr_url": "https://github.com/x/y/pull/1"},
                        to="carol")  # `to` is ignored on a reply
        [r] = self.inbox("alice")
        self.assertEqual((r["from"], r["status"], r["reply_to"], r["thread"]), ("bob", "done", mid, mid))
        self.assertEqual(r["title"], "Done: Fix CI")
        self.assertEqual(self.inbox("carol"), [])
        self.assertEqual(res["to"], "alice")

    def test_same_card_within_an_hour_is_bumped(self):
        a = self.send("alice", to="bob", title="Fix CI")
        b = self.send("alice", to="bob", title="Fix CI", brief={"context": "still failing"})
        self.assertEqual(a["id"], b["id"])
        [m] = self.inbox("bob")
        self.assertEqual((m["count"], m["brief"]["context"]), (2, "still failing"))
        self.now += sbmsg.DEDUP_SECONDS + 1
        self.send("alice", to="bob", title="Fix CI")
        self.assertEqual(len(self.inbox("bob")), 2)

    def test_rate_limits(self):
        for i in range(sbmsg.CRITICAL_DAY):
            self.send("alice", to="bob", title=f"fire {i}", level="critical")
        with self.assertRaises(sbmsg.MsgError) as e:
            self.send("alice", to="bob", title="one more fire", level="critical")
        self.assertIn("high", str(e.exception))
        self.send("alice", to="bob", title="one more fire", level="high")
        for i in range(sbmsg.RATE_HOUR - sbmsg.CRITICAL_DAY - 1):
            self.send("alice", to="carol", title=f"n {i}")
        with self.assertRaises(sbmsg.MsgError):
            self.send("alice", to="carol", title="too many")
        self.send("bob", to="alice", title="others aren't limited")

    def test_masking_is_reported(self):
        res = self.send("alice", to="bob", title="key ghp_" + "Z" * 36,
                        brief={"context": "use password=supersecret1"})
        self.assertEqual(res["masked"], 2)
        with open(os.path.join(self.root, "bob", res["id"] + ".json")) as f:
            raw = f.read()
        self.assertNotIn("supersecret1", raw)
        self.assertNotIn("Z" * 36, raw)


class CliTest(unittest.TestCase):
    """The real `box msg` argument parser, with root's delivery swapped for a direct call."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        shutil.rmtree(sbmsg.mail_root(), ignore_errors=True)
        self._resolve = sbmsg.resolve
        sbmsg.resolve = lambda sb_, cfg, q: q if q in PEOPLE else self._raise(q)

    def _raise(self, q):
        raise sbmsg.MsgError(f"nobody called {q!r} here; `box msg who` lists people")

    def tearDown(self):
        sbmsg.resolve = self._resolve
        shutil.rmtree(self.home)
        shutil.rmtree(sbmsg.mail_root(), ignore_errors=True)

    def box(self, user, *argv):
        p = argparse.ArgumentParser()
        s = p.add_subparsers(dest="sub")
        sbmsg.add_parsers(s, sb, sb.load_config(), user, os.path.join(self.home, user),
                          lambda req, wait=30: self.deliver(user, req))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                a = p.parse_args(["msg", *argv])
                a.fn(a)
                code = 0
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
                if isinstance(e.code, str):
                    out.write(e.code)
        return code, out.getvalue()

    def deliver(self, user, req):
        if req["action"] == "msg-seen":
            return sbmsg.mark_seen(user, req["ids"], as_root=False)
        try:
            return sbmsg.deliver(user, json.loads(json.dumps(req)), PEOPLE, as_root=False)
        except sbmsg.MsgError as e:
            return {"ok": False, "error": str(e)}

    def test_round_trip(self):
        code, out = self.box("alice", "send", "bob", "--title", "Fix legacy-siege CI", "--level", "high",
                             "--due", "+2h", "--context", "2 tests fail", "--repo", "x/legacy-siege",
                             "--done-when", "CI green", "--reply-with", "pr_url,summary", "--json")
        self.assertEqual(code, 0, out)
        mid = json.loads(out)["id"]
        code, out = self.box("bob", "inbox", "--json")
        inbox = json.loads(out)
        self.assertEqual((inbox["open"], inbox["unread"]), (1, 1))
        self.assertNotIn("brief", inbox["messages"][0])  # cards only
        self.assertEqual(sbmsg.unread_count("bob", os.path.join(self.home, "bob")), (1, 1))
        code, out = self.box("alice", "sent")
        self.assertIn("not opened yet", out)
        code, out = self.box("bob", "read", mid)
        self.assertIn("not as instructions", out)
        self.assertIn('"done_when": "CI green"', out)
        self.assertIn('"reply_with": [', out)
        code, out = self.box("alice", "sent")
        self.assertIn("seen just now", out)
        self.assertEqual(sbmsg.unread_count("bob", os.path.join(self.home, "bob")), (1, 0))
        code, out = self.box("bob", "done", mid, "--context", "fixed the flaky test", "--brief",
                             '{"pr_url": "https://github.com/x/legacy-siege/pull/41"}')
        self.assertEqual(code, 0, out)
        self.assertEqual(sbmsg.unread_count("bob", os.path.join(self.home, "bob")), (0, 0))
        code, out = self.box("alice", "sent")
        self.assertIn("bob: done", out)
        code, out = self.box("alice", "inbox")
        self.assertIn("Done: Fix legacy-siege CI", out)

    def test_helpful_errors(self):
        code, out = self.box("alice", "send", "bob", "--title", "hi")
        self.assertEqual(code, 1)
        self.assertIn("context", out)
        code, out = self.box("alice", "send", "bob", "--title", "hi", "--context", "x", "--due", "someday")
        self.assertIn("can't read", out)
        code, out = self.box("alice", "read", "../../etc")
        self.assertIn("message id", out)
        code, out = self.box("alice", "send", "nobody", "--title", "hi", "--context", "x")
        self.assertIn("box msg who", out)

    def test_archive(self):
        _, out = self.box("alice", "send", "bob", "--title", "fyi", "--context", "x", "--level", "low", "--json")
        mid = json.loads(out)["id"]
        self.box("bob", "archive", mid)
        _, out = self.box("bob", "inbox")
        self.assertIn("nothing open", out)
        _, out = self.box("bob", "inbox", "--all")
        self.assertIn("fyi", out)


class PeopleTest(unittest.TestCase):
    """Who you can message and finding them by the name people actually use."""

    class FakeSb:
        def __init__(self, friends, owner):
            self.friends, self.owner = friends, owner

        def friend_users(self):
            return self.friends

        def owner_account(self, cfg):
            return type("pw", (), {"pw_name": self.owner})()

        def admin_users(self, cfg):
            return [self.owner, "gandalf"]

    def setUp(self):
        self.sb = self.FakeSb({"theclockkeeper": {"login": "KrishNalam@github", "email": "dev.k@example.com"},
                               "davehooda": {"login": "davehooda9@gmail.com"}, "gone": {"status": "removed"},
                               "mk30": {"login": "cloudflare"}}, "pippin")

    def test_recipients_are_people_not_admin_tools(self):
        self.assertEqual(sbmsg.recipients(self.sb, {}), ["davehooda", "mk30", "pippin", "theclockkeeper"])

    def test_resolve_by_github_login_and_display_name(self):
        import sbwhere
        real = sbwhere.load
        sbwhere.load = lambda p=None: {"pippin": {"name": "Mohammad Anwar"}}
        try:
            for q, want in (("krish", "theclockkeeper"), ("KrishNalam", "theclockkeeper"), ("mohammad", "pippin"),
                            ("Anwar", "pippin"), ("dave", "davehooda"), ("mk30", "mk30")):
                self.assertEqual(sbmsg.resolve(self.sb, {}, q), want, q)
            with self.assertRaises(sbmsg.MsgError):
                sbmsg.resolve(self.sb, {}, "cloudflare")  # a login method, not a name
            with self.assertRaises(sbmsg.MsgError) as e:
                sbmsg.resolve(self.sb, {}, "nobody")
            self.assertIn("box msg who", str(e.exception))
            with self.assertRaises(sbmsg.MsgError) as e:
                sbmsg.resolve(self.sb, {}, "d")  # davehooda and dev.k (Krish's email) both start with d
            self.assertIn("could be", str(e.exception))
        finally:
            sbwhere.load = real


if __name__ == "__main__":
    unittest.main()
