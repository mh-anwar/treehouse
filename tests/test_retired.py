"""Regression coverage for destructive migration cleanup, using only temporary files."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import sbcommon as sb
import sbretired


class Retired(unittest.TestCase):
    def test_only_recorded_unchanged_regular_files_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            decoys = base / "decoys"
            decoys.mkdir()
            conf = base / "conf"
            conf.write_text(f'CANARY_DIR="{decoys}"\n')
            contents = {".env": b"fake", "README.txt": b"original", "aws-credentials": b"key"}
            planted = {n: hashlib.sha256(data).hexdigest() for n, data in contents.items()}
            for name, data in contents.items():
                (decoys / name).write_bytes(data)
            (decoys / "README.txt").write_text("user replacement")
            (decoys / "id_ed25519_deploy").write_text("unrecorded key")
            (decoys / "real-backup.sql").write_text("real backup")
            (decoys / "aws-credentials").unlink()
            (decoys / "aws-credentials").symlink_to(decoys / "real-backup.sql")
            outside = base / "outside"
            outside.write_bytes(b"fake")
            planted["../outside"] = planted[".env"]
            (base / "canary-planted.json").write_text(json.dumps(planted))
            for _ in range(2):
                sbretired.cleanup_canary(str(conf), str(base))
            self.assertFalse((decoys / ".env").exists())
            self.assertEqual((decoys / "README.txt").read_text(), "user replacement")
            self.assertTrue((decoys / "aws-credentials").is_symlink())
            self.assertEqual((decoys / "real-backup.sql").read_text(), "real backup")
            self.assertTrue((decoys / "id_ed25519_deploy").exists())
            self.assertTrue(outside.exists())

    def test_missing_or_invalid_manifest_keeps_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for data in (None, "broken", "[]"):
                if data is not None:
                    (base / "canary-planted.json").write_text(data)
                sbretired.cleanup_canary(str(base / "conf"), tmp)

    def test_retired_config_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "conf"
            conf.write_text("TRAP_ENABLED=1\nCANARY_ACTION=freeze\nCANARY_DIR=/example\nTOOLS_NPM=example-tool\nBRAND=Example\n")
            cfg = sb.load_config(str(conf))
            self.assertEqual(cfg["BRAND"], "Example")
            self.assertEqual(cfg["TOOLS_NPM"], "example-tool")
            self.assertFalse(any(k.startswith(("TRAP_", "CANARY_")) for k in cfg))

    def test_stale_janitor_fields_removed_without_losing_budget_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "janitor-state.json"
            path.write_text(json.dumps({"alice": {"t3_busy": 123, "t3_other_cpu": 5, "over_since": 456}}))
            for _ in range(2):
                sbretired.cleanup_janitor(tmp)
            self.assertEqual(json.loads(path.read_text()), {"alice": {"over_since": 456}})
