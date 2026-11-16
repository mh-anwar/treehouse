"""Team metadata on every box: roster teams, copies (mirrors), conflicts, deletions, old rosters, forwarding.

Run: python3 -m unittest tests/test_team_federation.py   (no root, no network; a temp state dir)
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
import sbcommon as sb  # noqa: E402
import sbfed as f  # noqa: E402
import sbteam  # noqa: E402

NOW = 1_800_000_000
ME = sbteam.me()


def roster(box, teams=None, ts=NOW, serial=5):
    """A peer's roster; teams=None is a box too old to send teams."""
    return f.build_roster(box, "primary", {}, serial, ts, teams=teams)


def meta(members, home=None, **kw):
    m = {"members": list(members), "created_by": members[0] if members else "", "created": 1700000000, **kw}
    if home:
        m["home"] = home
    return m


class RosterTeams(unittest.TestCase):
    def test_local_teams_published_copies_and_other_homes_not(self):
        metas = {"alpha": meta(["dave", "kim"], home="main"),
                 "legacy": meta(["dave"]),                          # from before homes: this box's
                 "theirs": meta(["dave"], home="second", mirror=True),
                 "stray": meta(["dave"], home="second"),
                 "badmembers": meta(["dave", "Not A Name"], home="main"),
                 "Bad Name": meta(["dave"], home="main")}
        doc = f.build_roster("main", "primary", {}, 1, NOW, teams=metas)
        self.assertEqual(sorted(doc["teams"]), ["alpha", "legacy"])
        self.assertEqual(doc["teams"]["alpha"], {"members": ["dave", "kim"], "home": "main", "created_by": "dave",
                                                 "created": 1700000000})
        self.assertEqual(doc["teams"]["legacy"]["home"], "main")

    def test_only_metadata_travels(self):
        doc = f.build_roster("main", "primary", {}, 1, NOW,
                             teams={"alpha": meta(["dave"], home="main", secret="x", dir="/srv/teams/alpha")})
        self.assertEqual(set(doc["teams"]["alpha"]), {"members", "home", "created_by", "created"})

    def test_no_teams_key_when_unreadable(self):
        self.assertNotIn("teams", f.build_roster("main", "primary", {}, 1, NOW))
        self.assertEqual(f.build_roster("main", "primary", {}, 1, NOW, teams={})["teams"], {})

    def test_roster_with_teams_still_passes_checks(self):
        doc = roster("second", {"alpha": meta(["dave"], home="second")})
        self.assertEqual(f.check_roster(doc, "second", 4, NOW), (True, ""))


