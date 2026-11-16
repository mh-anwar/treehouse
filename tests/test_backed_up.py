"""The janitor's "fully backed up" check (sbclean.backed_up) and which projects it finds (sbclean.find_projects).
    python3 tests/test_backed_up.py"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import sbclean  # noqa: E402

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@example.com", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True)


class BackedUp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sbbackup-")
        self.remote = os.path.join(self.tmp, "remote.git")
        git(self.tmp, "init", "-q", "--bare", "-b", "main", self.remote)
        self.repo = os.path.join(self.tmp, "home", "app")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("a\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-q", "-m", "a")
        git(self.repo, "remote", "add", "origin", self.remote)
        git(self.repo, "push", "-q", "origin", "main")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ignore(self, *pats):
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write("\n".join(pats) + "\n")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-q", "-m", "ignore")
        git(self.repo, "push", "-q", "origin", "main")

    def test_ignored_secret_not_backed_up(self):
        self._ignore(".env", "node_modules/")
        open(os.path.join(self.repo, ".env"), "w").close()
        self.assertFalse(sbclean.backed_up(self.repo))

    def test_ignored_build_output_is_fine(self):
        self._ignore("node_modules/", "dist/", "*.log")
        for d in ("node_modules/x", "dist"):
            os.makedirs(os.path.join(self.repo, d))
            open(os.path.join(self.repo, d, "f.js"), "w").close()
        open(os.path.join(self.repo, "debug.log"), "w").close()
        self.assertTrue(sbclean.backed_up(self.repo))

    def test_detached_head_commit(self):
        git(self.repo, "checkout", "-q", "--detach")
        with open(os.path.join(self.repo, "a.txt"), "a") as f:
            f.write("loose\n")
        git(self.repo, "commit", "-q", "-am", "loose")
        self.assertFalse(sbclean.backed_up(self.repo))

    def test_clean_and_pushed(self):
        self.assertTrue(sbclean.backed_up(self.repo))

    def test_dirty(self):
        with open(os.path.join(self.repo, "a.txt"), "a") as f:
            f.write("more\n")
        self.assertFalse(sbclean.backed_up(self.repo))

    def test_untracked(self):
        open(os.path.join(self.repo, "new.txt"), "w").close()
        self.assertFalse(sbclean.backed_up(self.repo))

    def test_unpushed_commit(self):
        with open(os.path.join(self.repo, "a.txt"), "a") as f:
            f.write("more\n")
        git(self.repo, "commit", "-q", "-am", "b")
        self.assertFalse(sbclean.backed_up(self.repo))

    def test_unpushed_branch(self):
        git(self.repo, "checkout", "-q", "-b", "side")
        with open(os.path.join(self.repo, "b.txt"), "w") as f:
            f.write("b\n")
        git(self.repo, "add", "b.txt")
        git(self.repo, "commit", "-q", "-m", "b")
        git(self.repo, "checkout", "-q", "main")
        self.assertFalse(sbclean.backed_up(self.repo))

    def test_stash(self):
        with open(os.path.join(self.repo, "a.txt"), "a") as f:
            f.write("more\n")
        git(self.repo, "stash", "-q")
        self.assertFalse(sbclean.backed_up(self.repo))

    def test_no_remote(self):
        git(self.repo, "remote", "remove", "origin")
        self.assertFalse(sbclean.backed_up(self.repo))

    def test_not_git(self):
        d = os.path.join(self.tmp, "home", "notes")
        os.makedirs(d)
        self.assertFalse(sbclean.backed_up(d))

    def test_find_projects(self):
        home = os.path.join(self.tmp, "home")
        for d in ("notes", ".hidden", "keep", "services"):
            os.makedirs(os.path.join(home, d), exist_ok=True)
        for d in ("keep", "services", ".hidden"):
            git(os.path.join(home, d), "init", "-q")
        legacy = os.path.join(home, "projects", "old")
        os.makedirs(legacy)
        git(legacy, "init", "-q")
        found = sbclean.find_projects(home)
        self.assertEqual([n for n, _ in found], ["app", "old"])


if __name__ == "__main__":
    unittest.main()
