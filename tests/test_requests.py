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
        adm.denied = lambda cfg, pkgs: []
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

    def put(self, state):
        rid = f"pippin-{int(time.time())}-1"
        adm.req_update(lambda db: db["requests"].__setitem__(rid, dict(
            id=rid, user="pippin", kind="install", packages=["webkit2gtk-driver"], ts=int(time.time()),
            state=state)))
        return rid

    def run_worker(self):
        adm.process_requests(dict(adm.sb.load_config(), REQUEST_AUTO_INSTALL="yes", REQUEST_DAILY_LIMIT="0", REQUEST_MAX_DEPS="0"))

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

    def test_unapproved_uninstallable_still_asks_owner(self):
        self.distro = FakeDistro((False, [], "E: Unable to satisfy dependencies"))
        rid = self.put("new")
        self.run_worker()
        self.assertEqual(adm.req_db()["requests"][rid]["state"], "waiting")


if __name__ == "__main__":
    unittest.main()
