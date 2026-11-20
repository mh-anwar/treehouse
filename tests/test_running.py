"""lib/sbrunning.py (the dashboard's Running card, `box running`) on a fake cgroup tree, plus the group-server
description rules (lib/sbshared.py). Stdlib only, no root, no Docker."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
import sbrunning as r  # noqa: E402
import sbshared  # noqa: E402

M = 1 << 20
CID = {n: (n * 64)[:64] for n in "abcdef"}  # fake 64-hex container ids


class Running(unittest.TestCase):
    UID = 4242

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sbrun-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.procs = {}  # pid -> (comm, start epoch, rss)
        self.pid = 1000
        old_root, old_proc, old_boot = r.CGROUP_ROOT, r._proc, r._boot_time
        r.CGROUP_ROOT = os.path.join(self.tmp, "cg")
        r._proc = lambda pid, boot: self.procs.get(int(pid))
        r._boot_time = lambda: 0
        self.addCleanup(setattr, r, "CGROUP_ROOT", old_root)
        self.addCleanup(setattr, r, "_proc", old_proc)
        self.addCleanup(setattr, r, "_boot_time", old_boot)
        self.base = os.path.join(r.CGROUP_ROOT, "user.slice", f"user-{self.UID}.slice")
        um = os.path.join(self.base, f"user@{self.UID}.service")
        self.cg(f"{um}/init.scope", 1, ["systemd"])
        self.cg(f"{um}/app.slice/docker.service", 200, ["rootlesskit", "dockerd"])
        self.cg(f"{um}/app.slice/t3code.service", 1700, ["node"])
        self.cg(f"{um}/app.slice/sb-blog.service", 220, ["node"])
        self.cg(f"{um}/app.slice/sb-gascity.service", 900, ["dolt", "gc"])
        self.cg(f"{um}/app.slice/app-t3code.slice/app-t3code-agents.slice/app-t3code-agents-1.slice/"
                "t3code-claude-1234abcd.scope", 8000, ["claude", "python"])
        self.cg(f"{um}/app.slice/tmux-spawn-11111111-2222.scope", 50, ["tmux", "bash"])
        for k, (proj, name, image, mem, comm) in {
                "a": ("uoft", "supabase_db_uoft", "public.ecr.aws/supabase/postgres:17", 95, "postgres"),
                "b": ("uoft", "supabase_rest_uoft", "public.ecr.aws/supabase/postgrest:v16", 24, "postgrest"),
                "c": ("", "legacy-mongo", "mongodb/mongodb-atlas-local:8.0", 1400, "mongod"),
                "d": ("", "", "", 30, "nginx")}.items():  # d: no metadata readable
            self.cg(f"{um}/user.slice/docker-{CID[k]}.scope", mem, [comm])
            if name:
                d = os.path.join(self.home, ".local/share/docker/containers", CID[k])
                os.makedirs(d)
                with open(os.path.join(d, "config.v2.json"), "w") as f:
                    json.dump({"Name": "/" + name, "Config": {"Image": image, "Labels": (
                        {"com.docker.compose.project": proj} if proj else {})}}, f)
        self.cg(f"{self.base}/session-7.scope", 10, ["sshd", "bash"])
        os.makedirs(f"{um}/app.slice/empty.slice")  # no processes: not an item

    def cg(self, path, mem_mb, comms, start=1_000_000):
        os.makedirs(path, exist_ok=True)
        pids = []
        for c in comms:
            self.pid += 1
            self.procs[self.pid] = (c, start, mem_mb * M // len(comms))
            pids.append(str(self.pid))
        for name, text in (("cgroup.procs", "\n".join(pids)), ("memory.current", str(mem_mb * M)),
                           ("cpu.stat", f"usage_usec {mem_mb * 1000}\n")):
            with open(os.path.join(path, name), "w") as f:
                f.write(text + "\n")

    def items(self, home=True):
        return {i["name"]: i for i in r.running(self.UID, self.home if home else None, now=1_000_000 + 90_000)}

    def test_everything_is_found_and_classified(self):
        it = self.items()
        self.assertEqual(it["uoft"]["kind"], "database")            # compose project = one item
        self.assertEqual(it["uoft"]["detail"], "Supabase · 2 containers")
        self.assertEqual(it["uoft"]["mem"], (95 + 24) * M)
        self.assertEqual(sorted(it["uoft"]["ids"]), sorted([CID["a"], CID["b"]]))
        self.assertEqual(it["legacy-mongo"]["kind"], "database")
        self.assertEqual(it["legacy-mongo"]["stop"], "containers")
        self.assertEqual(it[CID["d"][:12]]["kind"], "container")    # no readable metadata: still listed
        self.assertEqual((it["blog"]["kind"], it["blog"]["stop"]), ("server", "server"))
        self.assertEqual(it["gascity"]["kind"], "database")         # a server running dolt
        self.assertEqual((it["Claude session"]["kind"], it["Claude session"]["stop"]), ("agent", None))
        self.assertEqual((it["T3 Code"]["kind"], it["T3 Code"]["stop"]), ("t3", None))
        self.assertEqual((it["tmux terminal"]["kind"], it["tmux terminal"]["stop"]), ("terminal", "unit"))
        self.assertEqual((it["SSH login"]["kind"], it["SSH login"]["stop"]), ("login", None))
        bg = it["Background"]
        self.assertEqual(bg["mem"], 201 * M)                         # docker engine + init, never stoppable
        self.assertIsNone(bg["stop"])
        self.assertEqual(it["legacy-mongo"]["age"], 90_000)
        names = [i["name"] for i in r.running(self.UID, self.home)]
        self.assertEqual(names[0], "Claude session")                 # biggest first, background last
        self.assertEqual(names[-1], "Background")

    def test_without_the_home_containers_still_show_by_process(self):
        it = self.items(home=False)
        mongo = next(i for i in it.values() if "mongod" in i["procs"])
        self.assertEqual(mongo["kind"], "database")
        self.assertEqual(mongo["name"], CID["c"][:12])

    def test_find_only_returns_stoppable_live_items(self):
        items = r.running(self.UID, self.home)
        self.assertEqual(r.find(items, "c:uoft")["ids"].__len__(), 2)
        self.assertEqual(r.find(items, "u:sb-blog.service")["stop"], "server")
        for bad in ("u:t3code.service", "u:session-7.scope", "c:nope", "u:../../etc", "-rf", "", None, 5,
                    "u:t3code-claude-1234abcd.scope", "c:" + "x" * 200):
            self.assertIsNone(r.find(items, bad), bad)

    def test_worth_mentioning_skips_agents_and_plumbing(self):
        names = {i["name"] for i in r.worth_mentioning(r.running(self.UID, self.home, now=1_000_000 + 90_000))}
        self.assertTrue({"uoft", "legacy-mongo", "gascity"} <= names)
        self.assertFalse(names & {"Claude session", "T3 Code", "Background", "SSH login"})
        self.assertNotIn("tmux terminal", names)  # small

    def test_ago(self):
        self.assertEqual([r.ago(s) for s in (30, 600, 3700, 90_000, 172_800, None)],
                         ["1m", "10m", "1h 1m", "1d 1h", "2d", "?"])


class SharedDescriptions(unittest.TestCase):
    def test_check_desc(self):
        self.assertEqual(sbshared.check_desc("  Discord bot that\nposts the\tschedule  ")[0],
                         "Discord bot that posts the schedule")
        for bad in (None, "", "bot", "x" * 301, 5):
            self.assertTrue(sbshared.check_desc(bad)[1], bad)
        self.assertEqual(sbshared.check_desc("Posts \x1b[31mthe weekly\x07 schedule")[0],
                         "Posts [31mthe weekly schedule")  # no escape sequences reach anyone's page or terminal

    def test_desc_option_is_parsed_out_of_the_command(self):
        a = SimpleNamespace(cmd=["--desc", "Weekly schedule bot", "--json"], port=None, lines=100, json=False)
        self.assertIsNone(sbshared.parse_tail(a))
        self.assertEqual((a.desc, a.cmd, a.json), ("Weekly schedule bot", [], True))
        a = SimpleNamespace(cmd=["Weekly", "schedule", "bot"], port=None, lines=100, json=False)
        self.assertIsNone(sbshared.parse_tail(a))
        self.assertEqual(a.cmd, ["Weekly", "schedule", "bot"])


if __name__ == "__main__":
    unittest.main()
