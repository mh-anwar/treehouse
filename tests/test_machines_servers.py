"""Machines, per-machine AI accounts, server descriptions and keep running, real memory use, the real disk, agent
rules that follow sudo, and the wired-port netplan file. Stdlib only, no root, no network.
    python3 -m unittest tests.test_machines_servers -v"""

import argparse
import base64
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
import sbcommon as sb  # noqa: E402
import sbrunning  # noqa: E402
import sbshared  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
TMP = tempfile.mkdtemp(prefix="sbmach-")
_home, os.environ["HOME"] = os.environ.get("HOME"), TMP


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    argv, sys.argv = sys.argv, [os.path.basename(path)]
    try:
        loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


box = load("box_machines", os.path.join(LIB, "box"))
web = load("sbweb_machines", os.path.join(LIB, "sharedbox-web"))
admin = load("sbadmin_machines", os.path.join(LIB, "sharedbox-admin"))
if _home is not None:
    os.environ["HOME"] = _home


def read(path):
    with open(path) as f:
        return f.read()


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class Memory(unittest.TestCase):
    """What a slice really uses: anon + shmem + kernel memory that isn't a cache. Page cache and reclaimable slab
    (left behind by programs that exited) are reported apart, as reclaimable cache."""

    def cg(self, stat, current):
        d = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(d, "memory.stat"), "w") as f:
            f.write("".join(f"{k} {v}\n" for k, v in stat.items()))
        with open(os.path.join(d, "memory.current"), "w") as f:
            f.write(f"{current}\n")
        return d

    def test_system_slice_with_old_cache(self):
        M = 1 << 20
        # nar-shaddaa's system.slice: anon 190M, kernel 390M (mostly dentry cache), file cache 4.6G
        d = self.cg({"anon": 190 * M, "file": 4700 * M, "kernel": 390 * M, "shmem": 4 * M,
                     "slab_reclaimable": 330 * M}, 5300 * M)
        used, cache = sbrunning.mem_split(d)
        self.assertEqual(used, (190 + 4 + 60) * M)
        self.assertEqual(cache, (4700 - 4 + 330) * M)
        self.assertEqual(sbrunning.mem_used(d), used)
        c = sb.read_cgroup(d)
        self.assertEqual((c["mem_used"], c["mem_cache"]), (used, cache))
        self.assertEqual(c["mem"], 5300 * M)  # memory.current stays available, but nothing shows it to people

    def test_old_kernel_and_unreadable(self):
        d = self.cg({"anon": 100, "file": 50, "kernel_stack": 10, "pagetables": 5, "slab_unreclaimable": 7,
                     "slab_reclaimable": 3}, 999)
        self.assertEqual(sbrunning.mem_split(d), (122, 53))
        empty = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(empty, "memory.current"), "w") as f:
            f.write("4096\n")
        self.assertIsNone(sbrunning.mem_split(empty))
        self.assertEqual(sbrunning.mem_used(empty), 4096)  # no memory.stat: memory.current is all there is


class Disk(unittest.TestCase):
    """The admin Disk card used to say "1.4M / 3.0G": the web service can't see /home. It now uses the root-run
    health sampler's numbers (or "/")."""

    def test_from_metrics(self):
        old = web.metrics_tail
        self.addCleanup(setattr, web, "metrics_tail", old)
        web.metrics_tail = lambda s: [{"ts": 1, "disk_free": 40.0, "disk_free_b": 200 << 30, "disk_total_b": 500 << 30}]
        self.assertEqual(web.real_disk(), (500 << 30, 200 << 30))
        web.metrics_tail = lambda s: [{"ts": 1, "disk_free": 40.0, "disk_free_b": 200 << 30}]  # older sampler lines
        self.assertEqual(web.real_disk(), (500 << 30, 200 << 30))
        web.metrics_tail = lambda s: []
        total, free = web.real_disk()
        self.assertGreater(total, 1 << 30)  # "/" of the machine running the tests, never a tiny private mount


