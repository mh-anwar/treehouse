"""Weekly safe cleanup (lib/sbweekly.py): what it picks, and above all what it never touches: Docker volumes,
tagged images and containers, ~/keep, git repos, ~/.claude, ~/.codex, ~/.t3, anything through a symlink.
    python3 -m unittest tests.test_weekly_clean -v"""

import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
import sbweekly  # noqa: E402

DAY = 86400


def touch(path, data="x" * 100, age_days=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(data)
    if age_days:
        t = time.time() - age_days * DAY
        os.utime(path, (t, t))


class Home(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="sbweekly-"))
        h = self.home
        # things that must go
        for rel in (".npm/_cacache/content-v2/aa", ".cache/go-build/00/a-d", ".cache/sccache/x/y",
                    ".cache/pip/http/z", ".cache/node-gyp/22.0.0/include/node.h", ".cache/google-chrome/Default/c"):
            touch(os.path.join(h, rel))
        touch(os.path.join(h, ".cache/google-chrome-for-testing-headless/scoped_dirOLD/f"), age_days=3)
        os.utime(os.path.join(h, ".cache/google-chrome-for-testing-headless/scoped_dirOLD"),
                 (time.time() - 3 * DAY,) * 2)
        touch(os.path.join(h, ".cache/google-chrome-for-testing-headless/scoped_dirNEW/f"))
        for b in ("chromium-1248", "chromium-1254", "chromium_headless_shell-1248", "chromium_headless_shell-1254",
                  "ffmpeg-1013", "firefox-1555"):
            touch(os.path.join(h, ".cache/ms-playwright", b, "bin"))
        touch(os.path.join(h, ".cache/ms-playwright/.links/abc"))
        os.makedirs(os.path.join(h, ".cache/ms-playwright/daemon"))
        # things that must never go
        self.never = [
            "keep/.cache/pip/important", "keep/.npm/_cacache/x",
            ".claude/projects/session.jsonl", ".codex/sessions/s.jsonl", ".t3/runtime/versions/1/t3",
            "proj/.cache/pip/f", "proj/node_modules/x", ".cache/uv/archive-v0/x",
            ".local/share/docker/volumes/db/_data/PG_VERSION", ".local/share/docker/image/overlay2/repositories.json",
            ".config/google-chrome/Default/Preferences", ".cache/other-tool/data",
        ]
        for rel in self.never:
            touch(os.path.join(h, rel), age_days=60)
        os.makedirs(os.path.join(h, "proj", ".git"))
        # a repo whose cache-looking folder sits where a cache would be: ~/.npm is a git repo
        os.makedirs(os.path.join(h, ".cache", "pip-repo"))

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def picked(self, **kw):
        return {os.path.relpath(p, self.home) for p, _ in sbweekly.select_targets(self.home, **kw)}

    def test_selects_rebuildable_caches(self):
        got = self.picked()
        for rel in (".npm/_cacache/content-v2", ".cache/go-build/00", ".cache/sccache/x", ".cache/pip/http",
                    ".cache/node-gyp/22.0.0", ".cache/google-chrome/Default",
                    ".cache/google-chrome-for-testing-headless/scoped_dirOLD",
                    ".cache/ms-playwright/chromium-1248", ".cache/ms-playwright/chromium_headless_shell-1248"):
            self.assertIn(rel, got)

    def test_playwright_keeps_newest_of_each_browser_and_bookkeeping(self):
        got = self.picked()
        for rel in ("chromium-1254", "chromium_headless_shell-1254", "ffmpeg-1013", "firefox-1555", ".links",
                    "daemon"):
            self.assertNotIn(f".cache/ms-playwright/{rel}", got)

    def test_recent_headless_run_stays(self):
        self.assertNotIn(".cache/google-chrome-for-testing-headless/scoped_dirNEW", self.picked())

    def test_never_targets_keep_agents_repos_docker_data(self):
        got = self.picked()
        for rel in got:
            top = rel.split(os.sep)[0]
            self.assertNotIn(top, ("keep", ".claude", ".codex", ".t3", "proj", ".local", ".config"), rel)
        for rel in self.never:
            self.assertFalse(any(rel == g or rel.startswith(g + os.sep) for g in got), rel)

    def test_running_tool_leaves_its_cache(self):
        got = self.picked(running={"npm", "go", "chrome"})
        self.assertFalse(any(g.startswith((".npm/", ".cache/go-build/", ".cache/google-chrome/")) for g in got))
        self.assertIn(".cache/pip/http", got)

    def test_in_use_build_stays(self):
        old = os.path.join(self.home, ".cache/ms-playwright/chromium-1248")
        got = self.picked(in_use={os.path.join(old, "bin")})
        self.assertNotIn(".cache/ms-playwright/chromium-1248", got)

    def test_symlinked_cache_into_keep_is_refused(self):
        shutil.rmtree(os.path.join(self.home, ".cache/pip"))
        os.symlink(os.path.join(self.home, "keep/.cache/pip"), os.path.join(self.home, ".cache/pip"))
        got = self.picked()
        self.assertFalse(any(g.startswith(".cache/pip") for g in got))
        self.assertIsNotNone(sbweekly.safe_target(os.path.join(self.home, ".cache/pip/important"), self.home))

    def test_cache_inside_git_repo_is_refused(self):
        os.makedirs(os.path.join(self.home, ".cache/go-build/.git"))
        self.assertEqual(sbweekly.safe_target(os.path.join(self.home, ".cache/go-build/00"), self.home),
                         "inside a git repository")
        self.assertFalse(any(g.startswith(".cache/go-build/") for g in self.picked()))

    def test_safe_target_refuses_everything_outside_the_list(self):
        h = self.home
        for rel in ("keep/x", ".claude/x", ".codex/x", ".t3/x", "proj/.cache/pip/f", ".cache/other-tool/data",
                    ".local/share/docker/volumes/db", ".bashrc", "proj"):
            self.assertIsNotNone(sbweekly.safe_target(os.path.join(h, rel), h), rel)
        self.assertIsNotNone(sbweekly.safe_target("/etc/passwd", h))
        self.assertIsNotNone(sbweekly.safe_target(os.path.join(h, ".cache/pip/../../keep/x"), h))
        self.assertIsNone(sbweekly.safe_target(os.path.join(h, ".cache/pip/http"), h))

    def test_run_user_deletes_only_selected(self):
        with mock.patch.dict(os.environ, {"HOME": self.home}), \
                mock.patch.object(sbweekly, "docker_pass", return_value=[]), \
                mock.patch.object(sbweekly, "native_pruners", return_value=[]), \
                mock.patch.object(sbweekly.sbclean, "my_paths", return_value=set()), \
                mock.patch.object(sbweekly, "running_tools", return_value=set()):
            dry = sbweekly.run_user({"dry_run": True})
            self.assertTrue(os.path.exists(os.path.join(self.home, ".cache/pip/http/z")))
            self.assertGreater(dry["freed"], 0)
            rep = sbweekly.run_user({"dry_run": False})
        self.assertEqual(rep["freed"], dry["freed"])
        self.assertFalse(os.path.exists(os.path.join(self.home, ".cache/pip/http")))
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".cache/pip")))  # the folder itself stays
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".cache/ms-playwright/chromium-1254")))
        for rel in self.never:
            self.assertTrue(os.path.exists(os.path.join(self.home, rel)), rel)


