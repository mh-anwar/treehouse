"""One vocabulary and one number per fact on the dashboard (lib/sharedbox-web, sbcommon.GLOSSARY):
the glossary doc matches the code, sizes read "9.6 GB", the machine has one name on every part of the page, the
whole-machine line uses the Machines tab's numbers. Stdlib only:  python3 -m unittest discover -s tests -v"""

import importlib.machinery
import importlib.util
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
import glossary_doc  # noqa: E402

LIB = sbtestenv.LIB
sys.path.insert(0, LIB)
import sbcommon as sb  # noqa: E402


def load_web():
    loader = importlib.machinery.SourceFileLoader("sbweb_vocab", os.path.join(LIB, "sharedbox-web"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("sbweb_vocab", loader))
    loader.exec_module(mod)
    return mod


w = load_web()


class Glossary(unittest.TestCase):
    def test_doc_matches_code(self):
        with open(os.path.join(glossary_doc.ROOT, "docs", "glossary.md")) as f:
            self.assertEqual(f.read(), glossary_doc.render(), "run: python3 tests/glossary_doc.py")

    def test_home_shows_every_word(self):
        g = w.glossary_section()
        for term, _ in sb.GLOSSARY:
            self.assertIn(f"<dt>{term}</dt>", g)
        self.assertNotIn("{machine}", g)


class Sizes(unittest.TestCase):
    def test_units(self):
        G, M = 1 << 30, 1 << 20
        cases = {0: "0 MB", 600 * M: "600 MB", int(9.6 * G): "9.6 GB", 40 * G: "40 GB", int(491.8 * G): "492 GB",
                 int(1.4 * 1024 * G): "1.4 TB"}
        for n, want in cases.items():
            self.assertEqual(sb.size_text(n), want)

    def test_web_never_uses_the_short_units(self):
        with open(os.path.join(LIB, "sharedbox-web")) as f:
            self.assertNotIn("sb.human", f.read())

    def test_cores(self):
        self.assertEqual(w.fmt_cores(0.001), "idle")
        self.assertEqual(w.fmt_cores(0.4, 8), "0.4 of 8 cores")
        self.assertEqual(w.fmt_cores(2.0, 8), "2 of 8 cores")


class OneName(unittest.TestCase):
    def test_header_account_and_machines_agree(self):
        old = (w.SELF_IP, list(w.BOX_LIST))
        try:
            w.SELF_IP = "100.64.0.9"
            w.BOX_LIST[:] = [("treebox", "100.64.0.9"), ("other", "100.64.0.8")]
            self.assertEqual(w.here_name(), "treebox")
            page = w.page("My dashboard", "", gate=True)
            self.assertIn("· treebox</span>", page)
            acct = w.ai_accounts_section({"box": "mybox", "boxes": {}}, {"claude": {"ok": True}},
                                         w.portal_btn("t", ""), "t", "")
            self.assertIn("treebox", acct)
            self.assertNotIn("mybox", acct)
        finally:
            w.SELF_IP, w.BOX_LIST[:] = old[0], old[1]

    def test_nav_matches_tab_names(self):
        page = w.page("My dashboard", "", gate=True)
        self.assertIn(">Home</a>", page)
        self.assertNotIn(">Dashboard</a>", page)


class Events(unittest.TestCase):
    def test_box_is_never_an_actor(self):
        self.assertIn("The owner", w.event_text({"kind": "invite_created", "user": "box", "email": "b@x.io"}))
        txt = w.event_text({"kind": "tools_updated", "user": "box"})
        self.assertNotIn(">box<", txt)


if __name__ == "__main__":
    unittest.main()
