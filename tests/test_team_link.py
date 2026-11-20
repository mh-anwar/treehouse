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


class ScreenUrls(unittest.TestCase):
    def test_hard_wrapped_link_is_joined(self):
        first = "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c&response_type=code&redirect_uri=" \
                "https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&scope=user%3Ainference&code_challenge=AK"
        screen = f" Browser didn't open? Use the URL below\n{first}\n70gb6&code_challenge_method=S256&state=xyz\n\n" \
                 " Paste code here if prompted\n"
        self.assertEqual(sbconnect.screen_urls(screen), [first + "70gb6&code_challenge_method=S256&state=xyz"])

    def test_short_link_and_device_code_untouched(self):
        screen = "Sign in to T3 Connect. Open on any device:\n  https://accounts.t3.codes/device?user_code=GFXD-KQWN\n" \
                 "Code: GFXD-KQWN\nWaiting\n"
        self.assertEqual(sbconnect.screen_urls(screen), ["https://accounts.t3.codes/device?user_code=GFXD-KQWN"])

    def test_indented_link_wrapped_with_the_same_indent_is_joined(self):
        part1 = " https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=1071006060591-x.apps." \
                "googleusercontent.com&code_challenge=AbC"
        screen = f" Your browser should open automatically. If not:\n{part1}\n dEf&prompt=consent&state=xyz\n\n" \
                 " If you aren't automatically redirected, paste the authorization code below:\n"
        self.assertEqual(sbconnect.screen_urls(screen), [part1.strip() + "dEf&prompt=consent&state=xyz"])
        # `antigravity acp --login`: tmux's own wrap, joined by capture-pane -J into one indented line
        long = "https://accounts.google.com/o/oauth2/auth?" + "&".join(f"k{i}=v{i}" for i in range(120))
        self.assertEqual(sbconnect.screen_urls(f"Open this link in a browser to log in with Google:\n\n  {long}\n\n"
                                               "Then paste the authorization code, or the full URL\n"), [long])
        # a differently indented line after a long link is not part of it
        self.assertEqual(sbconnect.screen_urls(f"{part1}\n   Waiting\n"), [part1.strip()])

    def test_link_mid_sentence(self):
        self.assertEqual(sbconnect.screen_urls("open https://github.com/login/device and type the code."),
                         ["https://github.com/login/device"])
