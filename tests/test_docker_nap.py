"""Idle Docker databases (`box _docker-nap`): what gets stopped, and above all what never does.
Stdlib only, no Docker needed: the container list, connections, CPU and agent checks are faked.
    python3 -m unittest discover -s tests -v"""

import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
TMP = tempfile.mkdtemp(prefix="sbnap-")
CONF = os.path.join(TMP, "sharedbox.conf")
with open(CONF, "w") as f:
    f.write("DOCKER_NAP_MINUTES=60\n")
os.environ.update(SHAREDBOX_CONF=CONF, SHAREDBOX_STATE=os.path.join(TMP, "state"), SHAREDBOX_LIB=LIB,
                  SHAREDBOX_SHARE=os.path.join(ROOT, "share"), HOME=TMP)
sys.path.insert(0, LIB)

loader = importlib.machinery.SourceFileLoader("box_nap", os.path.join(LIB, "box"))
box = importlib.util.module_from_spec(importlib.util.spec_from_loader("box_nap", loader))
_argv, sys.argv = sys.argv, ["box"]
loader.exec_module(box)
sys.argv = _argv

HOUR = 3600


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def container(cid, name, image, ports=(), started=0.0, keep=False):
    return {"id": cid, "name": name, "image": image, "ports": set(ports), "keep": keep, "started": started,
            "db": bool(box.DB_IMAGE_RE.search(image)) and not box.DB_HELPER_RE.search(image)}