class ServeMeta(unittest.TestCase):
    """`box serve`: what a server is for lives in its unit file (X-Treehouse-* lines systemd ignores)."""

    def test_unit_round_trip(self):
        meta = {"about": "Telegram bot for the study group, 100% up", "project": "bot", "by": "alice",
                "agent": "Claude Code", "thread": "thread:abc", "created": "2026-11-28 10:00",
                "importance": "important", "url": "", "keep": True, "hackathon": "pause"}
        text = box.serve_unit_text("bot", "/home/alice/bot", 42003, ["/usr/bin/python3", "bot.py"], meta)
        self.assertIn("Restart=always", text)
        self.assertIn("ManagedOOMPreference=omit", text)
        self.assertIn("X-Treehouse-About=Telegram bot for the study group, 100%% up", text)  # systemd's % doubled
        m = sbrunning.parse_unit_meta(text)
        self.assertEqual(m["about"], "Telegram bot for the study group, 100% up")
        self.assertTrue(m["keep"])
        self.assertEqual(m["hackathon"], "")  # keep running wins: hackathon mode never pauses it
        self.assertEqual((m["port"], m["project"], m["agent"]), ("42003", "bot", "Claude Code"))
        plain = sbrunning.parse_unit_meta(box.serve_unit_text("x", "/w", None, ["/bin/true"],
                                                              {"about": "", "hackathon": "pause"}))
        self.assertFalse(plain["keep"])
        self.assertEqual(plain["hackathon"], "pause")
        self.assertIn("/bin/true", plain["exec"])

    def test_options_anywhere(self):
        o, cmd = box.serve_opts(["--about", "my site", "--port", "5", "--important", "--", "python3", "-m", "x"],
                                need_cmd=True)
        self.assertEqual((o.about, o.port, o.important, cmd), ("my site", 5, True, ["python3", "-m", "x"]))
        o, cmd = box.serve_opts(["--about", "a", "--safe-to-stop", "node", "server.js", "--port", "9"], need_cmd=True)
        self.assertEqual(cmd, ["node", "server.js", "--port", "9"])  # after the command: the command's own flags
        with self.assertRaises(SystemExit):
            box.serve_opts(["--nope"])

    def test_add_needs_a_description(self):
        a = argparse.Namespace(action="add", name="web", port=None, follow=False, cmd=["--", "python3", "-V"])
        with self.assertRaises(SystemExit) as cm:
            box.cmd_serve(a)
        self.assertIn("--about", str(cm.exception))
        self.assertIn("--important", str(cm.exception))
        a.cmd = ["--about", "Site", "--", "python3", "-V"]
        with self.assertRaises(SystemExit) as cm:
            box.cmd_serve(a)
        self.assertNotIn("--about \"one plain", str(cm.exception))
        self.assertIn("--safe-to-stop", str(cm.exception))

    def test_running_items_get_their_description(self):
        d = os.path.join(TMP, "h1", ".config", "systemd", "user")
        os.makedirs(d)
        with open(os.path.join(d, "sb-blog.service"), "w") as f:
            f.write(box.serve_unit_text("blog", "/w", 1, ["/bin/true"], {"about": "The blog", "keep": True}))
        items = sbrunning.with_meta([{"unit": "sb-blog.service", "kind": "server"}, {"unit": "sb-t3.service"}],
                                    os.path.join(TMP, "h1"))
        self.assertEqual((items[0]["about"], items[0]["keep"]), ("The blog", True))
        self.assertNotIn("about", items[1])

    def test_hackathon_pause_needs_root(self):
        self.assertEqual(sb.hackathon_servers(sb.load_config(), True), "" if os.geteuid() else sb.hackathon_servers(
            sb.load_config(), True))


class GroupServers(unittest.TestCase):
    def test_always_kept_running(self):
        cfg = sb.load_config()
        u = sbshared.render_unit(cfg, "web", ["/usr/bin/python3", "app.py"], 42060)
        self.assertIn("Restart=always", u)
        self.assertIn("ManagedOOMPreference=omit", u)
        self.assertNotIn("OOMScoreAdjust=500", u)
        self.assertIn("ManagedOOMPreference=omit", sbshared.KEEP_DROPIN)


