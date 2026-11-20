"""The dashboard's tabs and in-place results (lib/sharedbox-web): which tab a ?tab= shows, where a button's result
goes, the fetch answer (the page with the result in the card the button was pressed in) and the redirect without
JavaScript (same tab, #card). Stdlib only, no root, no network:
    python3 -m unittest discover -s tests -v"""

import email.parser
import importlib.machinery
import importlib.util
import io
import os
import re
import sys
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)

LIB = sbtestenv.LIB


def load_web():
    loader = importlib.machinery.SourceFileLoader("sbweb_tabs", os.path.join(LIB, "sharedbox-web"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("sbweb_tabs", loader))
    loader.exec_module(mod)
    return mod


w = load_web()


class Tabs(unittest.TestCase):
    def test_resolve_tab(self):
        for t in ("home", "messages", "servers", "account"):
            self.assertEqual(w.resolve_tab(t), t)
            self.assertEqual(w.resolve_tab(t, owner=True), t)
        for t in ("", "nope", "people", "requests", "settings", "admin", "<x>"):
            self.assertEqual(w.resolve_tab(t), "home", t)  # a friend never gets the owner's tabs
        self.assertEqual(w.resolve_tab("settings", owner=True), "settings")
        self.assertEqual(w.resolve_tab("admin", owner=True, nreq=2), "requests")  # old ?tab=admin / #admin links
        self.assertEqual(w.resolve_tab("admin", owner=True, nreq=0), "people")
        self.assertEqual(w.resolve_tab("boxes", owner=True, boxes=False), "home")

    def test_tab_bar(self):
        bar = w.tab_bar("/_box", w.page_tabs(), "servers", {"messages": (3, "")})
        self.assertEqual(bar.count("aria-current='page'"), 1)
        self.assertIn("href='/_box/me?tab=servers' data-tab='servers' class='on' aria-current='page'", bar)
        self.assertIn("Messages<span class='pill '>3</span>", bar)
        self.assertNotIn("tsep", bar)
        self.assertNotIn("tab=requests", bar)
        owner = w.tab_bar("/_box", w.page_tabs(True), "home", owner=True)
        self.assertEqual(owner.count("tsep"), 1)  # one bar: the admin tabs after a divider
        self.assertLess(owner.index("data-tab='account'"), owner.index("tsep"))
        self.assertLess(owner.index("tsep"), owner.index("data-tab='people'"))
        # old #card links: only to tabs this person has
        self.assertIn("&quot;running&quot;: &quot;servers&quot;", bar)
        self.assertNotIn("&quot;admin&quot;", bar)
        self.assertIn("&quot;admin&quot;", owner)

    def test_place_of(self):
        cases = [({"_view": "serve-logs"}, ("servers", "servers")), ({"_view": "shared-ctl"}, ("servers", "shared")),
                 ({"_view": "running-ctl"}, ("servers", "running")), ({"_view": "msg"}, ("messages", "messages")),
                 ({"_view": "where"}, ("home", "people")), ({"_view": "telegram"}, ("account", "account")),
                 ({"_view": "restore"}, ("account", "archive")), ({"_view": "add-key"}, ("account", "more")),
                 ({"_view": "login", "key": "gh"}, ("account", "account")),
                 ({"_view": "login", "key": "claude"}, ("home", "start")), ({"_view": "t3prompt"}, ("home", "start")),
                 ({"ok": False, "error": "x"}, ("home", "start")), ({"_view": "request-done"}, ("requests", "requests")),
                 ({"_view": "request-update"}, ("settings", "update")), ({"_view": "invite"}, ("people", "invite")),
                 ({"_view": "hackathon"}, ("settings", "hackathon"))]
        for res, want in cases:
            self.assertEqual(w.place_of(res), want, res)

    def test_result_place_trusts_the_page_only_with_valid_names(self):
        res = {"_view": "login", "key": "t3connect"}
        self.assertEqual(w.result_place(res, {}), ("home", "start"))
        self.assertEqual(w.result_place(res, {"_tab": "account", "_card": "account"}), ("account", "account"))
        for bad in ({"_tab": "account", "_card": "x' onload='y"}, {"_tab": "requests", "_card": "requests"},
                    {"_tab": "account"}, {"_card": "account"}, {"_tab": "account", "_card": "A"}):
            self.assertEqual(w.result_place(res, bad), ("home", "start"), bad)
        self.assertEqual(w.result_place(res, {"_tab": "requests", "_card": "requests"}, owner=True),
                         ("requests", "requests"))

    def test_redirect_url(self):
        self.assertEqual(w.redirect_url("/_box", "abc", "servers", "running"), "/_box/me?r=abc&tab=servers#running")
        self.assertEqual(w.redirect_url("", "abc", "home", "people"), "/me?r=abc&tab=home#people")
        self.assertEqual(w.redirect_url("/_box", "abc", "home", "bad card!"), "/_box/me?r=abc&tab=home")

    def test_page_script(self):
        js = w.page_script()
        self.assertIn('"X-Requested-With":"fetch"', js)
        self.assertIn('body.set("_card"', js)
        self.assertNotIn("fetch(f.action", js)  # every form has a hidden input named "action" that shadows it
        self.assertNotIn("</script>", js[:-len("</script>")])  # the failure notice can't end the script early

    def test_flash(self):
        fid = w.flash_put("u1", {"ok": True, "message": "hi"}, "servers")
        self.assertEqual(w.flash_get(fid, "u1"), ({"ok": True, "message": "hi"}, "servers"))
        self.assertEqual(w.flash_get(fid, "someone else"), (None, ""))
        self.assertEqual(w.flash_get("nope", "u1"), (None, ""))

    def test_slot(self):
        s = w.Slot("running", "<p>done</p>")
        self.assertEqual(s("servers"), "<div class='out' data-out></div>")
        self.assertEqual(s("running"), "<div class='out' data-out><p>done</p></div>")
        self.assertTrue(s.used)
        self.assertEqual(s("running"), "<div class='out' data-out></div>")  # shown once
        self.assertIn("<p>done</p>", w.tab_body("home", ["<b>x</b>"], w.Slot("gone", "<p>done</p>"), "<p>done</p>"))


class Posts(unittest.TestCase):
    """portal_post through GateHandler: a fetch from the page gets the page back with the result in the card it
    named; a plain form post gets a redirect to the same tab and card, and the GET after it shows the result there."""
    EMAIL = "friend@example.com"

    def setUp(self):
        self._orig = (w.portal_call, w.sb.user_by_email)
        self.friend = {"email": self.EMAIL, "status": "active"}
        w.sb.user_by_email = lambda e, users=None: ("sam", self.friend) if e == self.EMAIL else (None, None)

        def portal_call(ip, who, action, **kw):
            if action == "status":
                return {"ok": True, "ssh": True, "logins": {"claude": {"label": "claude", "ok": True}},
                        "t3": {"url": "http://100.64.0.5:31000/", "running": True},
                        "servers": [{"name": "blog", "state": "running", "port": "31003"}], "running": []}
            if action == "serve-logs":
                return {"ok": True, "name": kw.get("key"), "lines": ["line one", "line <b>two</b>"]}
            if action == "shared-ls":
                return {"ok": True, "servers": []}
            return {"ok": False, "error": "unexpected"}
        w.portal_call = portal_call
        w.portal_rate.clear()

    def tearDown(self):
        w.portal_call, w.sb.user_by_email = self._orig
        w.portal_rate.clear()

    def handler(self, method, path, body=b"", headers=()):
        h = object.__new__(w.GateHandler)
        raw = f"X-Sharedbox-Cf-Email: {self.EMAIL}\r\nX-Sharedbox-Cf-Jwt: jwt\r\n" + "".join(
            f"{k}: {v}\r\n" for k, v in headers)
        if method == "POST":
            raw += f"Content-Length: {len(body)}\r\n"
        h.headers = email.parser.Parser().parsestr(raw + "\r\n")
        h.path, h.rfile = path, io.BytesIO(body)
        out = {}

        def send(code, data, ctype="text/html; charset=utf-8", headers=()):
            out.update(code=code, body=data, headers=dict(headers))
        h.send = send
        getattr(h, "do_" + method)()
        return out

    def post(self, fields, headers=()):
        who = w.cf_who(self.EMAIL, "jwt")
        body = urllib.parse.urlencode(dict(fields, token=w.token_for("gate", who["id"]))).encode()
        return self.handler("POST", "/_box/me", body, headers)

    @staticmethod
    def card(page, cid):
        """The HTML of the card with this data-card, up to the next card."""
        i = page.index(f"data-card='{cid}'")
        j = page.find("data-card=", i + 10)
        return page[i:j if j > 0 else None]

    def test_fetch_answers_with_the_card(self):
        r = self.post({"action": "serve-logs", "key": "blog", "_tab": "servers", "_card": "servers"},
                      (("X-Requested-With", "fetch"),))
        self.assertEqual(r["code"], 200)
        self.assertEqual(r["headers"].get("X-Treehouse-Card"), "servers")
        card = self.card(r["body"], "servers")
        self.assertIn("<pre class=\"logs\"", card)
        self.assertIn("line &lt;b&gt;two&lt;/b&gt;", card)
        self.assertIn("data-tab='servers' class='on'", r["body"])
        self.assertEqual(r["body"].count("pre class=\"logs\""), 1)

    def test_plain_post_redirects_to_the_same_tab_and_card(self):
        r = self.post({"action": "serve-logs", "key": "blog"})
        self.assertEqual(r["code"], 303)
        loc = r["headers"]["Location"]
        m = re.fullmatch(r"/_box/me\?r=([\w-]+)&tab=servers#servers", loc)
        self.assertTrue(m, loc)
        g = self.handler("GET", loc.split("#")[0])
        self.assertEqual(g["code"], 200)
        self.assertIn("<pre class=\"logs\"", self.card(g["body"], "servers"))

    def test_expired_form_in_the_card(self):
        who = w.cf_who(self.EMAIL, "jwt")
        assert who
        body = urllib.parse.urlencode({"token": "wrong", "action": "serve-logs", "key": "blog", "_tab": "servers",
                                       "_card": "servers"}).encode()
        r = self.handler("POST", "/_box/me", body, (("X-Requested-With", "fetch"),))
        self.assertEqual(r["code"], 400)
        self.assertIn("Form expired", self.card(r["body"], "servers"))

    def test_unknown_action_without_js(self):
        r = self.post({"action": "nope"})
        self.assertEqual(r["code"], 303)
        self.assertTrue(r["headers"]["Location"].endswith("&tab=home#start"), r["headers"])


if __name__ == "__main__":
    unittest.main()
