"""Tests for lib/sbfed.py (accounts across boxes). Pure logic plus a real ssh-keygen sign/verify round trip:
    python3 -m unittest tests.test_federation -v"""

import base64
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
try:
    import sbtestenv  # noqa: F401,E402  (throwaway SHAREDBOX dirs; from the shared test helpers)
except ImportError:
    pass
import sbfed as f  # noqa: E402

NOW = 1_800_000_000


def fake_key(tag="a", kind="ssh-ed25519"):
    blob = len(kind).to_bytes(4, "big") + kind.encode() + (32).to_bytes(4, "big") + tag.encode().ljust(32, b"x")
    return f"{kind} {base64.b64encode(blob).decode()} {tag}@box"


def roster(box="sietchtabr", role="primary", users=None, serial=5, ts=NOW):
    return f.build_roster(box, role, users or {}, serial, ts)


def u(uid, status="active", keys=(), home=None, limits=None):
    d = {"uid": uid, "status": status, "keys": list(keys), "limits": limits or {}}
    if home:
        d["home"] = home
    return d


class Keys(unittest.TestCase):
    def test_plain_key_kept_and_comment_tidied(self):
        k = fake_key("dave")
        self.assertEqual(f.clean_key(k + " ;rm -rf"), k + "rm-rf")

    def test_options_and_garbage_refused(self):
        self.assertIsNone(f.clean_key('command="sh" ' + fake_key()))
        self.assertIsNone(f.clean_key("ssh-ed25519 !!!notbase64"))
        self.assertIsNone(f.clean_key(""))

    def test_type_must_match_blob(self):
        k = fake_key()
        self.assertIsNone(f.clean_key("ssh-rsa " + k.split()[1]))

    def test_managed_block_replaces_only_ours(self):
        user = "ssh-ed25519 AAAA mine\n"
        t1 = f.managed_block(user, ["k1"])
        self.assertTrue(t1.startswith(user) and "k1" in t1)
        t2 = f.managed_block(t1, ["k2"])
        self.assertNotIn("k1", t2)
        self.assertIn("k2", t2)
        self.assertEqual(t2.count(f.BEGIN), 1)
        self.assertEqual(f.managed_block(t2, []), user)

    def test_own_keys_skip_our_block(self):
        mine, peer = fake_key("laptop"), fake_key("peer")
        text = mine + "\n" + f.managed_block("", [peer])
        self.assertEqual(f.own_keys(text), [mine])


class Roster(unittest.TestCase):
    def test_build_drops_bad_names_and_keys_and_unknown_limits(self):
        d = roster(users={"dave": u(2000, keys=[fake_key(), "junk"], limits={"MEMORY_MAX": "9G", "X": "1"}),
                          "Bad Name": u(2001)})
        self.assertEqual(list(d["users"]), ["dave"])
        self.assertEqual(len(d["users"]["dave"]["keys"]), 1)
        self.assertEqual(d["users"]["dave"]["limits"], {"MEMORY_MAX": "9G"})
        self.assertEqual(d["users"]["dave"]["home"], "sietchtabr")

    def test_check_refuses_replay_wrong_box_and_future(self):
        d = roster(serial=10)
        self.assertEqual(f.check_roster(d, "sietchtabr", 9, NOW), (True, ""))
        self.assertTrue(f.check_roster(d, "sietchtabr", 10, NOW)[0])  # same one again: harmless
        self.assertFalse(f.check_roster(d, "sietchtabr", 11, NOW)[0])
        self.assertFalse(f.check_roster(d, "minastirith", 0, NOW)[0])
        self.assertFalse(f.check_roster(roster(ts=NOW + 3600), "sietchtabr", 0, NOW)[0])

    def test_stale_roster_is_not_fresh(self):
        self.assertTrue(f.fresh(roster(), NOW + 3600))
        self.assertFalse(f.fresh(roster(), NOW + f.MAX_AGE + 1))


class PlanAccounts(unittest.TestCase):
    def plan(self, users, local=None, passwd=None):
        return f.plan_accounts(roster(users=users), local or {}, passwd or {}, 2000, 2999)

    def test_new_active_user_is_created_with_same_uid(self):
        self.assertEqual(self.plan({"dave": u(2000)}), [("create", "dave", 2000)])

    def test_suspended_upstream_and_absent_here_is_not_created(self):
        self.assertEqual(self.plan({"dave": u(2000, "suspended")}), [])

    def test_uid_conflicts_are_reported_never_forced(self):
        self.assertEqual(self.plan({"dave": u(2000)}, passwd={"radagast": 2000})[0][0], "conflict")
        self.assertEqual(self.plan({"dave": u(2000)}, passwd={"gid:2000": "docker"})[0][0], "conflict")
        self.assertEqual(self.plan({"dave": u(2000)}, passwd={"dave": 1000})[0][0], "conflict")
        self.assertEqual(self.plan({"dave": u(500)})[0][0], "conflict")

    def test_local_account_not_a_mirror_is_left_alone(self):
        p = self.plan({"dave": u(2000, "suspended")}, local={"dave": {"uid": 2000, "status": "active"}})
        self.assertEqual(p[0][0], "conflict")

    def test_suspend_resume_follow_upstream(self):
        local = {"dave": {"uid": 2000, "status": "active", "home_box": "sietchtabr"}}
        self.assertEqual(self.plan({"dave": u(2000, "suspended")}, local)[0][:2], ("suspend", "dave"))
        local["dave"]["status"] = "suspended"
        self.assertEqual(self.plan({"dave": u(2000)}, local)[0][:2], ("resume", "dave"))

    def test_removed_upstream_is_suspended_not_deleted(self):
        local = {"kim": {"uid": 2001, "status": "active", "home_box": "sietchtabr"}}
        p = self.plan({}, local)
        self.assertEqual(p, [("suspend", "kim", "removed on sietchtabr (home kept here)")])
        self.assertFalse(any(a in ("delete", "remove") for a, _, _ in p))

    def test_limits_follow_upstream(self):
        local = {"dave": {"uid": 2000, "status": "active", "home_box": "sietchtabr", "limits": {}}}
        self.assertEqual(self.plan({"dave": u(2000, limits={"MEMORY_MAX": "9G"})}, local),
                         [("limits", "dave", {"MEMORY_MAX": "9G"})])

    def test_users_homed_elsewhere_are_ignored(self):
        self.assertEqual(self.plan({"dave": u(2000, home="minastirith")}), [])