class Accounts(unittest.TestCase):
    """Which account each AI login is, from the agents' own files (never a token)."""

    def jwt(self, claims):
        b = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return f"x.{b}.y"

    def test_login_account(self):
        home = box.HOME
        os.makedirs(os.path.join(home, ".codex"), exist_ok=True)
        with open(os.path.join(home, ".codex", "auth.json"), "w") as f:
            json.dump({"tokens": {"id_token": self.jwt({"email": "me@chatgpt.example"})}}, f)
        self.assertEqual(box.login_account("codex"), "me@chatgpt.example")
        os.makedirs(os.path.dirname(box.AGY_TOKEN), exist_ok=True)
        with open(box.AGY_TOKEN, "w") as f:
            json.dump({"token": "secret", "id_token_email": "me@gmail.example"}, f)
        self.assertEqual(box.login_account("antigravity"), "me@gmail.example")
        with open(os.path.join(home, ".codex", "auth.json"), "w") as f:
            json.dump({"tokens": {"id_token": self.jwt({"email": "<script>@x"})}}, f)
        self.assertEqual(box.login_account("codex"), "")  # anything that isn't a plain email is dropped
        self.assertEqual(box.login_account("claude"), "")  # no OAuth credentials: the one-year token has no email

    def test_new_actions_are_allowed_for_the_owner(self):
        for act in ("peer-code", "logout", "peer-logout", "serve-set", "running-stop-any"):
            self.assertIn(act, admin.OWNER_PORTAL_ACTIONS)
            self.assertIn(act, web.OWNER_ACTIONS)
        self.assertEqual(admin.ACCOUNT_KEYS, ("claude", "codex", "antigravity"))


class StopAnything(unittest.TestCase):
    def test_owner_stop_refuses_unknown(self):
        cfg = sb.load_config()
        self.assertFalse(admin.running_stop_any(cfg, "owner", {"user": "nobody-here", "key": "u:sb-x.service"})["ok"])
        self.assertFalse(admin.running_stop_any(cfg, "owner", {"user": "alice", "key": "-rf"})["ok"])
        self.assertIn("running-stop-any", admin.OWNER_PORTAL_ACTIONS)

    def test_confirmations_say_whose_it_is(self):
        btn = web.portal_btn("tok", "/_box")
        items = [{"key": "u:sb-t3.service", "kind": "t3", "name": "T3", "stop": "t3", "mem": 1},
                 {"key": "u:sb-bot.service", "kind": "server", "name": "bot", "stop": "server", "mem": 1, "keep": True,
                  "about": "Study group bot"}]
        theirs = "".join(web.run_rows(items, btn, "mk30", "owner"))
        self.assertIn("This is mk30&#x27;s T3. Their open chats will disconnect", theirs.replace("\\u2019", "'"))
        self.assertIn("marked keep running by mk30", theirs)
        self.assertIn("name='force' value='1'", theirs)
        self.assertIn("running-stop-any", theirs)
        self.assertIn("Study group bot", theirs)
        own = "".join(web.run_rows(items, btn))  # a person's own Running card: no Stop for T3 Code
        self.assertEqual(own.count("running-stop\""), 1)
        self.assertNotIn("This is", own)


class Rules(unittest.TestCase):
    def test_sudo_section_follows_the_account(self):
        tpl = read(os.path.join(ROOT, "share", "AGENTS.md"))
        self.assertIn("{sudo_section}", tpl)
        self.assertNotIn("## No sudo, ever", tpl)
        self.assertIn("## No sudo, ever", box.NO_SUDO)
        has = box.HAS_SUDO.format(user="radagast", host="minastirith")
        self.assertIn("`radagast` can use `sudo` without a password on minastirith", has)
        self.assertNotIn("Never run `sudo`", has)
        old = box.sudo_mode
        self.addCleanup(setattr, box, "sudo_mode", old)
        box.sudo_mode = lambda: "yes"
        self.assertIn("can use `sudo`", box.rule_fields()["sudo_section"])
        box.sudo_mode = lambda: "no"
        self.assertIn("No sudo, ever", box.rule_fields()["sudo_section"])


class Netplan(unittest.TestCase):
    def test_wired_ports_get_dhcp_without_a_mac(self):
        text = read(os.path.join(ROOT, "etc", "netplan-wired.yaml"))
        body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        self.assertIn('match: {name: "en*"}', body)
        for k in ("dhcp4: true", "dhcp6: true", "optional: true"):
            self.assertIn(k, body)
        self.assertNotIn("macaddress", body)
        inst = read(os.path.join(ROOT, "install.sh"))
        self.assertIn("networkctl reload", inst)
        self.assertNotRegex(inst, r"(?m)^[^#]*netplan apply")


class Help(unittest.TestCase):
    def test_servers_topic_asks_for_descriptions(self):
        t = box.help_topics()
        self.assertIn("--about", t["servers"])
        self.assertIn("--keep-running", t["servers"])
        self.assertIn("box serve hackathon", t["hackathon"] + t["servers"])
        self.assertIn("--on MACHINE", t["logins"])
        self.assertFalse(re.search(r"\bthe box\b", t["servers"] + t["hackathon"] + t["logins"]))


if __name__ == "__main__":
    unittest.main()
