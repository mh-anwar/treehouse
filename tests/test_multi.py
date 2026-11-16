"""Tests for lib/sbmulti.py (multi-box planning). Stdlib only, no network, no SSH:
    python3 -m unittest tests.test_multi -v"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
import sbmulti as m  # noqa: E402


def row(name, score, ok=True):
    return {"name": name, "score": score, "ok": ok}


class Score(unittest.TestCase):
    def test_idle_box_scores_its_free_cores_times_speed(self):
        self.assertEqual(m.score({"ncpu": 4, "load1": 0.0, "level": "calm", "mem": 5}, 0.5), 2.0)

    def test_busy_full_or_out_of_memory_scores_low(self):
        self.assertEqual(m.score({"ncpu": 8, "load1": 9, "level": "calm"}), 0.0)
        self.assertEqual(m.score({"ncpu": 8, "load1": 0, "mem": 95}), 0.0)
        self.assertEqual(m.score({"ncpu": 8, "load1": 0, "disk_free": 2}), 0.0)
        self.assertAlmostEqual(m.score({"ncpu": 8, "load1": 0, "level": "busy"}), 2.4)
        self.assertEqual(m.score(None), 0.0)

    def test_speeds_parse_and_ignore_junk(self):
        self.assertEqual(m.speeds({"BOX_SPEED": "minastirith=0.45, x=abc y=99 Big=2"}), {"minastirith": 0.45, "big": 2.0})


class Pick(unittest.TestCase):
    def test_stays_here_unless_another_box_clearly_has_more_room(self):
        self.assertEqual(m.pick([row("a", 4), row("b", 5)], "a"), "a")
        self.assertEqual(m.pick([row("a", 1), row("b", 3)], "a"), "b")

    def test_never_picks_unreachable(self):
        self.assertEqual(m.pick([row("a", 0), row("b", 9, ok=False)], "a"), "a")

    def test_only_this_box(self):
        self.assertEqual(m.pick([row("a", 0)], "a"), "a")


class Shards(unittest.TestCase):
    def test_proportional_and_complete(self):
        plan = m.plan_shards(6, [row("a", 1), row("b", 2)], "a")
        self.assertEqual([i for i, _ in plan], list(range(6)))
        self.assertEqual(sum(1 for _, b in plan if b == "b"), 4)

    def test_everything_here_when_nobody_has_room(self):
        self.assertEqual(m.plan_shards(3, [row("a", 0), row("b", 0)], "a"), [(0, "a"), (1, "a"), (2, "a")])

    def test_unreachable_box_gets_nothing(self):
        self.assertTrue(all(b == "a" for _, b in m.plan_shards(4, [row("a", 1), row("b", 9, ok=False)], "a")))


class Identity(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def test_ssh_config_alias_wins_when_it_points_at_the_ip(self):
        self.assertEqual(m.ssh_dest("mt", "100.64.0.20", "pip", self.home, lambda n: "user x\nhostname 100.64.0.20\n"),
                         "mt")

    def test_falls_back_to_same_user_at_ip(self):
        self.assertEqual(m.ssh_dest("mt", "100.64.0.20", "pip", self.home, lambda n: "hostname mt\n"),
                         "pip@100.64.0.20")

    def test_boxes_json_override_and_rejects_option_injection(self):
        os.makedirs(os.path.join(self.home, ".sharedbox"))
        p = os.path.join(self.home, ".sharedbox", "boxes.json")
        with open(p, "w") as f:
            json.dump({"mt": "radagast@100.64.0.20"}, f)
        self.assertEqual(m.ssh_dest("mt", "100.64.0.20", "pip", self.home, lambda n: ""), "radagast@100.64.0.20")
        with open(p, "w") as f:
            json.dump({"mt": "-oProxyCommand=evil"}, f)
        self.assertEqual(m.ssh_dest("mt", "100.64.0.20", "pip", self.home, lambda n: ""), "pip@100.64.0.20")


def run_script(script, home, close_after=None):
    """Run a generated remote script with local sh, stdin held open like ssh does (closing it = interrupt)."""
    p = subprocess.Popen(["sh", "-c", script], env={"HOME": home, "PATH": "/usr/bin:/bin"},
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if close_after is not None:
        import time
        time.sleep(close_after)
        p.stdin.close()
    out = p.stdout.read()
    rc = p.wait(20)
    p.stdout.close()
    if not p.stdin.closed:
        p.stdin.close()
    return rc, out


class SyncAndScript(unittest.TestCase):
    def test_mirror_path_is_stable_and_safe(self):
        a = m.mirror_rel("host", "/home/u/projects/my app")
        self.assertEqual(a, m.mirror_rel("host", "/home/u/projects/my app"))
        self.assertNotEqual(a, m.mirror_rel("host", "/home/u/projects/other"))
        self.assertRegex(a, r"^\.sharedbox/mirror/my_app-[0-9a-f]{10}$")

    def test_rsync_git_uses_gitignore_filter_and_skips_dot_git(self):
        argv = m.rsync_argv("/p", True, "u@h", "rel")
        self.assertIn("--filter=:- .gitignore", argv)
        self.assertIn("--exclude=/.git", argv)
        self.assertEqual(argv[-2:], ["/p/", "u@h:rel/"])
        self.assertNotIn("--exclude=/.git", m.rsync_argv("/p", True, "u@h", "rel", with_git=True))
        self.assertIn("--exclude=node_modules", m.rsync_argv("/p", False, "u@h", "rel"))

    def test_prep_key_changes_with_lockfile(self):
        d = tempfile.mkdtemp()
        k0 = m.prep_key(d, "npm ci")
        with open(os.path.join(d, "package-lock.json"), "w") as f:
            f.write("{}")
        k1 = m.prep_key(d, "npm ci")
        self.assertNotEqual(k0, k1)
        self.assertEqual(k1, m.prep_key(d, "npm ci"))
        self.assertNotEqual(k1, m.prep_key(d, "pnpm i"))

    def test_remote_script_runs_and_returns_exit_code(self):
        """Run the generated script with a local sh, a fake HOME as the 'remote' home."""
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, "rel", "sub"))
        s = m.remote_script("rel", "sub", ["sh", "-c", 'echo "$BOX_SHARD:$(basename $PWD)"; exit 3'],
                            {"BOX_SHARD": 2}, prep="touch prepped", key="k1")
        rc, out = run_script(s, home)
        self.assertEqual(rc, 3)
        self.assertIn(b"2:sub", out)
        self.assertTrue(os.path.exists(os.path.join(home, "rel", "sub", "prepped")))
        with open(os.path.join(home, "rel.prep")) as f:
            self.assertEqual(f.read().strip(), "k1")
        os.remove(os.path.join(home, "rel", "sub", "prepped"))
        run_script(s, home)
        self.assertFalse(os.path.exists(os.path.join(home, "rel", "sub", "prepped")), "prep re-ran with same key")

    def test_remote_script_missing_folder(self):
        self.assertEqual(run_script(m.remote_script("nope", ".", ["true"]), tempfile.mkdtemp())[0], 97)

    def test_closing_stdin_kills_the_remote_job(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, "rel"))
        import time
        t = time.time()
        rc, _ = run_script(m.remote_script("rel", ".", ["sleep", "30"]), home, close_after=0.5)
        self.assertNotEqual(rc, 0)
        self.assertLess(time.time() - t, 10)


if __name__ == "__main__":
    unittest.main()
