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


class MemberDropRuns(unittest.TestCase):
    def test_process_member_drop_survives_owner_boosters(self):
        """It once crashed on every run (dict(**{uid: name})), so `box boost` requests piled up."""
        d = tempfile.mkdtemp()
        old = (admin.sb.MEMBER_DROP, admin.sb.MEMBER_REPLIES)
        admin.sb.MEMBER_DROP, admin.sb.MEMBER_REPLIES = os.path.join(d, "drop"), os.path.join(d, "replies")
        real = (admin._secure_dir, admin.member_uids, admin.sb.owner_account)
        try:
            admin._secure_dir = lambda path, mode: os.makedirs(path, exist_ok=True)
            admin.member_uids = lambda cfg: {os.getuid() + 1: "friend"}
            admin.sb.owner_account = lambda cfg: type("pw", (), {"pw_uid": os.getuid() + 2, "pw_name": "owner"})()
            os.makedirs(admin.sb.MEMBER_DROP)
            with open(os.path.join(admin.sb.MEMBER_DROP, "abc.json"), "w") as f:
                f.write("{}")
            admin.process_member_drop({})  # must not raise
            self.assertEqual(os.listdir(admin.sb.MEMBER_DROP), [])  # consumed (refused: not a known uid)
        finally:
            admin.sb.MEMBER_DROP, admin.sb.MEMBER_REPLIES = old
            admin._secure_dir, admin.member_uids, admin.sb.owner_account = real


if __name__ == "__main__":
    unittest.main()