class PlanTeams(unittest.TestCase):
    def plan(self, rosters, local=None, me="main", now=NOW):
        return f.plan_teams(rosters, local or {}, me, now)

    def copy(self, members, home="second", created_by=None):
        return {"name": "alpha", "members": members, "home": home, "created_by": created_by or members[0],
                "created": 1700000000, "mirror": True}

    def test_new_team_is_copied_and_marked(self):
        p = self.plan({"second": roster("second", {"alpha": meta(["dave", "kim"], home="second")})})
        self.assertEqual(p, [("mirror", "alpha", self.copy(["dave", "kim"]))])

    def test_unchanged_copy_is_left_alone(self):
        r = {"second": roster("second", {"alpha": meta(["dave"], home="second")})}
        self.assertEqual(self.plan(r, {"alpha": self.copy(["dave"])}), [])

    def test_member_change_propagates(self):
        r = {"second": roster("second", {"alpha": meta(["dave", "kim"], home="second")})}
        p = self.plan(r, {"alpha": self.copy(["dave"])})
        self.assertEqual(p, [("mirror", "alpha", self.copy(["dave", "kim"]))])

    def test_deleted_on_home_drops_the_copy(self):
        r = {"second": roster("second", {})}
        self.assertEqual(self.plan(r, {"alpha": self.copy(["dave"])}), [("drop", "alpha", "second")])

    def test_local_team_never_dropped_or_overridden(self):
        local = {"alpha": meta(["kim"], home="main")}
        p = self.plan({"second": roster("second", {"alpha": meta(["dave"], home="second")})}, local)
        self.assertEqual([a for a, _, _ in p], ["conflict"])
        self.assertIn("this box keeps its own", p[0][2])
        self.assertEqual(self.plan({"second": roster("second", {})}, local), [])
        # a local team from before homes (no "home") is just as local
        self.assertEqual([a for a, _, _ in self.plan(
            {"second": roster("second", {"alpha": meta(["dave"], home="second")})}, {"alpha": meta(["kim"])})],
            ["conflict"])

    def test_old_roster_without_teams_changes_nothing(self):
        self.assertEqual(self.plan({"second": roster("second", None)}, {"alpha": self.copy(["dave"])}), [])

    def test_stale_roster_changes_nothing(self):
        old = roster("second", {}, ts=NOW - f.MAX_AGE - 10)
        self.assertEqual(self.plan({"second": old}, {"alpha": self.copy(["dave"])}), [])
        old = roster("second", {"beta": meta(["dave"], home="second")}, ts=NOW - f.MAX_AGE - 10)
        self.assertEqual(self.plan({"second": old}), [])

    def test_peer_cannot_publish_a_team_homed_elsewhere(self):
        doc = roster("second", {})
        doc["teams"]["alpha"] = {"members": ["dave"], "home": "main", "created_by": "dave", "created": 1}
        doc["teams"]["beta"] = {"members": ["dave"], "home": "third", "created_by": "dave", "created": 1}
        self.assertEqual(self.plan({"second": doc}), [])

    def test_own_roster_is_ignored(self):
        self.assertEqual(self.plan({"main": roster("main", {"alpha": meta(["dave"], home="main")})}), [])

    def test_bad_teams_value_is_ignored(self):
        doc = roster("second", None)
        doc["teams"] = ["alpha"]
        self.assertEqual(self.plan({"second": doc}, {"alpha": self.copy(["dave"])}), [])

    def test_two_boxes_one_name(self):
        r = {"second": roster("second", {"alpha": meta(["dave"], home="second")}),
             "third": roster("third", {"alpha": meta(["kim"], home="third")})}
        p = self.plan(r, {"alpha": self.copy(["dave"])})
        self.assertEqual([a for a, _, _ in p], ["conflict"])  # the existing copy stays
        p = self.plan(r)
        self.assertEqual([a for a, _, _ in p], ["conflict", "mirror"])
        self.assertEqual(p[1][2]["home"], "second")

    def test_moved_to_another_box(self):
        r = {"second": roster("second", {}), "third": roster("third", {"alpha": meta(["dave"], home="third")})}
        p = self.plan(r, {"alpha": self.copy(["dave"])})
        self.assertEqual(p, [("mirror", "alpha", self.copy(["dave"], home="third"))])


