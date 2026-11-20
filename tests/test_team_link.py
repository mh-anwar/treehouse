"""Teammates' agents in your T3: the pure helpers in sbconnect (labels, pairing URLs, T3's JSON)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import sbconnect  # noqa: E402


class TeamLink(unittest.TestCase):
    def test_split_pair_url(self):
        self.assertEqual(sbconnect.split_pair_url("http://100.64.0.5:42000/pair#token=abc123"),
                         ("http://100.64.0.5:42000", "abc123"))
        for bad in ("", "http://100.64.0.5:42000/pair", "ftp://x/pair#token=a", "#token=a"):
            self.assertEqual(sbconnect.split_pair_url(bad), ("", ""), bad)

    def test_grants_only_team_sessions_not_revoked(self):
        sessions = [
            {"sessionId": "s1", "client": {"label": sbconnect.team_label("bob")}, "issuedAt": "2026-11-19T10:00:00Z",
             "lastConnectedAt": "2026-11-20T01:00:00Z"},
            {"sessionId": "s2", "client": {"label": "alice T3 app"}},
            {"sessionId": "s3", "client": {"label": sbconnect.team_label("carol")}, "revokedAt": "2026-11-19"},
            {"client": {"label": sbconnect.team_label("dave")}},  # no id: can't be revoked, so not listed
            "junk",
        ]
        self.assertEqual(sbconnect.team_grants(sessions),
                         [{"to": "bob", "session": "s1", "since": "2026-11-19", "last": "2026-11-20"}])
        self.assertEqual(sbconnect.team_grants(None), [])

    def test_links_to_origin(self):
        envs = [{"environmentId": "e1", "urls": ["http://100.64.0.5:42000/"]},
                {"environmentId": "e2", "urls": ["https://relay.example"]},
                {"urls": ["http://100.64.0.5:42000"]}]
        self.assertEqual(sbconnect.team_links_to(envs, "http://100.64.0.5:42000"), ["e1"])
        self.assertEqual(sbconnect.team_links_to({"not": "a list"}, "x"), [])

    def test_access_levels_are_t3s(self):  # `t3 environment link --access` choices
        self.assertLessEqual(set(sbconnect.TEAM_ACCESS),
                             {"read-only", "approval-required", "auto-accept-edits", "auto", "full-access"})


if __name__ == "__main__":
    unittest.main()
