"""Everything on the dashboard can be done by the person's agent: each button the web page posts has a `box`
command, root takes the new ones from the drop files with the file's owner as the identity, and `box help settings`
names only commands that exist. Stdlib only, no root, no network.
    python3 -m unittest tests.test_settings_cli -v"""

import argparse
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, LIB)
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
TMP = tempfile.mkdtemp(prefix="sbsettings-")
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


box = load("box_settings", os.path.join(LIB, "box"))
admin = load("sbadmin_settings", os.path.join(LIB, "sharedbox-admin"))
if _home is not None:
    os.environ["HOME"] = _home

# every action the dashboard posts (lib/sharedbox-web's do_POST) -> the `box` command that does the same.
# A new button without an entry here fails the test below: give it a command (and a line in `box help settings`).
DASHBOARD = {
    "invite": "invite", "hackathon": "hackathon", "boost": "boost", "self-update": "friends",
    "request-approve": "friends", "request-done": "friends", "request-decline": "friends",
    "running-stop": "running", "serve-ctl": "serve", "serve-logs": "serve", "shared-ctl": "shared",
    "shared-logs": "shared", "msg": "msg", "where": "where", "telegram": "telegram", "telegram-setup": "telegram",
    "projects": "projects", "peer-login": "login", "peer-check": "login", "team": "teammates", "restore": "restore",
    "add-key": "ssh-key", "login": "login", "code": "login", "check": "login", "t3": "t3", "t3app": "t3",
    "t3prompt": "t3", "peer-code": "login", "logout": "login", "peer-logout": "login",
    "running-stop-any": "running", "serve-set": "serve",
}


def subcommands():
    """The `box` subcommands, from its own parser (main() builds it; stop it before it parses)."""
    seen = {}
    real = argparse.ArgumentParser.parse_args

    def grab(self, *a, **k):
        for act in self._actions:
            if isinstance(act, argparse._SubParsersAction):
                seen.update(act.choices)
        raise SystemExit(0)
    argparse.ArgumentParser.parse_args = grab
    argv, sys.argv = sys.argv, ["box", "status"]
    try:
        box.main()
    except SystemExit:
        pass
    finally:
        argparse.ArgumentParser.parse_args = real
        sys.argv = argv
    return seen


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class EveryButtonHasACommand(unittest.TestCase):
    def test_web_actions_are_all_mapped(self):
        with open(os.path.join(LIB, "sharedbox-web")) as f:
            src = f.read()
        post = src[src.index("    def do_POST(self):"):src.index("# ---------- public front door pages")]
        found = set(re.findall(r'action == "([a-z0-9-]+)"', post))
        for group in re.findall(r'action in \(([^)]*)\)', post) + re.findall(r'actions = \(([^)]*)\)', post):
            found |= set(re.findall(r'"([a-z0-9-]+)"', group))
        found |= {"request-approve", "request-done", "request-decline"}  # action.startswith("request-")
        missing = sorted(found - set(DASHBOARD))
        self.assertEqual(missing, [], f"dashboard actions with no `box` command mapped: {missing}")

    def test_mapped_commands_exist(self):
        cmds = subcommands()
        self.assertEqual(sorted({c for c in DASHBOARD.values() if c not in cmds}), [])

    def test_help_settings_names_real_commands(self):
        topics = box.help_topics()
        self.assertIn("settings", topics)
        cmds = subcommands()
        named = set(re.findall(r"`box ([a-z][a-z0-9-]*)", topics["settings"] + topics["logins"]))
        self.assertTrue({"telegram", "login", "teammates"} <= named, named)
        self.assertEqual(sorted(named - set(cmds)), [])


def run(fn, **kw):
    out = io.StringIO()
    code = 0
    with redirect_stdout(out):
        try:
            fn(argparse.Namespace(**kw))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return code, out.getvalue()


