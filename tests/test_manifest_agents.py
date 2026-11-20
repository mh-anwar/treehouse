"""TOOLS_MANIFEST agents (Antigravity): sha512-checked, atomically switched installs and their launchers.
No network: the manifest and the tarball are served from memory by a patched http_get."""
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import sbtestenv  # noqa: E402,F401

LIB = Path(__file__).resolve().parents[1] / "lib"
loader = importlib.machinery.SourceFileLoader("manifest_tools", str(LIB / "sharedbox-tools"))
tools = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(tools)

MANIFEST_URL = "https://updates.example.org/manifests/linux_{arch}.json"
TAR_URL = "https://updates.example.org/agy-{v}.tar.gz"
CFG = {"TOOLS_NPM": "", "TOOLS_MANIFEST": f"google/antigravity={MANIFEST_URL}#antigravity,agy"}


def fake_binary(version):
    return (f'#!/bin/sh\nif [ "$1" = --version ]; then echo "{version}"; exit 0; fi\n'
            f'printf "%s:%s:%s" "{version}" "$AGY_CLI_DISABLE_AUTO_UPDATE" "$1"\n').encode()


def tarball(members):
    """members: [(name, bytes or None for a symlink)] -> .tar.gz bytes"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members:
            ti = tarfile.TarInfo(name)
            if data is None:
                ti.type, ti.linkname = tarfile.SYMTYPE, "/bin/sh"
            else:
                ti.size, ti.mode = len(data), 0o755
            tf.addfile(ti, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


class ManifestAgents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.store, self.bins, state = t / "agents", t / "bin", t / "state"
        self.bins.mkdir()
        self.served = {}
        for p in (patch.object(tools.sb, "AGENTS_DIR", str(self.store)), patch.object(tools, "SHIM_DIR", str(self.bins)),
                  patch.object(tools.sb, "STATE_DIR", str(state)), patch.object(tools.sb, "USER_STATE", str(state / "us")),
                  patch.object(tools, "manifest_arch", lambda: "amd64"), patch.object(tools, "log", lambda *a: None),
                  patch.object(tools, "http_get", self.http_get)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def http_get(self, url, timeout=60):
        if url not in self.served:
            raise OSError(f"not served: {url}")
        return io.BytesIO(self.served[url])

    def publish(self, version, members=None, sha=None):
        data = tarball(members if members is not None else [("antigravity", fake_binary(version))])
        self.served[TAR_URL.format(v=version)] = data
        self.served[MANIFEST_URL.format(arch="amd64")] = json.dumps(
            {"version": version, "url": TAR_URL.format(v=version), "sha512": sha or hashlib.sha512(data).hexdigest()}
        ).encode()

    def sync(self):
        errors = []
        with patch("sys.stderr", io.StringIO()), patch("sys.stdout", io.StringIO()):
            tools.sync_agents(CFG, False, errors)
        return errors

    def run_shim(self, name):
        return subprocess.check_output([str(self.bins / name), "hello"], text=True)

    def root(self):
        return self.store / "google+antigravity"

    def test_installs_verified_tarball_and_both_launchers(self):
        self.publish("1.5.13")
        self.assertEqual(self.sync(), [])
        self.assertEqual(os.readlink(self.root() / "current"), "1.5.13")
        for name in ("antigravity", "agy"):
            self.assertEqual(self.run_shim(name), "1.5.13:true:hello")  # self-updater off, args passed through
        self.assertEqual(sorted(p.name for p in self.root().iterdir()), ["1.5.13", "current"])  # no temp dirs left
        # the next night: a new version is switched to; the old one is kept for sessions that started on it
        self.publish("1.6.0")
        self.assertEqual(self.sync(), [])
        self.assertEqual(self.run_shim("agy"), "1.6.0:true:hello")
        self.assertEqual(sorted(p.name for p in self.root().iterdir()), ["1.5.13", "1.6.0", "current"])

    def test_bad_checksum_keeps_the_old_version(self):
        self.publish("1.5.13")
        self.sync()
        self.publish("1.6.0", sha="0" * 128)
        errors = self.sync()
        self.assertTrue(any("checksum mismatch" in e for e in errors), errors)
        self.assertEqual(self.run_shim("antigravity"), "1.5.13:true:hello")
        self.assertEqual(sorted(p.name for p in self.root().iterdir()), ["1.5.13", "current"])

    def test_unexpected_archive_members_are_refused(self):
        self.publish("1.5.13")
        self.sync()
        for members in ([("antigravity", fake_binary("1.6.0")), ("extra", b"x")],
                        [("bin/antigravity", fake_binary("1.6.0"))],
                        [("../antigravity", fake_binary("1.6.0"))],
                        [("antigravity", None)],   # a symlink, not a file
                        []):
            with self.subTest(members=[m[0] for m in members]):
                self.publish("1.6.0", members=members)
                errors = self.sync()
                self.assertTrue(any("expected one file named antigravity" in e for e in errors), errors)
                self.assertEqual(self.run_shim("agy"), "1.5.13:true:hello")
                self.assertFalse((self.root() / "1.6.0").exists())

    def test_binary_that_does_not_run_is_refused(self):
        self.publish("1.5.13", members=[("antigravity", b"#!/bin/sh\nexit 3\n")])
        errors = self.sync()
        self.assertTrue(any("--version` failed" in e for e in errors), errors)
        self.assertFalse((self.bins / "agy").exists())
        self.assertFalse((self.root() / "current").exists())

    def test_bad_manifest_is_refused(self):
        for bad in ({"version": "1.6.0", "url": "http://plain.example.org/a.tgz", "sha512": "a" * 128},
                    {"version": "1.6.0; rm -rf /", "url": TAR_URL, "sha512": "a" * 128},
                    {"version": "1.6.0", "url": TAR_URL, "sha512": "abc"}, ["not", "a", "dict"]):
            with self.subTest(bad=bad):
                self.served[MANIFEST_URL.format(arch="amd64")] = json.dumps(bad).encode()
                errors = self.sync()
                self.assertTrue(any("manifest" in e for e in errors), errors)

    def test_dangling_links_are_replaced_and_stale_ones_removed(self):
        # what the 2026-11-14 cleanup left behind: hand-made links into a version that was deleted
        for name in ("antigravity", "agy"):
            (self.bins / name).symlink_to(self.root() / "1.5.9" / "antigravity")
        (self.bins / "gone").symlink_to(self.store / "other-agent" / "1.0.0" / "gone")
        (self.bins / "elsewhere").symlink_to("/nonexistent/elsewhere")   # not ours: left alone
        self.publish("1.5.13")
        self.assertEqual(self.sync(), [])
        for name in ("antigravity", "agy"):
            self.assertFalse((self.bins / name).is_symlink())
            self.assertEqual(self.run_shim(name), "1.5.13:true:hello")
        self.assertFalse(os.path.lexists(self.bins / "gone"))
        self.assertTrue(os.path.lexists(self.bins / "elsewhere"))

    def test_existing_confs_get_the_default_and_can_opt_out(self):
        cfg = tools.sb.load_config("/nonexistent/sharedbox.conf")
        self.assertIn("google/antigravity", dict(tools.agent_specs(cfg)))
        self.assertEqual(tools.MANIFEST["google/antigravity"]["bins"], ["antigravity", "agy"])
        self.assertNotIn("google/antigravity", dict(tools.agent_specs(dict(cfg, TOOLS_MANIFEST=""))))
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(tools.manifest_specs({"TOOLS_MANIFEST": "x=http://a/b#c bad =https://a#b"}), {})


if __name__ == "__main__":
    unittest.main()