class DockerNap(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        old = self.now - 5 * HOUR
        self.stacks = {
            "ku-db": [container("db1", "supabase_db_ku-db", "public.ecr.aws/supabase/postgres:17", [54322], old),
                      container("meta1", "supabase_pg_meta_ku-db", "public.ecr.aws/supabase/postgres-meta:v0", [], old),
                      container("st1", "supabase_studio_ku-db", "public.ecr.aws/supabase/studio:1", [54323], old)],
            "mybot": [container("bot", "mybot", "node:22", [20050], old)],
        }
        self.conns, self.cpu, self.turn, self.agents, self.stops = set(), {}, False, {}, []
        for f in ("docker-naps.json", "docker-keep", "docker-napped.log"):
            try:
                os.unlink(os.path.join(TMP, ".sharedbox", f))
            except OSError:
                pass
        self.patch("docker_env", lambda: {"DOCKER_HOST": "unix:///fake"})
        self.patch("docker_stacks", lambda env: self.stacks)
        self.patch("established_local_ports", lambda: self.conns)
        self.patch("container_cpu_usec", lambda cid: self.cpu.get(cid, 0))
        self.patch("t3_db_state", lambda: (self.turn, False))
        orig_run = box.subprocess.run

        def run(cmd, **kw):
            if cmd[:2] == ["docker", "stop"]:
                self.stops.append(sorted(cmd[2:]))
                return subprocess.CompletedProcess(cmd, 0)
            return orig_run(cmd, **kw)
        self.patch_obj(box.subprocess, "run", run)
        self.patch_obj(box.sb, "busy_agents", lambda **kw: self.agents)
        self.patch_obj(box.shutil, "which", lambda n: "/usr/bin/" + n)
        self.patch_obj(box.time, "time", lambda: self.now)

    def patch(self, name, fn):
        self.patch_obj(box, name, fn)

    def patch_obj(self, obj, name, fn):
        old = getattr(obj, name)
        setattr(obj, name, fn)
        self.addCleanup(setattr, obj, name, old)

    def nap(self, minutes_later=10):
        self.now += minutes_later * 60
        return box.docker_nap(60)

    def test_idle_stack_stopped_whole_and_apps_never(self):
        self.nap(0)
        self.assertEqual(self.nap(10)["stopped"], [])  # running for hours, but this timer only just looked
        self.assertEqual(self.nap(40)["stopped"], [])
        res = self.nap(10)
        self.assertEqual(res["stopped"], ["ku-db"])
        self.assertEqual(self.stops, [["db1", "meta1", "st1"]])  # the whole stack goes, the bot never does

    def test_agent_turn_running_never_stops_and_hour_restarts(self):
        self.nap(0)
        self.turn = True
        self.assertEqual(self.nap(30)["stopped"], [])
        self.assertEqual(self.nap(40)["waited"], "agent turn running")  # due, but an agent is working
        self.assertEqual(self.nap(120)["stopped"], [])  # a long turn: still never
        self.turn = False  # the agent finished: a full quiet hour from now, not right away
        self.assertEqual(self.nap(10)["stopped"], [])
        self.assertEqual(self.nap(40)["stopped"], [])
        self.assertEqual(self.nap(25)["stopped"], ["ku-db"])

    def test_agent_process_working_never_stops(self):
        self.agents = {123: (1000, "claude")}
        self.nap(0)
        self.assertEqual(self.nap(70)["stopped"], [])
        self.assertEqual(self.stops, [])

    def test_open_connection_keeps_it_up(self):
        self.conns = {54322}
        self.nap(0)
        for _ in range(12):
            self.assertEqual(self.nap(10)["stopped"], [])

    def test_queries_keep_it_up_but_helper_chatter_does_not(self):
        self.nap(0)
        self.nap(50)
        self.cpu["meta1"] = 10**9  # postgres-meta polling all day: not use
        self.cpu["db1"] = 60 * 10**6  # the database itself: 60 CPU-s in 10 min = 10% of a core
        self.assertEqual(self.nap(10)["stopped"], [])
        self.assertEqual(self.nap(50)["stopped"], [])  # idle again, but busy 50 min ago
        self.assertEqual(self.nap(15)["stopped"], ["ku-db"])

    def test_recently_started_gets_full_hour(self):
        self.nap(0)
        self.nap(50)
        for c in self.stacks["ku-db"]:  # the user (or an agent) started it again just now
            c["started"] = self.now
        self.assertEqual(self.nap(20)["stopped"], [])  # watched for 70 min, but only up for 20
        self.assertEqual(self.nap(30)["stopped"], [])
        self.assertEqual(self.nap(15)["stopped"], ["ku-db"])

    def test_opt_outs(self):
        os.makedirs(os.path.join(TMP, ".sharedbox"), exist_ok=True)
        with open(os.path.join(TMP, ".sharedbox", "docker-keep"), "w") as f:
            f.write("ku-db\n")
        self.nap(0)
        self.assertEqual(self.nap(70), {"stopped": [], "kept": ["ku-db"], "waited": ""})
        os.unlink(os.path.join(TMP, ".sharedbox", "docker-keep"))
        self.stacks["ku-db"][0]["keep"] = True
        self.assertEqual(self.nap(10)["kept"], ["ku-db"])

    def test_db_image_detection(self):
        for img in ("postgres:16", "redis:7-alpine", "mysql", "mongo:7", "bitnami/mariadb:11",
                    "public.ecr.aws/supabase/postgres:17.11", "valkey/valkey:8"):
            self.assertTrue(container("x", "x", img)["db"], img)
        for img in ("node:22", "public.ecr.aws/supabase/postgres-meta:v0.1", "supabase/studio:1",
                    "prometheuscommunity/postgres-exporter", "redis/redisinsight", "nginx", "myredisapp-ui"):
            self.assertFalse(container("x", "x", img)["db"], img)


if __name__ == "__main__":
    unittest.main()


class FastShell(unittest.TestCase):
    """`box tune shell`: slow nvm lines become one fast block; everything else in the file stays."""
    RC = ('export PATH="$HOME/.local/bin:$PATH"\n'
          'export NVM_DIR="$HOME/.nvm"; [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" --no-use && nvm use --silent default\n'
          'alias ll="ls -l"\n'
          '# a comment mentioning nvm.sh stays\n'
          'export NVM_DIR="$HOME/.nvm"\n'
          '[ -s "$NVM_DIR/nvm.sh" ] && \\. "$NVM_DIR/nvm.sh"  # This loads nvm\n'
          '[ -s "$NVM_DIR/bash_completion" ] && \\. "$NVM_DIR/bash_completion"\n'
          '. "$HOME/.local/bin/env"\n')

    def test_rewrite(self):
        new, changed = box.fast_nvm_rewrite(self.RC)
        self.assertTrue(changed)
        self.assertEqual(new.count(box.FAST_NVM_BEGIN), 1)
        body = new.replace(box.FAST_NVM, "")
        self.assertNotIn('. "$NVM_DIR/nvm.sh"', body)
        for keep in ('alias ll="ls -l"', "# a comment mentioning nvm.sh stays", '. "$HOME/.local/bin/env"',
                     'export PATH="$HOME/.local/bin:$PATH"'):
            self.assertIn(keep, new)
        self.assertLess(new.index('export PATH="$HOME/.local/bin'), new.index(box.FAST_NVM_BEGIN))  # same place

    def test_idempotent_and_detection(self):
        new, _ = box.fast_nvm_rewrite(self.RC)
        again, _ = box.fast_nvm_rewrite(new)
        self.assertEqual(again.count(box.FAST_NVM_BEGIN), 1)
        d = tempfile.mkdtemp()
        with open(os.path.join(d, ".bashrc"), "w") as f:
            f.write(self.RC)
        self.assertEqual(box.eager_nvm_files(d), [".bashrc"])
        with open(os.path.join(d, ".bashrc"), "w") as f:
            f.write(new)
        self.assertEqual(box.eager_nvm_files(d), [])
        shutil.rmtree(d)
