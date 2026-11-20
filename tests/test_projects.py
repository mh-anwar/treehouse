"""Tests for lib/sbprojects.py (projects on all my boxes). Real git against a local bare "GitHub" repo, and a fake
second box: its own home folder, reached through a `remote` function that runs the receiving side directly instead
of over SSH. No network, no SSH, no root:
    python3 -m unittest tests.test_projects -v"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
import sbprojects as p  # noqa: E402

NOW = 1_800_000_000
GIT_ID = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
          "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def sh(*args, cwd=None):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, **GIT_ID})
    if r.returncode:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout


def git(args, cwd=None, timeout=60):
    """The module's runner, with a throwaway identity and no global config (allows local file:// clones)."""
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL, env={**os.environ, **p.GIT_ENV, **GIT_ID})
    return r.returncode, r.stdout, r.stderr


def put(path, text):
    with open(path, "w") as f:
        f.write(text)


def get(path):
    with open(path) as f:
        return f.read()


ANY = lambda url: bool(url)  # noqa: E731  (tests clone from a local bare repo, which real syncs refuse)


@unittest.skipUnless(shutil.which("git"), "needs git")
class Boxes(unittest.TestCase):
    """Box A (this one) chooses projects; box B is the fake second box."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sbprojects-")
        self.a, self.b, self.hub = (os.path.join(self.tmp, x) for x in ("a", "b", "hub"))
        for d in (self.a, self.b, self.hub):
            os.makedirs(d)
        self.origin = os.path.join(self.hub, "app.git")
        sh("init", "-q", "--bare", "-b", "main", self.origin)
        self.work = os.path.join(self.a, "app")
        sh("clone", "-q", self.origin, self.work)
        self.commit(self.work, "one")
        sh("push", "-q", "origin", "HEAD:main", cwd=self.work)
        self.calls = []
        self.t3 = []

    def tearDown(self):
        shutil.rmtree(self.tmp, True)

    def commit(self, repo, text):
        with open(os.path.join(repo, "f.txt"), "a") as f:
            f.write(text + "\n")
        sh("add", "f.txt", cwd=repo)
        sh("commit", "-q", "-m", text, cwd=repo)

    def t3_add(self, path, name):
        self.t3.append((path, name))
        return True, ""

    def fake_b(self, dest, req):
        """What SSH + `box _projects-here` on box B would answer (JSON round trip, like the wire)."""
        self.calls.append(dest)
        req = json.loads(json.dumps(req))
        return json.loads(json.dumps(p.apply_all(self.b, req, git, self.t3_add, allow=ANY)))

    def sync(self, force=True):
        # box A lists with the real cloneable() check: give the repo a GitHub-looking URL in the list, and let the
        # fake box clone from the local bare repo it really points at
        return p.sync(self.a, {"minastirith": "minastirith"}, self.remote_local, git, now=NOW, force=force)

    def remote_local(self, dest, req):
        for x in req["projects"]:
            x["url"] = self.origin
        return self.fake_b(dest, req)

    def use_github_url(self):
        sh("remote", "set-url", "origin", "https://github.com/alice/app.git", cwd=self.work)
        # the bare repo stays the real origin for fetches on box A's side of the test
        sh("config", "url." + self.origin + ".insteadOf", "https://github.com/alice/app.git", cwd=self.work)

    # ---- listing and choosing (box A) ----

    def test_lists_repos_with_and_without_a_usable_remote(self):
        os.makedirs(os.path.join(self.a, "local-only"))
        sh("init", "-q", os.path.join(self.a, "local-only"))
        os.makedirs(os.path.join(self.a, "not-a-repo"))
        os.makedirs(os.path.join(self.a, ".hidden", ".git"))
        self.use_github_url()
        rows = {r["name"]: r for r in p.list_repos(self.a, git)}
        self.assertEqual(set(rows), {"app", "local-only"})
        self.assertTrue(rows["app"]["available"])
        self.assertEqual(rows["app"]["url"], "https://github.com/alice/app.git")
        self.assertFalse(rows["local-only"]["available"])
        self.assertIn("Push it to GitHub first", rows["local-only"]["why"])

    def test_a_local_path_or_a_token_in_the_url_is_not_copied(self):
        self.assertFalse(p.cloneable("/home/alice/app"))
        self.assertFalse(p.cloneable("file:///home/alice/app"))
        self.assertFalse(p.cloneable("https://x-access-token:ghp_abc@github.com/alice/app.git"))
        self.assertFalse(p.cloneable("--upload-pack=touch /tmp/x"))
        self.assertTrue(p.cloneable("git@github.com:alice/app.git"))
        self.assertTrue(p.cloneable("ssh://git@github.com/alice/app.git"))
        rows = p.list_repos(self.a, git)  # still the local bare path: not available, and its path isn't echoed
        self.assertEqual(rows[0]["url"], "")

    def test_choose_needs_an_available_repo_and_off_always_works(self):
        ok, msg = p.set_choice(self.a, "app", True, repos=p.list_repos(self.a, git))
        self.assertFalse(ok)
        self.assertIn("Push it to GitHub first", msg)
        self.use_github_url()
        ok, _ = p.set_choice(self.a, "app", True, repos=p.list_repos(self.a, git))
        self.assertTrue(ok)
        self.assertEqual(p.load_choice(self.a), ["app"])
        self.assertFalse(p.set_choice(self.a, "../etc", True)[0])
        self.assertTrue(p.set_choice(self.a, "app", False)[0])
        self.assertEqual(p.load_choice(self.a), [])

    def test_same_remote_in_every_spelling(self):
        same = {p.norm_url(u) for u in ("https://github.com/alice/app.git", "https://GitHub.com/alice/app/",
                                        "git@github.com:alice/app.git", "ssh://git@github.com/alice/app",
                                        "ssh://git@github.com:22/alice/app.git")}
        self.assertEqual(same, {"github.com/alice/app"})
        self.assertNotEqual(p.norm_url("https://github.com/alice/app"), p.norm_url("https://github.com/bob/app"))

    # ---- the round trip to the fake second box ----

    def choose(self):
        self.use_github_url()
        self.assertTrue(p.set_choice(self.a, "app", True, repos=p.list_repos(self.a, git))[0])

    def state(self, name="app"):
        s = p.summary(self.a, ["minastirith"], git)
        return next(r for r in s["projects"] if r["name"] == name)["boxes"]["minastirith"]

    def test_nothing_chosen_does_nothing(self):
        out = self.sync()
        self.assertFalse(out["ran"])
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.b, "app")))

    def test_first_sync_clones_blobless_into_the_same_folder_and_adds_it_to_t3(self):
        self.choose()
        out = self.sync()
        self.assertTrue(out["ran"])
        copy = os.path.join(self.b, "app")
        self.assertEqual(sh("log", "--format=%s", cwd=copy).split(), ["one"])
        self.assertEqual(sh("config", "--get", "remote.origin.partialclonefilter", cwd=copy).strip(), "blob:none")
        self.assertEqual(self.t3, [(copy, "app")])
        st = self.state()
        self.assertEqual((st["state"], st["t3"]), ("cloned", True))
        self.assertEqual(p.summary(self.a, ["minastirith"], git)["boxes"]["minastirith"]["last_sync"], NOW)

    def test_later_syncs_fast_forward_a_clean_copy_and_add_to_t3_only_once(self):
        self.choose()
        self.sync()
        self.commit(self.work, "two")
        sh("push", "-q", "origin", "HEAD:main", cwd=self.work)
        self.sync()
        self.assertEqual(sh("log", "-1", "--format=%s", cwd=os.path.join(self.b, "app")).strip(), "two")
        self.assertEqual(self.state()["state"], "updated")
        self.sync()
        self.assertEqual(self.state()["state"], "current")
        self.assertEqual(len(self.t3), 1)

    def test_unpushed_work_on_this_box_never_travels(self):
        self.choose()
        self.sync()
        self.commit(self.work, "local only")  # committed on box A, not pushed
        self.sync()
        self.assertEqual(sh("log", "-1", "--format=%s", cwd=os.path.join(self.b, "app")).strip(), "one")

    def test_a_dirty_copy_is_left_alone(self):
        self.choose()
        self.sync()
        copy = os.path.join(self.b, "app")
        with open(os.path.join(copy, "f.txt"), "a") as f:
            f.write("mine\n")
        self.commit(self.work, "two")
        sh("push", "-q", "origin", "HEAD:main", cwd=self.work)
        self.sync()
        st = self.state()
        self.assertEqual(st["state"], "skipped")
        self.assertIn("uncommitted", st["note"])
        self.assertIn("mine", get(os.path.join(copy, "f.txt")))
        self.assertEqual(sh("log", "-1", "--format=%s", cwd=copy).strip(), "one")

    def test_an_untracked_file_counts_as_local_work(self):
        self.choose()
        self.sync()
        put(os.path.join(self.b, "app", "new.txt"), "draft")
        self.sync()
        self.assertEqual(self.state()["state"], "skipped")
        self.assertTrue(os.path.exists(os.path.join(self.b, "app", "new.txt")))

    def test_unpushed_commits_on_the_copy_are_kept(self):
        self.choose()
        self.sync()
        copy = os.path.join(self.b, "app")
        self.commit(copy, "theirs")
        self.commit(self.work, "two")
        sh("push", "-q", "origin", "HEAD:main", cwd=self.work)
        self.sync()
        st = self.state()
        self.assertEqual(st["state"], "skipped")
        self.assertIn("of its own", st["note"])
        self.assertEqual(sh("log", "-1", "--format=%s", cwd=copy).strip(), "theirs")

    def test_a_different_repo_or_a_plain_folder_at_that_path_is_left_alone(self):
        self.choose()
        other = os.path.join(self.b, "app")
        sh("init", "-q", other)
        sh("remote", "add", "origin", "https://github.com/someone/else.git", cwd=other)
        self.commit(other, "different")
        self.sync()
        self.assertIn("different repo", self.state()["note"])
        self.assertEqual(sh("log", "-1", "--format=%s", cwd=other).strip(), "different")
        self.assertEqual(self.t3, [])
        shutil.rmtree(other)
        os.makedirs(other)
        put(os.path.join(other, "keep.txt"), "x")
        self.sync()
        self.assertIn("isn't a git repo", self.state()["note"])
        self.assertEqual(os.listdir(other), ["keep.txt"])

    def test_turning_off_stops_syncing_and_keeps_the_copy(self):
        self.choose()
        self.sync()
        p.set_choice(self.a, "app", False)
        self.calls.clear()
        out = self.sync()
        self.assertFalse(out["ran"])
        self.assertEqual(self.calls, [])
        self.assertTrue(os.path.isdir(os.path.join(self.b, "app", ".git")))
        self.assertFalse(next(r for r in p.summary(self.a, ["minastirith"], git)["projects"])["everywhere"])

    def test_sync_runs_when_due_or_right_after_the_choice_changed(self):
        self.choose()
        self.sync(force=False)
        self.assertEqual(len(self.calls), 1)
        os.utime(p.choice_path(self.a), (NOW - 100, NOW - 100))  # choice older than the last sync
        p.sync(self.a, {"minastirith": "m"}, self.remote_local, git, now=NOW + 60)
        self.assertEqual(len(self.calls), 1)  # not due yet
        p.sync(self.a, {"minastirith": "m"}, self.remote_local, git, now=NOW + p.SYNC_EVERY)
        self.assertEqual(len(self.calls), 2)

    def test_an_unreachable_box_is_reported_and_keeps_its_last_results(self):
        self.choose()
        self.sync()
        p.sync(self.a, {"minastirith": "m"}, lambda d, r: {"ok": False, "error": "Can't log in to that box"}, git,
               now=NOW + 1, force=True)
        s = p.summary(self.a, ["minastirith"], git)
        self.assertEqual(s["boxes"]["minastirith"]["error"], "Can't log in to that box")
        self.assertEqual(s["projects"][0]["boxes"]["minastirith"]["state"], "cloned")

    def test_a_failed_clone_is_an_error_and_leaves_nothing_behind(self):
        res = p.apply_all(self.b, {"projects": [{"name": "app", "url": os.path.join(self.hub, "nope.git")}]}, git,
                          self.t3_add, allow=ANY)
        self.assertEqual(res["results"][0]["state"], "error")
        self.assertFalse(os.path.exists(os.path.join(self.b, "app")))
        self.assertEqual(self.t3, [])

    def test_the_receiving_side_refuses_odd_names_and_urls(self):
        res = p.apply_all(self.b, {"projects": [{"name": "../x", "url": "https://github.com/a/b"},
                                                {"name": "ok", "url": "/srv/elsewhere"}]}, git, self.t3_add)
        self.assertEqual([r["state"] for r in res["results"]], ["error", "error"])
        self.assertEqual(os.listdir(self.b), [])

    def test_out_of_time_new_clones_wait_for_the_next_sync(self):
        ticks = iter([0, 1000, 1000])
        res = p.apply_all(self.b, {"projects": [{"name": "app", "url": self.origin}]}, git, self.t3_add, allow=ANY,
                          clock=lambda: next(ticks), budget=10)
        self.assertEqual(res["results"][0]["state"], "waiting")
        self.assertFalse(os.path.exists(os.path.join(self.b, "app")))

    def test_t3_failure_is_reported_and_retried_next_time(self):
        self.choose()
        p.sync(self.a, {"minastirith": "m"},
               lambda d, r: p.apply_all(self.b, dict(r, projects=[dict(x, url=self.origin) for x in r["projects"]]),
                                        git, lambda path, n: (False, "T3 isn't installed on this box."), allow=ANY),
               git, now=NOW, force=True)
        st = self.state()
        self.assertEqual((st["state"], st["t3"]), ("cloned", False))
        self.assertIn("Not added to T3", st["note"])
        self.sync()
        self.assertTrue(self.state()["t3"])


class Card(unittest.TestCase):
    """The Account tab's "Projects on all your boxes" card (lib/sharedbox-web), from a summary like box makes."""

    @classmethod
    def setUpClass(cls):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("sbweb_projects", os.path.join(sbtestenv.LIB, "sharedbox-web"))
        cls.w = importlib.util.module_from_spec(importlib.util.spec_from_loader("sbweb_projects", loader))
        loader.exec_module(cls.w)

    def page(self, projects):
        return self.w.projects_section({"projects": projects}, "tok", "/_box")

    def test_hidden_without_other_boxes_and_says_so_when_it_cant_list(self):
        self.assertEqual(self.w.projects_section({}, "tok", "/_box"), "")
        self.assertIn("Couldn't list", self.page({"ok": False, "error": "x"}))

    def test_rows_buttons_and_states(self):
        out = self.page({"ok": True, "boxes": {"mini": {"last_sync": None, "error": ""}}, "projects": [
            {"name": "app", "available": True, "everywhere": True, "boxes": {"mini": {"state": "waiting"}}},
            {"name": "web", "available": True, "everywhere": True,
             "boxes": {"mini": {"state": "skipped", "note": "Has uncommitted changes there; not updated.", "t3": True}}},
            {"name": "notes", "available": True, "everywhere": False, "boxes": {}},
            {"name": "loose", "available": False, "why": "No remote yet. Push it to GitHub first."},
            {"name": "<bad>", "available": True}]})
        self.assertIn("class='spin'", out)  # in progress: a spinner, never a check
        self.assertIn("left alone", out)
        self.assertEqual(out.count("Stop syncing"), 2)
        self.assertEqual(out.count("Keep on all boxes"), 1)
        self.assertIn("1 project not on GitHub yet", out)
        self.assertNotIn("<bad>", out)
        self.assertIn("name=\"op\" value=\"off\"", out)
        self.assertEqual(self.w.place_of({"_view": "projects"}), ("account", "projects"))

    def test_button_result_is_a_notice(self):
        """Root answers {ok, message} for "Keep on all boxes" / "Stop syncing"; that is a plain notice, not the
        login-in-progress card (which needs a login token and used to raise KeyError, a 502 at the gate)."""
        btn = lambda *a, **k: ""  # noqa: E731
        out = self.w.result_html({"ok": True, "message": "App is no longer synced.", "_view": "projects"},
                                 btn, "tok", "/_box")
        self.assertIn("App is no longer synced.", out)
        self.assertIn("note ok", out)
        out = self.w.result_html({"ok": True, "message": "The first copy starts soon.", "_view": "projects",
                                  "_busy": True}, btn, "tok", "/_box")
        self.assertIn("note busy", out)  # only queued: the in-progress state, never a check mark
        out = self.w.portal_result({"ok": True, "message": "Something finished."}, btn, "/_box")
        self.assertIn("Something finished.", out)  # any other answer without a login token: a notice, no crash


if __name__ == "__main__":
    unittest.main()