class TeamDirs(unittest.TestCase):
    """A temp teams dir (empty) and a clean metadata dir in the test state dir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["SHAREDBOX_TEAMS"] = self.tmp
        self.meta_dir = os.path.join(sb.STATE_DIR, "teams")
        shutil.rmtree(self.meta_dir, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.meta_dir, ignore_errors=True)
        os.environ.pop("SHAREDBOX_TEAMS", None)

    def write_local(self, name, m):
        os.makedirs(self.meta_dir, exist_ok=True)
        sb.write_json(sbteam.meta_path(name), m, mode=0o644)

    def mirror(self, name="alpha", members=(ME, "kim"), home="second"):
        r = {home: roster(home, {name: meta(list(members), home=home)})}
        return sbteam.sync_mirrors(r, "main", NOW)


class SyncMirrors(TeamDirs):
    def test_copy_written_like_local_metadata(self):
        done = self.mirror()
        self.assertEqual([(a, n, e) for a, n, _, e in done], [("mirror", "alpha", "")])
        m = sbteam.read_meta("alpha")
        self.assertTrue(m["mirror"])
        self.assertEqual(m["home"], "second")
        self.assertEqual(m["members"], [ME, "kim"])
        self.assertEqual(stat.S_IMODE(os.stat(sbteam.meta_path("alpha")).st_mode), 0o644)
        self.assertEqual(self.mirror(), [])  # nothing changed: nothing written

    def test_deletion_and_member_changes_propagate(self):
        self.mirror()
        self.mirror(members=("kim",))
        self.assertEqual(sbteam.read_meta("alpha")["members"], ["kim"])
        done = sbteam.sync_mirrors({"second": roster("second", {})}, "main", NOW)
        self.assertEqual([a for a, *_ in done], ["drop"])
        self.assertFalse(os.path.exists(sbteam.meta_path("alpha")))

    def test_old_peer_roster_keeps_copies(self):
        self.mirror()
        self.assertEqual(sbteam.sync_mirrors({"second": roster("second", None)}, "main", NOW), [])
        self.assertTrue(sbteam.read_meta("alpha"))

    def test_local_team_wins_and_is_untouched(self):
        local = meta(["kim"], home="main")
        self.write_local("alpha", local)
        done = self.mirror()
        self.assertEqual([a for a, *_ in done], ["conflict"])
        self.assertEqual(sbteam.read_meta("alpha"), local)
        sbteam.sync_mirrors({"second": roster("second", {})}, "main", NOW)
        self.assertEqual(sbteam.read_meta("alpha"), local)

    def test_load_metas_feeds_the_roster(self):
        self.write_local("beta", meta(["kim"], home="main"))
        self.mirror()
        doc = f.build_roster("main", "primary", {}, 1, NOW, teams=sbteam.load_metas())
        self.assertEqual(list(doc["teams"]), ["beta"])  # the copy isn't passed on

    def test_create_refused_over_a_copy(self):
        self.mirror()
        res = sbteam.handle({}, ME, {"action": "team-create", "name": "alpha", "members": []})
        self.assertFalse(res["ok"])
        self.assertIn("lives on box second", res["error"])
        self.assertTrue(sbteam.read_meta("alpha")["mirror"])


class FedSyncLog(TeamDirs):
    def test_admin_logs_changes_and_each_conflict_once(self):
        loader = importlib.machinery.SourceFileLoader("teamfed_admin", os.path.join(sbtestenv.LIB, "sharedbox-admin"))
        admin = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(admin)
        st = {"applied": {}, "log": []}
        self.write_local("beta", meta(["kim"], home="main"))
        docs = {"second": roster("second", {"alpha": meta([ME], home="second"), "beta": meta(["kim"], home="second")})}
        with contextlib.redirect_stdout(io.StringIO()):
            admin.fed_apply_teams(st, docs, "main", NOW)
            admin.fed_apply_teams(st, docs, "main", NOW)
        self.assertEqual(len([ln for ln in st["log"] if "team alpha: copy from second" in ln]), 1)
        self.assertEqual(len([ln for ln in st["log"] if "team beta: NOT copying" in ln]), 1)


class Stop(Exception):
    pass


class ForwardFromCopy(TeamDirs):
    def run_main(self, argv):
        with patch("sbteam.box_name", return_value="main"), \
                patch("sbcommon.boxes", return_value={"main": "100.64.0.1", "second": "100.64.0.2"}), \
                patch("sbmulti.ssh_dest", return_value=f"{ME}@second") as dest, \
                patch("os.execvp", side_effect=Stop) as execute:
            with self.assertRaises(Stop):
                sbteam.main(argv, {})
        dest.assert_called_with("second", "100.64.0.2", ME, os.path.expanduser("~"))
        return execute.call_args[0]

    def test_named_team_forwards(self):
        self.mirror()
        self.assertEqual(self.run_main(["--team", "alpha", "post", "hello there"]),
                         ("ssh", ["ssh", f"{ME}@second", "box", "team", "--team", "alpha", "post", "'hello there'"]))

    def test_only_team_found_and_forwarded(self):
        self.mirror()
        self.assertEqual(self.run_main(["board"]),
                         ("ssh", ["ssh", f"{ME}@second", "box", "team", "--team", "alpha", "board"]))

    def test_members_only(self):
        self.mirror(members=("kim",))
        self.assertEqual(sbteam.my_teams(), [])
        self.assertEqual(sbteam.mirrored_teams(), {})
        with self.assertRaises(SystemExit):
            sbteam.resolve("alpha")

    def test_listed_with_its_home(self):
        self.mirror()
        self.assertEqual(sbteam.my_teams(), ["alpha"])
        self.assertEqual(sbteam.mirrored_teams(), {"alpha": "second"})
        self.assertEqual(sbteam.my_teams(mirrored=False), [])
        rules = sbteam.rules_section()
        self.assertIn("on box second", rules)
        self.assertIn("ssh second cat", rules)

    def test_clone_runs_here_over_ssh(self):
        self.mirror()
        calls = []

        class R:
            returncode = 0

        with patch("os.path.expanduser", return_value=self.tmp), contextlib.redirect_stdout(io.StringIO()):
            rc = sbteam.clone_remote("alpha", {}, f"{ME}@second", run=lambda argv, **kw: calls.append(argv) or R())
        self.assertEqual(rc, 0)
        self.assertEqual(calls[1], ["git", "clone", f"{ME}@second:{self.tmp}/alpha/repo.git",
                                    os.path.join(self.tmp, "alpha")])
        self.assertEqual(calls[0][:2], ["ssh", f"{ME}@second"])


if __name__ == "__main__":
    unittest.main()