class TelegramCommand(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.real = (box.shared_request, box.admin_request, box.sb.admin_users, box.read_secret)
        box.shared_request = lambda req, wait=60: self.sent.append(("shared", req)) or self.reply
        box.admin_request = lambda req, wait: self.sent.append(("admin", req)) or self.reply
        box.read_secret = lambda prompt: "123456789:" + "A" * 35 + "\n"

    def tearDown(self):
        box.shared_request, box.admin_request, box.sb.admin_users, box.read_secret = self.real

    def test_link_prints_the_one_time_url(self):
        self.reply = {"ok": True, "url": "https://t.me/treebot?start=abc", "bot": "treebot"}
        code, out = run(box.cmd_telegram, op="link", level=None, json=False)
        self.assertEqual(code, 0)
        self.assertIn("https://t.me/treebot?start=abc", out)
        self.assertEqual(self.sent, [("shared", {"action": "telegram", "op": "link"})])

    def test_notify_checks_the_level(self):
        self.reply = {"ok": True, "message": "Saved."}
        self.assertNotEqual(run(box.cmd_telegram, op="notify", level="loud", json=False)[0], 0)
        self.assertEqual(self.sent, [])
        run(box.cmd_telegram, op="notify", level="important", json=False)
        self.assertEqual(self.sent[-1][1], {"action": "telegram", "op": "notify", "level": "important"})

    def test_setup_is_owner_only_and_reads_the_token_from_stdin(self):
        self.reply = {"ok": True, "message": "Telegram is on: @treebot."}
        box.sb.admin_users = lambda cfg: set()
        self.assertNotEqual(run(box.cmd_telegram, op="setup", level=None, json=False)[0], 0)
        self.assertEqual(self.sent, [])
        box.sb.admin_users = lambda cfg: {box.ME.pw_name}
        code, out = run(box.cmd_telegram, op="setup", level=None, json=True)
        self.assertEqual(code, 0)
        [(where, req)] = self.sent
        self.assertEqual((where, req["action"], req["token"]), ("admin", "telegram-setup", "123456789:" + "A" * 35))
        self.assertTrue(json.loads(out)["ok"])

    def test_failure_exits_nonzero_with_json(self):
        self.reply = {"ok": False, "error": "Telegram isn't set up on this box yet."}
        code, out = run(box.cmd_telegram, op="test", level=None, json=True)
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])


class LoginCommand(unittest.TestCase):
    def setUp(self):
        self.real = (box.portal_login_start, box.portal_login_code, box.portal_login_wait, box.read_secret)
        self.calls = []
        box.portal_login_start = lambda key, console=False: self.calls.append(("start", key, console)) or {
            "ok": True, "url": "https://claude.ai/oauth?x", "code": None, "paste": True}
        box.portal_login_code = lambda key, code: self.calls.append(("code", key, code)) or {"ok": True, "done": True}
        box.portal_login_wait = lambda key, s: self.calls.append(("check", key)) or {"ok": False, "error": "not yet"}
        box.read_secret = lambda prompt: "abc123#xyz\n"

    def tearDown(self):
        box.portal_login_start, box.portal_login_code, box.portal_login_wait, box.read_secret = self.real

    def ns(self, **kw):
        return dict(dict(key="claude", code=False, check=False, console=False, other_boxes=False, json=False), **kw)

    def test_start_then_code(self):
        code, out = run(box.cmd_login, **self.ns())
        self.assertEqual(code, 0)
        self.assertIn("https://claude.ai/oauth?x", out)
        self.assertIn("--code", out)
        code, out = run(box.cmd_login, **self.ns(code=True))
        self.assertEqual((code, self.calls[-1]), (0, ("code", "claude", "abc123#xyz\n")))
        self.assertIn("logged in", out)

    def test_check_not_done_fails(self):
        self.assertEqual(run(box.cmd_login, **self.ns(key="codex", check=True))[0], 1)

    def test_other_boxes_only_for_per_box_logins(self):
        self.assertNotEqual(run(box.cmd_login, **self.ns(key="claude", other_boxes=True))[0], 0)


class DeviceCodes(unittest.TestCase):
    def test_t3_connect_screen(self):
        """T3 Connect prints "Code: ABCD-EFGH": a lowercase-only match left the T3 app's Connect button failing."""
        screen = ("Sign in to T3 Connect. Open on any device:\n  https://accounts.t3.codes/device?user_code=CHXQ-RZKH\n"
                  "Code: CHXQ-RZKH\nWaiting for approval. Expires in 10 min. Ctrl+C cancels.\n")
        self.assertEqual(box.DEVICE_CODE_RE.findall(screen), ["CHXQ-RZKH"])
        self.assertEqual(box.DEVICE_CODE_RE.findall("enter code: AB12-CD34\n"), ["AB12-CD34"])
        self.assertEqual(box.DEVICE_CODE_RE.findall("   WXYZ-1234\n"), ["WXYZ-1234"])


