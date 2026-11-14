"""Package requests: a wrong package name, or an approved one that still can't install, must close the request
instead of mailing the owner again on every approve. Stdlib only, no root, no network:
    python3 -m unittest tests.test_requests -v"""

import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
TMP = tempfile.mkdtemp(prefix="sbreq-")
CONF = os.path.join(TMP, "sharedbox.conf")
with open(CONF, "w") as f:
    f.write("REQUEST_AUTO_INSTALL=yes\n")
os.environ.update(SHAREDBOX_CONF=CONF, SHAREDBOX_STATE=os.path.join(TMP, "state"), SHAREDBOX_LIB=LIB,
                  SHAREDBOX_SHARE=os.path.join(ROOT, "share"))
os.makedirs(os.path.join(TMP, "state"), exist_ok=True)
sys.path.insert(0, LIB)


def load_script(name, modname):
    loader = importlib.machinery.SourceFileLoader(modname, os.path.join(LIB, name))
    spec = importlib.util.spec_from_loader(modname, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


adm = load_script("sharedbox-admin", "sbadmin_req")
# other test modules may have loaded sbcommon with their own (now deleted) state dir: use ours
adm.REQ_DB, adm.REQ_LOCK, adm.REQ_WORKER_LOCK = (os.path.join(TMP, n) for n in ("db.json", ".lock", ".wlock"))


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class FakeDistro:
    class Unsupported(Exception):
        pass

    VETTED = {}

    def __init__(self, sim):
        self.sim, self.installs = sim, []

    def native_names(self, names):
        return list(names)

    def installed(self, p):
        return False

    def simulate_install(self, pkgs):
        return self.sim


class RequestLoop(unittest.TestCase):
    def setUp(self):
        self.mails, self.finished = [], []
        adm.sbdistro = lambda: self.distro
        adm.req_ingest = lambda cfg: []
        adm.write_request_views = lambda cfg, db=None: None
        adm.req_mail_owner = lambda cfg: None
        adm.req_mail_failed = lambda cfg, r, why: self.mails.append(why)
        for name in ("ai_review", "req_install", "run"):
            self.addCleanup(setattr, adm, name, getattr(adm, name))
        adm.auto_installed_today = lambda db, user: 0
        adm.add_notice = lambda *a, **k: None
        for name, fake in (("friend_users", lambda: []), ("log_event", lambda *a, **k: None),
                           ("write_json", self.write_json)):  # sbcommon is shared with other test modules
            self.addCleanup(setattr, adm.sb, name, getattr(adm.sb, name))
            setattr(adm.sb, name, fake)
        adm.req_save(dict(requests={}, fyi=[]))

    @staticmethod
    def write_json(path, data, mode=0o600):
        import json
        with open(path, "w") as f:
            json.dump(data, f)

    def put(self, state, pkg="webkit2gtk-driver"):
        rid = f"pippin-{int(time.time())}-1"
        adm.req_update(lambda db: db["requests"].__setitem__(rid, dict(
            id=rid, user="pippin", kind="install", packages=[pkg], ts=int(time.time()),
            state=state)))
        return rid

    def run_worker(self, **over):
        adm.process_requests(dict(adm.sb.load_config(), **dict(dict(
            REQUEST_AUTO_INSTALL="yes", REQUEST_DAILY_LIMIT="0", REQUEST_MAX_DEPS="0", REQUEST_DENY=""), **over)))

    def test_wrong_name_closes_without_mail(self):
        self.distro = FakeDistro((False, [], "E: Package 'webkit2gtk-driver' has no installation candidate"))
        rid = self.put("new")
        self.run_worker()
        r = adm.req_db()["requests"][rid]
        self.assertEqual((r["state"], r["by"]), ("declined", "auto"))
        self.assertIn("check the exact name", r["reason"])
        self.assertEqual(self.mails, [])

    def test_approved_but_uninstallable_closes_and_mails_once(self):
        self.distro = FakeDistro((False, [], "E: Unable to satisfy dependencies"))
        rid = self.put("approved")
        self.run_worker()
        r = adm.req_db()["requests"][rid]
        self.assertEqual(r["state"], "declined")
        self.assertNotIn(r["state"], adm.REQ_OPEN)
        self.assertEqual(len(self.mails), 1)

    def test_unapproved_uninstallable_asks_owner_when_reviewer_is_owner(self):
        self.distro = FakeDistro((False, [], "E: Unable to satisfy dependencies"))
        rid = self.put("new")
        self.run_worker(REQUEST_REVIEWER="owner")
        self.assertEqual(adm.req_db()["requests"][rid]["state"], "waiting")

    # ---- REQUEST_REVIEWER=ai: whatever the rules hold back is settled without the owner

    def held(self, verdict, pkg="postgresql", sim=(True, ["postgresql"], "")):
        """A request the auto rules hold back (REQUEST_DENY), reviewed by a fake AI answering `verdict`."""
        self.distro = FakeDistro(sim)
        self.reviews = []
        adm.ai_review = lambda cfg, r, names, deps, why: self.reviews.append(names) or verdict
        self.installs = []
        adm.req_install = lambda names: self.installs.append(names) or (True, "")
        rid = self.put("new", pkg)
        self.run_worker(REQUEST_DENY="postgresql sudo")
        return adm.req_db()["requests"][rid]

    def test_ai_install(self):
        r = self.held(("install", "fine"))
        self.assertEqual((r["state"], self.installs), ("installed", [["postgresql"]]))

    def test_ai_decline_reaches_agent_and_fyi(self):
        r = self.held(("decline", "run it rootless with Docker"))
        self.assertEqual((r["state"], r["by"], self.installs), ("declined", "auto", []))
        self.assertIn("rootless", r["reason"])
        self.assertEqual(adm.req_db()["fyi"][-1]["outcome"], "declined")

    def test_ai_unavailable_falls_back_to_owner(self):
        r = self.held(None)
        self.assertEqual(r["state"], "waiting")

    def test_never_list_skips_the_ai(self):
        r = self.held(("install", "sure"), pkg="sudo", sim=(True, ["sudo"], ""))
        self.assertEqual((r["state"], self.reviews, self.installs), ("declined", [], []))

    def test_never_list_checks_dependencies(self):
        r = self.held(("install", "sure"), sim=(True, ["postgresql", "systemd"], ""))
        self.assertEqual((r["state"], self.reviews), ("declined", []))

    # ---- REQUEST_OPEN_USERS: a hackathon opens requests for some users, never past REQUEST_NEVER

    def opened(self, pkg="postgresql", sim=(True, ["postgresql"], ""), user="pippin", until=""):
        self.distro = FakeDistro(sim)
        self.reviews, self.installs = [], []
        adm.ai_review = lambda cfg, r, names, deps, why: self.reviews.append(names) or ("decline", "no")
        adm.req_install = lambda names: self.installs.append(names) or (True, "")
        rid = self.put("new", pkg)
        self.run_worker(REQUEST_DENY="postgresql sudo", REQUEST_MAX_DEPS="1", REQUEST_OPEN_USERS="kim " + user,
                        REQUEST_OPEN_UNTIL=until)
        return adm.req_db()["requests"][rid]

    def test_open_user_skips_deny_and_dep_cap(self):
        r = self.opened(sim=(True, ["postgresql", "libpq5"], ""))
        self.assertEqual((r["state"], self.reviews, self.installs), ("installed", [], [["postgresql"]]))
        self.assertTrue(r.get("auto"))

    def test_open_user_still_blocked_by_never(self):
        r = self.opened(pkg="sudo", sim=(True, ["sudo"], ""))
        self.assertEqual((r["state"], self.installs), ("declined", []))
        r = self.opened(sim=(True, ["postgresql", "systemd-timesyncd"], ""))
        self.assertEqual((r["state"], self.installs), ("declined", []))

    def test_other_users_not_opened(self):
        r = self.opened(user="dave")
        self.assertEqual((self.reviews, self.installs), ([["postgresql"]], []))

    def test_open_window_expires(self):
        r = self.opened(until="2000-01-01 00:00")
        self.assertEqual(self.installs, [])
        r = self.opened(until="2999-01-01 00:00")
        self.assertEqual(self.installs, [["postgresql"]])
        r = self.opened(until="tonight")
        self.assertEqual(self.installs, [])  # a typo closes it

    def test_owner_can_approve_an_auto_declined_request(self):
        r = self.held(("decline", "no"))
        adm.run = lambda *a, **k: None
        self.assertEqual(adm.request_act({}, r["id"], "approve")["state"], "approved")


class QuietInstall(unittest.TestCase):
    """A requested package's system services are switched off; nothing else is touched."""

    def setUp(self):
        self.calls = []
        for name in ("sbdistro", "run", "POLICY_RC_D"):
            self.addCleanup(setattr, adm, name, getattr(adm, name))
        self.addCleanup(setattr, adm.sb, "log_event", adm.sb.log_event)
        adm.sb.log_event = lambda *a, **k: None
        adm.POLICY_RC_D = os.path.join(TMP, "policy-rc.d")
        test = self

        class D(FakeDistro):
            sets = [{"base"}, {"base", "postgresql", "postgresql-18"}]

            def current(self):
                return "debian"

            def installed_set(self):
                return self.sets.pop(0)

            def installed(self, p):
                return True

            def system_units(self, pkgs):
                test.asked = pkgs
                return ["postgresql.service"]

        adm.sbdistro = lambda: D(None)

        def run(cmd, **k):
            self.calls.append(cmd)
            if "sharedbox-tools" in cmd[0]:
                self.during = os.path.exists(adm.POLICY_RC_D)
            return type("R", (), {"stdout": "", "stderr": "", "returncode": 0})()
        adm.run = run

    def test_services_off(self):
        ok, note = adm.req_install(["postgresql"])
        self.assertTrue(ok)
        self.assertTrue(self.during)  # services couldn't start during the install
        self.assertFalse(os.path.exists(adm.POLICY_RC_D))  # and the guard is gone afterwards
        self.assertEqual(self.asked, ["postgresql", "postgresql-18"])  # only the newly installed packages
        self.assertIn(["systemctl", "disable", "--now", "postgresql.service"], self.calls)
        self.assertIn("run it as yourself", note)

    def test_existing_policy_rc_d_left_alone(self):
        with open(adm.POLICY_RC_D, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        adm.req_install(["postgresql"])
        with open(adm.POLICY_RC_D) as f:
            self.assertEqual(f.read(), "#!/bin/sh\nexit 0\n")
        os.unlink(adm.POLICY_RC_D)


class Reviewer(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(dir=TMP)
        self.pw = type("pw", (), {"pw_dir": self.home})()
        for name in ("review_openrouter", "review_claude", "sbdistro"):
            self.addCleanup(setattr, adm, name, getattr(adm, name))
        self.addCleanup(setattr, adm.sb, "owner_account", adm.sb.owner_account)
        adm.sb.owner_account = lambda cfg: self.pw
        adm.sbdistro = lambda: FakeDistro(None)
        FakeDistro.describe = lambda self, names: {}

    def opencode_login(self, key):
        os.makedirs(os.path.join(self.home, ".local/share/opencode"))
        with open(os.path.join(self.home, ".local/share/opencode/auth.json"), "w") as f:
            f.write(f'{{"openrouter": {{"type": "api", "key": "{key}"}}}}')

    def test_key_from_opencode_login(self):
        self.opencode_login("sk-or-v1-" + "a" * 40)
        self.assertEqual(adm.openrouter_key({"OPENROUTER_KEY_FILE": "/nonexistent"}, self.pw), "sk-or-v1-" + "a" * 40)

    def test_junk_key_ignored(self):
        self.opencode_login("not a key")
        self.assertIsNone(adm.openrouter_key({"OPENROUTER_KEY_FILE": "/nonexistent"}, self.pw))

    def test_openrouter_first_then_claude(self):
        self.opencode_login("sk-or-v1-" + "b" * 40)
        calls = []
        adm.review_openrouter = lambda cfg, key, prompt: calls.append("or") or {}
        adm.review_claude = lambda cfg, pw, prompt: calls.append("claude") or {"decision": "install", "reason": "ok"}
        cfg = {"OPENROUTER_KEY_FILE": "/nonexistent"}
        self.assertEqual(adm.ai_review(cfg, {"user": "u"}, ["x"], [], "held"), ("install", "ok"))
        self.assertEqual(calls, ["or", "claude"])

    def test_no_answer_returns_none(self):
        adm.review_claude = lambda cfg, pw, prompt: {"decision": "maybe"}
        self.assertIsNone(adm.ai_review({"OPENROUTER_KEY_FILE": "/nonexistent"}, {"user": "u"}, ["x"], [], "held"))


if __name__ == "__main__":
    unittest.main()


class Hackathon(unittest.TestCase):
    def cfg(self, until, **kw):
        c = dict(adm.sb.DEFAULTS, ADMIN_USERS="pippin", HACKATHON_UNTIL=until, **kw)
        return adm.sb.apply_hackathon(c)

    def test_on_overlays_and_opens_requests(self):
        c = self.cfg("2999-01-01 00:00", HACKATHON_USERS="kim")
        self.assertEqual(c["HACKATHON"], "1")
        for k in ("T3_NAP_MINUTES", "T3_IDLE_DAYS", "DOCKER_NAP_MINUTES", "TRAP_ENABLED", "CANARY_ENABLED"):
            self.assertEqual(c[k], "0", k)
        self.assertEqual(c["REQUEST_OPEN_USERS"].split(), ["pippin", "kim"])
        self.assertTrue(adm.open_for(c, "kim"))
        self.assertFalse(adm.open_for(c, "dave"))

    def test_past_or_bad_time_is_off(self):
        for until in ("2000-01-01 00:00", "tonight", ""):
            c = self.cfg(until)
            self.assertEqual(c["HACKATHON"], "", until)
            self.assertEqual(c["TRAP_ENABLED"], adm.sb.DEFAULTS["TRAP_ENABLED"])

    def test_reconcile_acts_only_on_transitions(self):
        calls = []
        orig = adm.sb.subprocess.run
        adm.sb.subprocess.run = lambda cmd, **k: calls.append(cmd)
        self.addCleanup(setattr, adm.sb.subprocess, "run", orig)
        for name in ("log_event", "write_json", "read_json"):
            self.addCleanup(setattr, adm.sb, name, getattr(adm.sb, name))
        state = {}
        adm.sb.read_json = lambda p, d=None: dict(state)
        adm.sb.write_json = lambda p, data, **k: state.update(data)
        adm.sb.log_event = lambda *a, **k: None
        on = self.cfg("2999-01-01 00:00")
        self.assertEqual(adm.sb.hackathon_reconcile(on), "on")
        self.assertIn(["systemctl", "stop", "sharedbox-trap.service"], calls)
        self.assertIsNone(adm.sb.hackathon_reconcile(on))
        calls.clear()
        self.assertEqual(adm.sb.hackathon_reconcile(self.cfg("2000-01-01 00:00", CANARY_ENABLED="0")), "off")
        self.assertEqual(calls, [["systemctl", "enable", "--now", "sharedbox-trap.service"]])
