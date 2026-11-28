"""The weekly health report and hourly alerts never fail because email isn't set up: they go to the owner's inbox
(box msg, pushed by the Telegram bot), else only to the log, and the unit exits 0.
    python3 -m unittest tests.test_health_fallback -v"""

import contextlib
import functools
import importlib.machinery
import importlib.util
import io
import json
import os
import pwd
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
import sbmsg  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("sbhealth_fb", os.path.join(sbtestenv.LIB, "sharedbox-health"))
health = importlib.util.module_from_spec(importlib.util.spec_from_loader("sbhealth_fb", _loader))
_loader.exec_module(health)

NO_MAIL = {"MAIL_TO": "", "SMTP_HOST": "smtp.gmail.com", "SMTP_PASSWORD_FILE": "/nonexistent"}


class Fallback(unittest.TestCase):
    def setUp(self):
        self.mail = tempfile.mkdtemp(prefix="sbmail-")
        self.me = pwd.getpwuid(os.getuid())
        self.patches = [
            mock.patch.object(sbmsg, "deliver_system",
                              functools.partial(sbmsg.deliver_system, root=self.mail, as_root=False)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.mail, ignore_errors=True)

    def inbox(self):
        d = os.path.join(self.mail, self.me.pw_name)
        out = []
        for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if n.endswith(".json"):
                with open(os.path.join(d, n)) as f:
                    out.append(json.load(f))
        return out

    def run_deliver(self, cfg, owner, **kw):
        out = io.StringIO()
        with mock.patch.object(health.sb, "owner_account", return_value=owner), contextlib.redirect_stdout(out):
            where = health.deliver(cfg, "🟡 weekly health: needs a look (1 issue)", "== Verdict ==\nthe text",
                                   "<p>page</p>", **kw)
        return where, out.getvalue()

    def test_no_email_goes_to_owner_inbox(self):
        with mock.patch.object(health.sbmail, "send") as send:
            where, out = self.run_deliver(NO_MAIL, self.me, level="high", summary="- disk getting full")
        send.assert_not_called()
        self.assertEqual(where, "inbox")
        msgs = self.inbox()
        self.assertEqual(len(msgs), 1)
        m = msgs[0]
        self.assertEqual(m["from"], sbmsg.SYSTEM_SENDER)
        self.assertEqual(m["level"], "high")
        self.assertIn("weekly health: needs a look", m["title"])
        self.assertNotIn("🟡", m["title"])
        self.assertIn("disk getting full", m["brief"]["context"])
        self.assertIn("email isn't set up", m["brief"]["context"])
        self.assertIn("the text", m["brief"]["report"])
        self.assertIn("the text", out)  # and in the log

    def test_no_owner_goes_to_log_only(self):
        where, out = self.run_deliver(NO_MAIL, None)
        self.assertEqual(where, "log")
        self.assertEqual(self.inbox(), [])
        self.assertIn("written to the log only", out)
        self.assertIn("the text", out)

    def test_inbox_failure_falls_back_to_log(self):
        with mock.patch.object(sbmsg, "deliver_system", side_effect=sbmsg.MsgError("broken")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                where, out = self.run_deliver(NO_MAIL, self.me)
        self.assertEqual(where, "log")
        self.assertIn("the text", out)

    def test_configured_email_is_used(self):
        with mock.patch.object(health.sbmail, "configured", return_value=True), \
                mock.patch.object(health.sbmail, "send") as send:
            where, _ = self.run_deliver(NO_MAIL, self.me)
        self.assertEqual(where, "email")
        send.assert_called_once()
        self.assertEqual(self.inbox(), [])

    def test_smtp_error_still_reaches_owner(self):
        with mock.patch.object(health.sbmail, "configured", return_value=True), \
                mock.patch.object(health.sbmail, "send", side_effect=OSError("connection refused")):
            where, _ = self.run_deliver(NO_MAIL, self.me)
        self.assertEqual(where, "inbox")
        self.assertIn("email failed", self.inbox()[0]["brief"]["context"])

    def test_same_report_twice_in_an_hour_bumps(self):
        self.run_deliver(NO_MAIL, self.me)
        self.run_deliver(NO_MAIL, self.me)
        msgs = self.inbox()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["count"], 2)

    def test_long_report_is_cut_to_fit(self):
        out = io.StringIO()
        with mock.patch.object(health.sb, "owner_account", return_value=self.me), contextlib.redirect_stdout(out):
            where = health.deliver(NO_MAIL, "weekly health: all good", "x" * 20000)
        self.assertEqual(where, "inbox")
        self.assertLessEqual(len(self.inbox()[0]["brief"]["report"]), sbmsg.TEXT_MAX)

    def test_report_send_exits_zero_without_email(self):
        """cmd_report --send end to end with the slow parts stubbed: returns normally (exit 0)."""
        a = mock.Mock(send=True)
        audit = mock.Mock()
        audit.run_all.return_value = []
        audit.save.return_value = ([], [])
        with mock.patch.object(health.sbaudit, "Audit", return_value=audit), \
                mock.patch.object(health, "gather", return_value={}), \
                mock.patch.object(health, "build_report", return_value=("🟢 weekly health: all good", "t", "<p>")), \
                mock.patch.object(health, "problems", return_value=[]), \
                mock.patch.object(health.sb, "owner_account", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            health.cmd_report(NO_MAIL, a)


if __name__ == "__main__":
    unittest.main()
