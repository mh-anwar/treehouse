"""Every action says what it does before it runs, and every job can be finished on its own tab (lib/sharedbox-web,
lib/sharedbox-admin, `box _portal`): inline consequences instead of browser pop-ups, Keep it / Stop it on Home,
descriptions added in place, the Friends tab's Pause/Resume/Remove and invite cancelling, problem reports on/off.
Stdlib only, no root, no network:  python3 -m unittest discover -s tests -v"""

import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
TMP = tempfile.mkdtemp(prefix="sbactions-")
_home, os.environ["HOME"] = os.environ.get("HOME"), TMP


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    argv, sys.argv = sys.argv, [os.path.basename(path)]
    try:
        loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


box = load("box_actions", os.path.join(LIB, "box"))
web = load("sbweb_actions", os.path.join(LIB, "sharedbox-web"))
admin = load("sbadmin_actions", os.path.join(LIB, "sharedbox-admin"))
if _home is not None:
    os.environ["HOME"] = _home

BTN = web.portal_btn("tok", "/_box")


class SaysWhatHappens(unittest.TestCase):
    def test_no_browser_popups_left(self):
        with open(os.path.join(LIB, "sharedbox-web")) as f:
            src = f.read()
        self.assertNotIn("confirm(", src)

    def test_ask_shows_consequence_then_the_real_button(self):
        html = web.ask(BTN, "running-stop", "Stop", "It stops.", "c:db", go="Stop db")
        self.assertIn("<summary class='btn sm danger'>Stop</summary>", html)
        self.assertIn("<p>It stops.</p>", html)
        self.assertIn(">Stop db</button>", html)
        self.assertIn("data-cancel", html)

    def test_database_stop_counts_containers_and_keeps_data(self):
        it = {"key": "c:shop", "kind": "database", "name": "shop-db", "detail": "Supabase · 5 containers",
              "stop": "containers", "mem": 1}
        msg, force = web.stop_text(it)
        self.assertIn("all 5 of its containers", msg)
        self.assertIn("data is kept", msg)
        self.assertFalse(force)

    def test_agent_sessions_say_why_no_stop(self):
        rows = "".join(web.run_rows([{"key": "", "kind": "agent", "name": "Claude session", "stop": None, "mem": 1}],
                                    BTN))
        self.assertIn("close its chat", rows)

    def test_group_server_stop_says_everyone(self):
        st = {"servers": []}
        shared = {"ok": True, "servers": [{"name": "web", "state": "running", "port": 1, "desc": "A shared site"}]}
        page = web.servers_section(st, shared, BTN, "/_box")
        self.assertIn("for everyone in the group", page)
        self.assertIn("View logs", page)
        self.assertNotIn("Show recent output", page)

    def test_switches_show_state_then_action(self):
        st = {"servers": [{"name": "bot", "state": "running", "port": 1, "about": "A bot for the class", "keep": True}]}
        page = web.servers_section(st, {"unavailable": True}, BTN, "/_box")
        self.assertIn("Protected from automatic stops", page)
        self.assertIn(">Turn off</button>", page)
        self.assertNotIn("Allow automatic stops", page)

    def test_description_added_in_place(self):
        st = {"servers": [{"name": "bot", "state": "running", "port": 1, "exec": "/usr/bin/python3 bot.py"}]}
        page = web.servers_section(st, {"unavailable": True}, BTN, "/_box")
        self.assertIn("Add a description", page)
        self.assertIn("value=\"serve-describe\"", page)
        self.assertNotIn("Ask your agent to describe", page)

    def test_hackathon_end_is_empty_and_required(self):
        page = web.hackathon_section(BTN, pause=[{"user": "alice", "name": "site", "about": ""}])
        self.assertIn("name='until' value='' required", page)
        self.assertIn("<b>site</b> (alice)", page)
        self.assertIn("Auto-approve installs from", page)
        self.assertNotIn("skip the safety list", page)


