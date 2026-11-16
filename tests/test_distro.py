"""sbdistro: a machine without apt package lists gets them once before tools are looked up.
    python3 -m unittest discover -s tests -v"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
import sbdistro  # noqa: E402


class AptLists(unittest.TestCase):
    def test_missing_and_present(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(sbdistro.apt_lists_missing(d))
            open(os.path.join(d, "archive.ubuntu.com_ubuntu_dists_noble_main_binary-amd64_Packages"), "w").close()
            self.assertFalse(sbdistro.apt_lists_missing(d))

    def test_update_runs_only_without_lists(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd[:2])
            return mock.Mock(returncode=0, stdout="Package: x\n")
        for missing, want in ((True, 1), (False, 0)):
            calls.clear()
            with mock.patch.object(sbdistro, "apt_lists_missing", return_value=missing), \
                    mock.patch.object(sbdistro.subprocess, "run", side_effect=run):
                self.assertEqual(sbdistro.available(["git"], "debian"), ["git"])
            self.assertEqual(calls.count(["apt-get", "update"]), want)


class ModernNode(unittest.TestCase):
    def policy(self, candidate):
        return mock.Mock(stdout=f"nodejs:\n  Installed: (none)\n  Candidate: {candidate}\n")

    def test_candidate_major(self):
        for cand, want in (("18.19.1+dfsg-6ubuntu5", 18), ("22.22.1+dfsg+~cs22.19.15-1ubuntu1", 22),
                           ("1:20.19.2+dfsg-1", 20), ("(none)", None)):
            with mock.patch.object(sbdistro.subprocess, "run", return_value=self.policy(cand)):
                self.assertEqual(sbdistro.apt_candidate_major("nodejs"), want)

    def test_old_node_adds_nodesource_and_drops_npm(self):
        with mock.patch.object(sbdistro, "apt_candidate_major", return_value=18), \
                mock.patch.object(sbdistro, "ensure_vetted_source") as add:
            self.assertEqual(sbdistro.modern_node(["git", "nodejs", "npm"]), ["git", "nodejs"])
            add.assert_called_once_with("nodejs")

    def test_new_enough_node_is_left_alone(self):
        with mock.patch.object(sbdistro, "apt_candidate_major", return_value=22), \
                mock.patch.object(sbdistro, "ensure_vetted_source") as add:
            self.assertEqual(sbdistro.modern_node(["nodejs", "npm"]), ["nodejs", "npm"])
            add.assert_not_called()
        self.assertEqual(sbdistro.modern_node(["git"]), ["git"])

    def test_nodesource_key_is_pinned(self):
        v = sbdistro.VETTED["nodejs"]
        self.assertTrue(v["key_url"].startswith("https://deb.nodesource.com/"))
        self.assertIn("signed-by=" + v["keyring"], v["repo"])
        self.assertEqual(v["fingerprints"], ["6F71F525282841EEDAF851B42F59B5F99B1BE0B4"])


if __name__ == "__main__":
    unittest.main()