class Docker(unittest.TestCase):
    def test_only_build_cache_and_dangling_images(self):
        cmds = sbweekly.docker_commands("/run/user/1000/docker.sock")
        self.assertEqual(len(cmds), 2)
        flat = [a for args, _ in cmds for a in args]
        for bad in ("volume", "volumes", "--volumes", "-a", "--all", "container", "containers", "system", "rm",
                    "rmi", "network"):
            self.assertNotIn(bad, flat)
        (b, _), (i, _) = cmds
        self.assertEqual(b[2:4], ["builder", "prune"])
        self.assertIn("until=168h", b)
        self.assertEqual(i[2:], ["image", "prune", "--force"])  # no -a: untagged (dangling) images only
        self.assertEqual(b[:2], ["-H", "unix:///run/user/1000/docker.sock"])

    def test_missing_socket_never_starts_a_daemon(self):
        with mock.patch.object(sbweekly, "_docker") as run:
            self.assertEqual(sbweekly.docker_pass("/nonexistent/docker.sock", False, "x"), [])
            run.assert_not_called()

    def test_reclaimed_parsed(self):
        self.assertEqual(sbweekly.parse_size("1.5GB"), 1_500_000_000)
        self.assertEqual(sbweekly.parse_size("512kB"), 512_000)
        self.assertEqual(sbweekly.parse_size("374.2M"), int(374.2 * 1024 ** 2))
        self.assertEqual(sbweekly.parse_size("0B"), 0)
        m = sbweekly.TOTAL_RE.search("Deleted build cache objects:\nabc\n\nTotal:\t2.1GB\n")
        self.assertEqual(sbweekly.parse_size(m.group(1)), 2_100_000_000)
        m = sbweekly.TOTAL_RE.search("Deleted Images:\ndeleted: sha256:x\n\nTotal reclaimed space: 30MB\n")
        self.assertEqual(sbweekly.parse_size(m.group(1)), 30_000_000)