class FinishedHere(unittest.TestCase):
    def test_actions_allowed_on_both_sides(self):
        for act in ("serve-describe", "shared-describe", "running-ok", "project-keep", "issues", "friend-suspend",
                    "friend-resume", "friend-remove", "invite-revoke"):
            self.assertIn(act, web.OWNER_ACTIONS)
            self.assertIn(act, admin.OWNER_PORTAL_ACTIONS)
        self.assertIn("hackathon-preview", admin.OWNER_FRIEND_ACTIONS)

    def test_needs_you_keep_it_and_stop_it(self):
        st = {"running": [{"key": "c:old", "kind": "database", "name": "old-db", "mem": 900 << 20,
                           "age": 4 * 86400, "stop": "containers", "detail": "postgres"}], "running_ok": []}
        items = web.needs_items("/_box", {"ok": False}, st, "nobody-here", btn=BTN)
        html = "".join(a for _, a in items)
        self.assertIn(">Keep it</button>", html)
        self.assertIn(">Stop it</summary>", html)
        st["running_ok"] = ["c:old"]
        self.assertEqual(web.needs_items("/_box", {"ok": False}, st, "nobody-here", btn=BTN), [])

    def test_message_the_owner_link(self):
        link = web.owner_link("/_box", "T3 isn't set up for me")
        self.assertIn("tab=messages", link)
        self.assertIn("title=T3", link)
        pre = web.compose_pre({"to": ["root; x"], "title": ["hi"]})
        self.assertEqual(pre["to"], "")

    def test_box_keeps_running_items_for_a_week(self):
        self.assertTrue(box.portal_running_ok("c:abc")["ok"])
        self.assertIn("c:abc", box.running_ok_keys())
        self.assertFalse(box.portal_running_ok("-rf")["ok"])

    def test_box_keeps_a_project(self):
        d = os.path.join(box.HOME, "proj")
        os.makedirs(os.path.join(d, ".git"), exist_ok=True)
        old = time.time() - 30 * 86400
        os.utime(d, (old, old))
        res = box.portal_project_keep("proj")
        self.assertTrue(res["ok"], res)
        self.assertTrue(os.path.exists(os.path.join(d, ".git", ".treehouse-kept")))
        self.assertFalse(box.portal_project_keep("../etc")["ok"])

    def test_problem_reports_switch(self):
        self.assertIn("off", box.set_issues(False))
        self.assertFalse(box.issues_on())
        self.assertFalse(box.log_issue("something slow", "slow"))
        box.set_issues(True)
        self.assertTrue(box.issues_on())

    def test_admin_refuses_bad_invite_ids(self):
        res = admin.owner_friend_act(sbtestenv_cfg(), "owner", {"action": "invite-revoke", "key": "../x"})
        self.assertFalse(res["ok"])
        res = admin.owner_friend_act(sbtestenv_cfg(), "owner", {"action": "friend-suspend", "key": "root"})
        self.assertFalse(res["ok"])


def sbtestenv_cfg():
    import sbcommon as sb
    return sb.load_config()


class Wording(unittest.TestCase):
    def test_messages(self):
        msg = {"ok": True, "you": "alice", "people": ["bob"], "messages": [
            {"id": "m1", "from": "bob", "title": "Hi", "read": False}],
            "sent": [{"id": "m2", "to": "bob", "title": "Yo", "seen": time.time() - 300}]}
        page = web.inbox_section(msg, {}, "tok", "/_box")
        self.assertIn("Done (tells bob)", page)
        self.assertIn(">Hide</button>", page)
        self.assertIn("read by bob", page)

    def test_events_link_and_attention(self):
        sec = web.EVENT_PLACE["oom_kill"]
        self.assertEqual(sec, "running")
        self.assertIn("over_budget", web.EVENT_ATTENTION)


if __name__ == "__main__":
    unittest.main()
