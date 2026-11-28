"""`box hackathon check` (lib/sbhackathon.py) and the `box swarm` passthrough. Stdlib only, no network:
    python3 -m unittest discover -s tests -v"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
sys.path.insert(0, LIB)
import sbhackathon as h  # noqa: E402

BOXES = {"alpha": "100.64.0.1", "beta": "100.64.0.2", "gamma": "100.64.0.3"}
FAR = "2099-01-01T00:00:00.000Z"


def good_probe(n, repos=("proj",)):
    return {"ok": True, "user": "sam", "signins_known": True,
            "signins": {"claude": True, "codex": True, "antigravity": True}, "t3": "running", "t3_kind": "socket",
            "repos": {r: {"present": True, "t3": True} for r in repos},
            "ssh": {p: True for p in BOXES if p != n}}


def envs_all():
    return [{"label": n, "urls": [f"http://{ip}:20000"], "expiresAt": FAR} for n, ip in BOXES.items() if n != "alpha"]


class LinkTest(unittest.TestCase):
    def test_matches_label_ip_or_hostname(self):
        self.assertTrue(h.t3_linked([{"label": "x", "urls": ["http://100.64.0.2:1"], "expiresAt": FAR}],
                                    "beta", "100.64.0.2"))
        self.assertTrue(h.t3_linked([{"label": "x", "urls": ["http://beta.tail.ts.net:3773"], "expiresAt": FAR}],
                                    "beta", "100.64.0.2"))
        self.assertTrue(h.t3_linked([{"label": "beta", "urls": [], "expiresAt": FAR}], "beta", "100.64.0.2"))
        self.assertFalse(h.t3_linked([{"label": "betamax", "urls": ["http://betamax.ts.net:1"], "expiresAt": FAR}],
                                     "beta", "100.64.0.2"))

    def test_expired_link_does_not_count(self):
        self.assertFalse(h.t3_linked([{"label": "beta", "urls": [], "expiresAt": "2001-01-01T00:00:00Z"}],
                                     "beta", "100.64.0.2"))


class AssessTest(unittest.TestCase):
    def res(self, **over):
        r = {n: {"reachable": True, "probe": good_probe(n)} for n in BOXES}
        r.update(over)
        return r

    def test_all_ready(self):
        rows = h.assess(self.res(), "alpha", BOXES, ["proj"], envs_all(), chosen=["proj"])
        self.assertTrue(all(r["ok"] for r in rows), rows)
        self.assertIn("all 3 machines ready", h.summary_line(rows, ["proj"]))
        self.assertEqual(h.fixes(rows), [])

    def test_problems_carry_fix_or_next_step(self):
        p = good_probe("beta")
        p["signins"]["codex"] = False
        p["signins"]["antigravity"] = None  # not installed: a note, not a problem
        p["t3"] = "stopped"
        p["repos"]["proj"] = {"present": False, "t3": None}
        p["ssh"]["gamma"] = False
        rows = h.assess(self.res(beta={"reachable": True, "probe": p}, gamma={"reachable": False, "probe": {}}),
                        "alpha", BOXES, ["proj"], envs_all(), chosen=["proj"])
        beta = next(r for r in rows if r["machine"] == "beta")
        nexts = [pr["next"] for pr in beta["problems"]]
        self.assertIn("box login codex --on beta", nexts)
        self.assertIn("box projects sync", nexts)
        self.assertIn("not installed: antigravity", beta["lines"])
        fx = h.fixes(rows)
        self.assertIn(("beta", ["box", "_portal", "t3"]), fx)
        self.assertIn(("beta", ["box", "boxes", "setup"]), fx)
        self.assertEqual(fx[-1], ("here", ["box", "projects", "sync"]))  # copying comes after SSH/T3 fixes
        self.assertLess(fx.index(("here", ["box", "boxes", "setup"])), fx.index(("beta", ["box", "_portal", "t3"])))
        # a sign-in is never an automatic fix: AI accounts are per machine
        self.assertFalse([pr for pr in beta["problems"] if "signed in" in pr["what"] and pr["fix"]])
        gamma = next(r for r in rows if r["machine"] == "gamma")
        self.assertFalse(gamma["ok"])
        text = h.render(rows, ["proj"])
        self.assertIn("beta: problem", text)
        self.assertTrue(text.splitlines()[-1].startswith("Hackathon check: "))

    def test_owner_style_t3_and_unknown_links(self):
        p = good_probe("beta")
        p["t3"], p["t3_kind"] = "stopped", "service"
        rows = h.assess(self.res(beta={"reachable": True, "probe": p}), "alpha", BOXES, [], None)
        self.assertIn(("beta", ["systemctl", "--user", "start", "t3code.service"]), h.fixes(rows))
        beta = next(r for r in rows if r["machine"] == "beta")
        self.assertTrue(any("link unknown" in x for x in beta["lines"]))

    def test_project_missing_here_is_reported_once(self):
        res = self.res()
        res["alpha"]["probe"]["repos"]["proj"] = {"present": False, "t3": None}
        for n in ("beta", "gamma"):
            res[n]["probe"]["repos"]["proj"] = {"present": False, "t3": None}
        rows = h.assess(res, "alpha", BOXES, ["proj"], envs_all(), chosen=["proj"])
        self.assertEqual([r["machine"] for r in rows if not r["ok"]], ["alpha"])
        self.assertEqual(h.fixes(rows), [])

    def test_named_repo_gets_chosen_first(self):
        rows = h.assess(self.res(), "alpha", BOXES, ["proj"], envs_all(), chosen=[], named_repo="proj")
        self.assertEqual(h.fixes(rows)[0], ("here", ["box", "projects", "everywhere", "proj", "on"]))


class GatherTest(unittest.TestCase):
    def test_probes_every_machine_with_its_peers(self):
        seen = {}

        def remote(dest, req):
            seen[dest] = req
            return dest != "u@gamma", {"error": "no route"} if dest == "u@gamma" else good_probe(dest[2:])
        res = h.gather(BOXES, "alpha", ["proj"], {"beta": "u@beta", "gamma": "u@gamma"}, LIB,
                       local_probe=lambda req: good_probe("alpha"), remote=remote)
        self.assertEqual(set(res), set(BOXES))
        self.assertEqual(set(seen["u@beta"]["peers"]), {"alpha", "gamma"})
        self.assertFalse(res["gamma"]["reachable"])

    def test_remote_script_is_self_contained(self):
        src = h.remote_script({"peers": {}, "repos": [], "lib": "/nonexistent"})
        compile(src, "probe", "exec")
        self.assertIn("print(json.dumps(probe(", src)


class SwarmTest(unittest.TestCase):
    def run_box(self, path_dir, home):
        env = dict(os.environ, PATH=f"{path_dir}:/usr/bin:/bin", HOME=home, SHAREDBOX_LIB=LIB)
        return subprocess.run([sys.executable, os.path.join(LIB, "box"), "swarm", "start", "an idea", "--who", "sam"],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_passthrough_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            bindir, home = os.path.join(d, "bin"), os.path.join(d, "home")
            os.makedirs(bindir)
            os.makedirs(home)
            r = self.run_box(bindir, home)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("isn't installed on this machine", r.stderr)
            fake = os.path.join(bindir, "swarm")
            with open(fake, "w") as f:
                f.write('#!/bin/sh\nprintf "%s|" "$@"\n')
            os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
            r = self.run_box(bindir, home)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout, "start|an idea|--who|sam|")


if __name__ == "__main__":
    unittest.main()