class MergeKeys(unittest.TestCase):
    def test_only_same_uid_active_and_recent(self):
        k1, k2, k3 = fake_key("1"), fake_key("2"), fake_key("3")
        docs = {"a": roster("a", users={"dave": u(2000, keys=[k1]), "kim": u(2001, keys=[k2])}),
                "b": roster("b", users={"dave": u(2000, keys=[k1, k3])}),
                "old": roster("old", users={"dave": u(2000, keys=[fake_key("4")])}, ts=NOW - 30 * 86400)}
        out = f.merge_keys({"dave": 2000, "kim": 9999}, docs, NOW)
        self.assertEqual(out["dave"], [k1, k3])
        self.assertEqual(out["kim"], [])

    def test_present_on(self):
        docs = {"a": roster("a", users={"dave": u(2000)}), "b": roster("b", users={"dave": u(2000, "suspended")})}
        self.assertEqual(f.present_on(docs, "dave", 2000, NOW), ["a"])


class SshAndT3(unittest.TestCase):
    def test_ssh_config_skips_self(self):
        lines = f.ssh_config_lines({"sietchtabr": "100.64.0.10", "minastirith": "100.64.0.20"},
                                   "sietchtabr", "dave")
        self.assertIn("Host minastirith", lines)
        self.assertIn("  User dave", lines)
        self.assertNotIn("Host sietchtabr", lines)

    def test_known_hosts_from_rosters(self):
        d = f.build_roster("minastirith", "secondary", {}, 1, NOW, host_key=fake_key("host"))
        self.assertEqual(f.known_hosts_lines({"minastirith": "100.64.0.20"}, {"minastirith": d}),
                         ["minastirith,100.64.0.20 " + f.key_id(fake_key("host"))])

    def test_pair_token(self):
        self.assertEqual(f.pair_token("http://1.2.3.4:20000/pair#token=ABC123"), "ABC123")
        self.assertEqual(f.pair_token("http://1.2.3.4:20000/pair?token=XYZ"), "XYZ")
        self.assertEqual(f.pair_token("http://x/"), "")

    def test_link_due(self):
        url = "http://100.64.0.20:20000"
        later = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(NOW + 20 * 86400))
        soon = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(NOW + 2 * 86400))
        self.assertFalse(f.link_due([{"urls": [url + "/"], "expiresAt": later}], url, NOW))
        self.assertTrue(f.link_due([{"urls": [url], "expiresAt": soon}], url, NOW))
        self.assertTrue(f.link_due([{"urls": ["http://other"], "expiresAt": later}], url, NOW))
        self.assertTrue(f.link_due([], url, NOW))

    def test_link_argv(self):
        self.assertEqual(f.link_argv("http://h:1", "T")[:5], ["t3", "environment", "link", "http://h:1",
                                                              "--pairing-code"])

    def test_role_defaults_to_primary(self):
        self.assertEqual(f.role({}), "primary")
        self.assertEqual(f.role({"BOX_ROLE": "Secondary"}), "secondary")


@unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
class Signing(unittest.TestCase):
    def test_sign_verify_round_trip_and_tamper(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        key = os.path.join(d, "k")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key], check=True)
        signers = os.path.join(d, "allowed")
        with open(key + ".pub") as pub, open(signers, "w") as fh:
            fh.write(f.signer_line("sietchtabr", pub.read()) + "\n")
        w = f.wire(roster(users={"dave": u(2000)}), key)
        doc, why = f.unwire(w, signers, "sietchtabr")
        self.assertEqual((doc or {}).get("users", {}).get("dave", {}).get("uid"), 2000, why)
        self.assertIsNone(f.unwire(w, signers, "minastirith")[0])  # signed by another box's identity
        bad = dict(w, doc=w["doc"].replace("2000", "2999"))
        self.assertIsNone(f.unwire(bad, signers, "sietchtabr")[0])


if __name__ == "__main__":
    unittest.main()
