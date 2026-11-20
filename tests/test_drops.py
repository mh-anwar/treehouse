"""Drop dirs are world-writable: an entry that isn't a plain file must not wedge the request handler.
    python3 tests/test_drops.py"""
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
import sbtestenv  # noqa: E402,F401

loader = importlib.machinery.SourceFileLoader("sbadmin", os.path.join(os.path.dirname(HERE), "lib", "sharedbox-admin"))
spec = importlib.util.spec_from_loader("sbadmin", loader)
admin = importlib.util.module_from_spec(spec)
loader.exec_module(admin)


class DropEntries(unittest.TestCase):
    def test_dirs_named_json_are_cleared_files_kept(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "ok.json"), "w").close()
        os.mkdir(os.path.join(d, "empty.json"))
        os.makedirs(os.path.join(d, "full.json", "x"))
        got = admin.drop_entries(d)
        self.assertEqual([os.path.basename(p) for p in got], ["ok.json"])
        self.assertEqual(sorted(os.listdir(d)), ["ok.json"])  # nothing left to re-trigger the path unit


class SharedDropRuns(unittest.TestCase):
    def test_process_shared_drop_survives_owner_boosters(self):
        """It once crashed on every run (dict(**{uid: name})), so `box shared`/`box boost` requests piled up."""
        d = tempfile.mkdtemp()
        old = (admin.sb.SHARED_DROP, admin.sb.SHARED_REPLIES)
        admin.sb.SHARED_DROP, admin.sb.SHARED_REPLIES = os.path.join(d, "drop"), os.path.join(d, "replies")
        real = (admin._secure_dir, admin.shared_uids, admin.sb.owner_account)
        try:
            admin._secure_dir = lambda path, mode: os.makedirs(path, exist_ok=True)
            admin.shared_uids = lambda cfg: {os.getuid() + 1: "friend"}
            admin.sb.owner_account = lambda cfg: type("pw", (), {"pw_uid": os.getuid() + 2, "pw_name": "owner"})()
            os.makedirs(admin.sb.SHARED_DROP)
            with open(os.path.join(admin.sb.SHARED_DROP, "abc.json"), "w") as f:
                f.write("{}")
            admin.process_shared_drop({})  # must not raise
            self.assertEqual(os.listdir(admin.sb.SHARED_DROP), [])  # consumed (refused: not a known uid)
        finally:
            admin.sb.SHARED_DROP, admin.sb.SHARED_REPLIES = old
            admin._secure_dir, admin.shared_uids, admin.sb.owner_account = real

    def test_msg_goes_through_the_drop_with_the_file_owner_as_sender(self):
        """`box msg send`: root reads the drop file's owner, not the "from" in the request."""
        import functools
        import json
        import sbmsg
        d = tempfile.mkdtemp()
        old = (admin.sb.SHARED_DROP, admin.sb.SHARED_REPLIES)
        admin.sb.SHARED_DROP, admin.sb.SHARED_REPLIES = os.path.join(d, "drop"), os.path.join(d, "replies")
        real = (admin._secure_dir, admin.shared_uids, admin.sb.owner_account, admin.sb.write_json, sbmsg.deliver)
        try:
            admin._secure_dir = lambda path, mode: os.makedirs(path, exist_ok=True)
            admin.shared_uids = lambda cfg: {os.getuid(): "alice", os.getuid() + 1: "bob"}
            admin.sb.owner_account = lambda cfg: None
            admin.sb.write_json = lambda path, data, mode=0o644, owner=None: real[3](path, data, mode=mode)
            sbmsg.deliver = functools.partial(real[4], root=os.path.join(d, "mail"), as_root=False)
            os.makedirs(admin.sb.SHARED_DROP)
            with open(os.path.join(admin.sb.SHARED_DROP, "m1.json"), "w") as f:
                json.dump({"action": "msg", "to": "bob", "from": "bob", "title": "Fix CI",
                           "brief": {"context": "tests fail"}}, f)
            admin.process_shared_drop({})
            with open(os.path.join(admin.sb.SHARED_REPLIES, "m1.json")) as f:
                res = json.load(f)
            self.assertTrue(res["ok"], res)
            [m] = sbmsg.list_dir(os.path.join(d, "mail", "bob"))
            self.assertEqual((m["from"], m["title"]), ("alice", "Fix CI"))
        finally:
            admin.sb.SHARED_DROP, admin.sb.SHARED_REPLIES = old
            admin._secure_dir, admin.shared_uids, admin.sb.owner_account, admin.sb.write_json, sbmsg.deliver = real


if __name__ == "__main__":
    unittest.main()
