"""sbtelegram: linking with a one-time code, commands, buttons, replies and pushing new messages (no network)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import sbmsg  # noqa: E402
import sbtelegram as tg  # noqa: E402


class FakeSb:
    STATE_DIR = ""
    events = []

    @staticmethod
    def log_event(kind, user=None, **kw):
        FakeSb.events.append((kind, user))

    @staticmethod
    def friend_users():
        return {"alice": {"status": "active", "login": "alice@x.io"}, "bob": {"status": "active"}}


class Api:
    def __init__(self):
        self.calls = []

    def __call__(self, method, params):
        self.calls.append((method, params))
        return {}

    def texts(self, chat=None):
        return [p["text"] for m, p in self.calls if m == "sendMessage" and (chat is None or p["chat_id"] == chat)]


class BotTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.mail = os.path.join(self.d, "mail")
        self.where = os.path.join(self.d, "where.json")
        self.store = tg.Store(os.path.join(self.d, "tg"))
        self.api = Api()
        self.t = 1_800_000_000
        self.delivered = []

        def deliver(user, req):
            self.delivered.append((user, req))
            return sbmsg.deliver(user, req, {"alice", "bob"}, now=self.t, root=self.mail, as_root=False)
        self.bot = tg.Bot(FakeSb, {}, self.store, self.api, lambda: ["alice", "bob"], deliver,
                          mail_root=self.mail, now=lambda: self.t, as_root=False, where_path=self.where)
        orig = tg.sbwhere.load
        self.addCleanup(setattr, tg.sbwhere, "load", orig)
        tg.sbwhere.load = lambda p=None: orig(p or self.where)

    def text(self, chat, text, username="al"):
        self.bot.handle({"message": {"chat": {"id": chat, "type": "private"}, "from": {"username": username},
                                     "text": text}})

    def link(self, user="alice", chat=11):
        code = tg.new_code(self.store, user, now=self.t)
        self.text(chat, f"/start {code}")
        return code

    def send(self, frm, to, title, level="normal"):
        return sbmsg.deliver(frm, {"to": to, "title": title, "level": level, "brief": {"context": "ctx here"}},
                             {"alice", "bob"}, now=self.t, root=self.mail, as_root=False)

    def test_link_with_code_once(self):
        code = self.link()
        self.assertEqual(tg.status(self.store, "alice")["linked"], True)
        self.assertEqual(tg.status(self.store, "alice")["handle"], "@al")
        self.assertIn("Connected to", self.api.texts(11)[-1])
        self.text(12, f"/start {code}")  # a second chat can't reuse it
        self.assertIn("expired or was already used", self.api.texts(12)[-1])
        self.assertIsNone(tg.user_for_chat(self.store, 12))

    def test_expired_code(self):
        code = tg.new_code(self.store, "alice", now=self.t - tg.CODE_TTL - 1)
        self.text(11, f"/start {code}")
        self.assertFalse(tg.status(self.store, "alice")["linked"])

    def test_unlinked_chat_only_gets_how_to_connect(self):
        self.text(99, "/where bob")
        self.assertIn("isn't connected", self.api.texts(99)[-1])

    def test_groups_are_ignored(self):
        self.bot.handle({"message": {"chat": {"id": -5, "type": "group"}, "from": {}, "text": "/start x"}})
        self.assertEqual(self.api.calls, [])

    def test_away_where_back(self):
        self.link()
        self.text(11, "/away in class till 3pm")
        self.assertIn("In class".lower(), self.api.texts(11)[-1].lower())
        self.text(11, "/where alice")
        self.assertIn("until", self.api.texts(11)[-1])
        self.text(11, "/back")
        self.text(11, "/where al")
        self.assertIn("no status set", self.api.texts(11)[-1])

    def test_split_away(self):
        self.assertEqual(tg.split_away("asleep back 9am"), ("asleep", "9am"))
        self.assertEqual(tg.split_away("on the back porch"), ("on the back porch", None))

    def test_push_new_only_after_link_and_respects_quiet(self):
        self.send("bob", "alice", "Before linking")
        self.t += 10
        self.link()
        self.api.calls.clear()
        self.t += 10
        self.send("bob", "alice", "Low one", level="low")
        self.text(11, "/loud")
        self.api.calls.clear()
        self.bot.push_new()
        self.assertEqual(len(self.api.texts(11)), 1)
        self.assertIn("Low one", self.api.texts(11)[0])
        self.bot.push_new()  # nothing twice
        self.assertEqual(len(self.api.texts(11)), 1)
        self.text(11, "/quiet")
        self.t += 10
        self.send("bob", "alice", "Another low", level="low")
        self.send("bob", "alice", "Urgent one", level="critical")
        sbmsg.deliver("bob", {"to": "alice", "title": "Due soon", "due": self.t + 3600, "brief": {"context": "c"}},
                      {"alice", "bob"}, now=self.t, root=self.mail, as_root=False)
        self.api.calls.clear()
        self.bot.push_new()
        self.assertEqual(sorted(t.split("<b>")[-1].split("<")[0] for t in self.api.texts(11)),
                         ["Due soon", "Urgent one"])

    def test_buttons_done_and_reply(self):
        self.link()
        mid = self.send("bob", "alice", "Fix CI")["id"]
        self.bot.handle({"callback_query": {"id": "q1", "data": f"done:{mid}", "message": {"chat": {"id": 11}}}})
        self.assertEqual(self.delivered[-1], ("alice", {"action": "msg", "reply_to": mid, "status": "done"}))
        self.bot.handle({"callback_query": {"id": "q2", "data": f"reply:{mid}", "message": {"chat": {"id": 11}}}})
        self.text(11, "on it after lunch")
        user, req = self.delivered[-1]
        self.assertEqual((user, req["reply_to"], req["title"]), ("alice", mid, "on it after lunch"))
        bob_inbox = sbmsg.list_dir(os.path.join(self.mail, "bob"))
        self.assertEqual(sorted(m.get("status") or m["title"] for m in bob_inbox), ["done", "on it after lunch"])

    def test_button_on_someone_elses_message_refused(self):
        self.link()
        mid = self.send("alice", "bob", "Not yours")["id"]
        self.bot.handle({"callback_query": {"id": "q", "data": f"done:{mid}", "message": {"chat": {"id": 11}}}})
        self.assertEqual(self.delivered, [])

    def test_card_escapes_html(self):
        m = {"id": "abc", "from": "bob", "title": "<b>x</b>", "level": "high", "brief": {"context": "a < b"}}
        t = tg.card_text(m, self.t, people={})
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", t)
        self.assertIn("a &lt; b", t)

    def test_stop_unlinks(self):
        self.link()
        self.text(11, "/stop")
        self.assertFalse(tg.status(self.store, "alice")["linked"])

    def test_read_token(self):
        p = os.path.join(self.d, "tok")
        with open(p, "w") as f:
            f.write("123456789:" + "A" * 35 + "\n")
        self.assertTrue(tg.read_token(p))
        with open(p, "w") as f:
            f.write("nope")
        self.assertIsNone(tg.read_token(p))


if __name__ == "__main__":
    unittest.main()