class Schedule(unittest.TestCase):
    def at(self, day):  # a timestamp on the given weekday, noon local time
        t = time.mktime(time.strptime("2026-11-23 12:00", "%Y-%m-%d %H:%M"))  # a Monday
        return t + sbweekly.DAYS.index(day) * DAY

    def test_due_on_the_day_once_a_week(self):
        cfg = {"WEEKLY_CLEAN": "1", "WEEKLY_CLEAN_DAY": "Sun"}
        sun = self.at("Sun")
        self.assertTrue(sbweekly.due(cfg, None, sun))           # never ran: waits for the day
        self.assertFalse(sbweekly.due(cfg, None, self.at("Sat")))
        self.assertFalse(sbweekly.due(cfg, sun - 3600, sun))    # already ran today
        self.assertTrue(sbweekly.due(cfg, sun - 7 * DAY, sun))
        self.assertTrue(sbweekly.due(cfg, self.at("Tue") - 9 * DAY, self.at("Tue")))  # overdue: any day
        self.assertFalse(sbweekly.due(cfg, sun - 6.5 * DAY, self.at("Sat")))

    def test_off_and_bad_day(self):
        self.assertFalse(sbweekly.due({"WEEKLY_CLEAN": "0", "WEEKLY_CLEAN_DAY": "Sun"}, None, self.at("Sun")))
        self.assertEqual(sbweekly.weekday({"WEEKLY_CLEAN_DAY": "wednesday"}), "Wed")
        self.assertEqual(sbweekly.weekday({"WEEKLY_CLEAN_DAY": "someday"}), "Sun")

    def test_event_reasons_are_from_a_fixed_list(self):
        rows = [{"reason": "pip cache", "bytes": 5}, {"reason": "\x1b[31mevil", "bytes": 7},
                {"reason": "pip cache", "bytes": 1}, {"reason": "Docker build cache", "bytes": 0}]
        self.assertEqual(sbweekly.by_reason(rows), {"pip cache": 6, "other": 7})


class JanitorBusy(unittest.TestCase):
    """The janitor skips an account whose agent is mid-turn and doesn't stamp it (retried next run)."""

    def setUp(self):
        lib = sbtestenv.LIB
        loader = importlib.machinery.SourceFileLoader("sbjan_weekly", os.path.join(lib, "sharedbox-janitor"))
        self.jan = importlib.util.module_from_spec(importlib.util.spec_from_loader("sbjan_weekly", loader))
        loader.exec_module(self.jan)

    def test_busy_skipped_idle_cleaned(self):
        from types import SimpleNamespace
        busy = SimpleNamespace(pw_name="busy", pw_uid=4242, pw_dir="/nonexistent")
        idle = SimpleNamespace(pw_name="idle", pw_uid=4343, pw_dir="/nonexistent")
        calls = []
        rep = {"deleted": [{"path": "/x", "bytes": 10, "reason": "pip cache"}], "freed": 10}
        j = self.jan
        with mock.patch.object(j, "weekly_accounts", return_value={"busy": busy, "idle": idle}), \
                mock.patch.object(j.sb, "busy_agents", return_value={99: (4242, "claude")}), \
                mock.patch.object(j, "user_pass", side_effect=lambda pw, *a, **k: calls.append(pw.pw_name) or rep), \
                mock.patch.object(j.sbweekly, "run_root", return_value={"deleted": [], "freed": 0}), \
                mock.patch.object(j.sb, "log_event") as ev:
            j.weekly_pass({"WEEKLY_CLEAN": "1"}, dry=False, force=True)
        self.assertEqual(calls, ["idle"])
        st = j.sb.read_json(j.WEEKLY_STATE, {})
        self.assertIn("idle", st)
        self.assertNotIn("busy", st)
        logged = [c.args[1] for c in ev.call_args_list if c.args[0] == "weekly_clean"]
        self.assertIn("idle", logged)
        self.assertNotIn("busy", logged)


if __name__ == "__main__":
    unittest.main()