class RootTakesTheNewRequests(unittest.TestCase):
    """The drop file's owner is who asks; a request can't name someone else."""

    def drop(self, req, uid):
        d = tempfile.mkdtemp(dir=TMP)
        old = (admin.sb.SHARED_DROP, admin.sb.SHARED_REPLIES)
        admin.sb.SHARED_DROP, admin.sb.SHARED_REPLIES = os.path.join(d, "drop"), os.path.join(d, "replies")
        real = (admin._secure_dir, admin.shared_uids, admin.sb.owner_account, admin.sb.write_json, admin.os.lstat)
        try:
            admin._secure_dir = lambda path, mode: os.makedirs(path, exist_ok=True)
            admin.shared_uids = lambda cfg: {1001: "alice"}
            admin.sb.owner_account = lambda cfg: type("pw", (), {"pw_uid": 1000, "pw_name": "owner"})()
            admin.sb.write_json = lambda path, data, mode=0o644, owner=None: real[3](path, data, mode=mode)
            st = os.lstat(TMP)

            class St:
                st_uid, st_mtime, st_mode, st_size = uid, st.st_mtime, 0o100600, 10
            admin.os.lstat = lambda p: St() if p.endswith(".json") and "/drop/" in p else real[4](p)
            os.makedirs(admin.sb.SHARED_DROP)
            with open(os.path.join(admin.sb.SHARED_DROP, "r1.json"), "w") as f:
                json.dump(req, f)
            admin.process_shared_drop({})
            with open(os.path.join(admin.sb.SHARED_REPLIES, "r1.json")) as f:
                return json.load(f)
        finally:
            admin.sb.SHARED_DROP, admin.sb.SHARED_REPLIES = old
            admin._secure_dir, admin.shared_uids, admin.sb.owner_account, admin.sb.write_json, admin.os.lstat = real

    def setUp(self):
        self.real = (admin.handle_telegram, admin.handle_team, admin.read_untrusted, admin.pwd.getpwnam)
        self.seen = []
        admin.handle_telegram = lambda cfg, name, req: self.seen.append(("tg", name, req.get("op"))) or {"ok": True}
        admin.handle_team = lambda name, pw, req: self.seen.append(("team", name, req.get("op"))) or {"ok": True}
        admin.pwd.getpwnam = lambda n: type("pw", (), {"pw_name": n})()

        def read(path, owner, limit=0):  # the real one also checks the file's owner, faked here by lstat
            with open(path) as f:
                return f.read()
        admin.read_untrusted = read

    def tearDown(self):
        admin.handle_telegram, admin.handle_team, admin.read_untrusted, admin.pwd.getpwnam = self.real

    def test_telegram_as_the_file_owner(self):
        self.assertTrue(self.drop({"action": "telegram", "op": "link", "user": "bob"}, 1001)["ok"])
        self.assertTrue(self.drop({"action": "telegram", "op": "status"}, 1000)["ok"])  # the owner too
        self.assertEqual(self.seen, [("tg", "alice", "link"), ("tg", "owner", "status")])

    def test_unknown_telegram_op_refused(self):
        self.assertFalse(self.drop({"action": "telegram", "op": "setup", "token": "x"}, 1001)["ok"])
        self.assertEqual(self.seen, [])

    def test_teammates_as_the_file_owner(self):
        real = admin.sb.friend_users
        try:
            admin.sb.friend_users = lambda: {"alice": {"uid": 1001}}
            self.assertTrue(self.drop({"action": "t3-team", "op": "grant", "to": "bob"}, 1001)["ok"])
            self.assertEqual(self.seen, [("team", "alice", "grant")])
            admin.sb.friend_users = lambda: {}  # an admin without a friend account: no managed T3, a clear answer
            res = self.drop({"action": "t3-team", "op": "list"}, 1001)
            self.assertFalse(res["ok"])
            self.assertIn("friend accounts", res["error"])
        finally:
            admin.sb.friend_users = real


class OwnerTelegramSetup(unittest.TestCase):
    def test_setup_and_off_through_friends_action(self):
        real = (admin.telegram_setup, admin.telegram_off)
        seen = []
        try:
            admin.telegram_setup = lambda cfg, token, by="root": seen.append(("setup", token, by)) or {"ok": True}
            admin.telegram_off = lambda cfg, by="root": seen.append(("off", by))
            self.assertTrue(admin.friends_action({}, "pippin", {"action": "telegram-setup", "token": "t"})["ok"])
            self.assertTrue(admin.friends_action({}, "pippin", {"action": "telegram-setup", "op": "off"})["ok"])
        finally:
            admin.telegram_setup, admin.telegram_off = real
        self.assertEqual(seen, [("setup", "t", "pippin"), ("off", "pippin")])
        self.assertIn("telegram-setup", admin.FRIENDS_ACTIONS)


if __name__ == "__main__":
    unittest.main()
