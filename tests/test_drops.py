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


if __name__ == "__main__":
    unittest.main()
