import ast
import argparse
import fcntl
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import sbclean
import sbteam


class MultiboxNext(unittest.TestCase):
    def test_mirrors(self):
        with tempfile.TemporaryDirectory() as home:
            base = Path(home) / ".sharedbox/mirror"
            base.mkdir(parents=True)
            now = time.time()
            for name in ("old", "busy", "recent"):
                p = base / name
                p.mkdir()
                os.utime(p, (now - 9 * 86400,) * 2)
                lock = base / (name + ".lock")
                lock.touch()
                os.utime(lock, (now - (0 if name == "recent" else 9 * 86400),) * 2)
            with (base / "busy.lock").open("r+") as lock:
                fcntl.flock(lock, fcntl.LOCK_SH)
                rows = sbclean.clean_mirrors(home, now)
            self.assertEqual([Path(r["path"]).name for r in rows], ["old"])
            self.assertTrue((base / "busy").exists())
            self.assertTrue((base / "recent").exists())

    def test_forward(self):
        with patch("sbcommon.read_json", return_value={"home": "other"}), \
                patch("sbteam.box_name", return_value="here"), \
                patch("sbcommon.boxes", return_value={"other": "100.64.0.2"}), \
                patch("sbmulti.ssh_dest", return_value="alice@other"), \
                patch("os.execvp") as execute:
            sbteam.forward_home("test", {}, ["post", "hello world"])
            execute.assert_called_once_with("ssh", ["ssh", "alice@other", "box", "team", "post", "'hello world'"])

    def test_heavy_executes_without_survey(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "lib/box").read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "cmd_heavy")
        namespace = {"os": os, "sys": sys}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "box", "exec"), namespace)
        with patch("os.execvp") as execute, patch("sbmulti.survey") as survey:
            namespace["cmd_heavy"](argparse.Namespace(cmd=["--", "echo", "hello"]))
            execute.assert_called_once_with("echo", ["echo", "hello"])
            survey.assert_not_called()
