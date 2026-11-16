"""Every agent launcher follows the atomic current link, independent of user preferences."""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import sbtestenv  # noqa: E402,F401

LIB = Path(__file__).resolve().parents[1] / "lib"
loader = importlib.machinery.SourceFileLoader("launcher_tools", str(LIB / "sharedbox-tools"))
tools = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(tools)


class AgentLaunchers(unittest.TestCase):
    def test_launchers_follow_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, bins, home = (Path(tmp) / name for name in ("agents", "bin", "home"))
            bins.mkdir()
            (home / ".sharedbox").mkdir(parents=True)
            (home / ".sharedbox/agent-updates").write_text("manual\n")
            with patch.object(tools.sb, "AGENTS_DIR", str(store)), patch.object(tools, "SHIM_DIR", str(bins)):
                for package, binary in (("@anthropic-ai/claude-code", "claude"), ("@openai/codex", "codex"),
                                        ("@opencode/cli", "opencode"), ("t3", "t3")):
                    root = Path(tools.agent_root(package))
                    for version in ("1.0.0", "2.0.0"):
                        target = root / version / "node_modules/.bin" / binary
                        target.parent.mkdir(parents=True)
                        target.write_text(f'#!/bin/sh\nprintf "%s:%s:%s:%s" "{version}" "$1" '
                                          '"$DISABLE_AUTOUPDATER" "$OPENCODE_DISABLE_AUTOUPDATE"\n')
                        target.chmod(0o755)
                    current = root / "current"
                    current.symlink_to("1.0.0")
                    tools.write_shim({}, package, binary)
                    for version in ("1.0.0", "2.0.0"):
                        current.unlink()
                        current.symlink_to(version)
                        result = subprocess.check_output([str(bins / binary), "argument with spaces"],
                                                         env=dict(os.environ, HOME=str(home)), text=True)
                        self.assertEqual(result, f"{version}:argument with spaces:1:1")

    def test_pin_cleanup_preserves_request_records_and_home_preferences(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, home = Path(tmp) / "state", Path(tmp) / "home"
            old = state / "agent-pins/alice"
            old.mkdir(parents=True)
            (old / "requests.json").write_text('{"requests": []}')
            (old / "notices.json").write_text('[]')
            (old / "claude-code").symlink_to("/unused/version")
            (home / ".sharedbox").mkdir(parents=True)
            preference = home / ".sharedbox/agent-updates"
            preference.write_text("manual\n")
            with patch.object(tools.sb, "STATE_DIR", str(state)), \
                    patch.object(tools.sb, "USER_STATE", str(state / "user-state")):
                tools.sb.migrate_user_state()
                tools.sb.migrate_user_state()  # idempotent
            self.assertFalse((state / "agent-pins").exists())
            self.assertEqual((state / "user-state/alice/requests.json").read_text(), '{"requests": []}')
            self.assertEqual((state / "user-state/alice/notices.json").read_text(), '[]')
            self.assertEqual(preference.read_text(), "manual\n")
