"""Tests for lib/sbme.py (~/me/agents.db). Fake Claude Code and Codex transcripts in a temp home; no network, no root:
    python3 -m unittest tests.test_me -v"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
import sbme  # noqa: E402


def claude_lines(sid="s1"):
    t = "2026-11-21T10:00:{:02d}.000Z"
    return [
        {"type": "custom-title", "customTitle": "Fix the gate"},
        {"type": "user", "timestamp": t.format(0), "cwd": "/home/x/proj", "message": {"content": "Please fix the gate"}},
        {"type": "assistant", "timestamp": t.format(5), "message": {"model": "claude-opus-5-5",
         "usage": {"output_tokens": 40, "cache_read_input_tokens": 1000},
         "content": [{"type": "text", "text": "Looking at it."},
                     {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "sleep 3; ls"}}]}},
        {"type": "user", "timestamp": t.format(9), "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu1", "is_error": True, "content": "Exit code 2\nnope"}]}},
        {"type": "user", "timestamp": t.format(10), "isMeta": True, "message": {"content": "<system>meta</system>"}},
        {"type": "assistant", "timestamp": t.format(20), "message": {"content": [{"type": "text", "text": "Done."}]}},
    ]


def codex_lines():
    t = "2026-11-21T11:00:{:02d}.000Z"
    return [
        {"type": "session_meta", "timestamp": t.format(0), "payload": {"id": "cx1", "cwd": "/home/x/p2"}},
        {"type": "turn_context", "timestamp": t.format(0), "payload": {"model": "gpt-6"}},
        {"type": "response_item", "timestamp": t.format(1), "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "count the beads"}]}},
        {"type": "response_item", "timestamp": t.format(2), "payload": {"type": "function_call", "name": "exec_command",
         "call_id": "c1", "arguments": json.dumps({"cmd": "bd list | wc -l"})}},
        {"type": "response_item", "timestamp": t.format(4), "payload": {"type": "function_call_output", "call_id": "c1",
         "output": "Process exited with code 0\n42"}},
        {"type": "response_item", "timestamp": t.format(6), "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "There are 42 beads."}]}},
    ]


def write(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for x in lines:
            f.write(json.dumps(x) + "\n")
    old = time.time() - 120          # ingest skips files written in the last 20 s
    os.utime(path, (old, old))


class MeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.cl = os.path.join(self.home, ".claude/projects/-home-x-proj/s1.jsonl")
        self.cx = os.path.join(self.home, ".codex/sessions/2026/11/21/rollout-cx1.jsonl")
        write(self.cl, claude_lines())
        write(self.cx, codex_lines())

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_claude(self):
        s = sbme.parse(self.cl)
        self.assertEqual(s["title"], "Fix the gate")
        self.assertEqual(s["model"], "claude-opus-5-5")
        self.assertEqual([m["role"] for m in s["messages"]], ["user", "assistant", "assistant"])
        self.assertEqual(s["tool_calls"], 1)
        self.assertEqual(s["errors"], 1)
        self.assertEqual(s["tools"][0]["dur_s"], 4.0)
        self.assertEqual(s["wall_s"], 20.0)
        self.assertEqual(s["out_tokens"], 40)

    def test_parse_codex(self):
        s = sbme.parse(self.cx)
        self.assertEqual((s["agent"], s["session"], s["model"]), ("codex", "cx1", "gpt-6"))
        self.assertEqual(s["tools"][0]["input"], "bd list | wc -l")
        self.assertEqual(s["errors"], 0)
        self.assertEqual(s["prompts"], 1)

    def test_ingest_incremental_search_sql(self):
        r = sbme.ingest(self.home, "boxa")
        self.assertEqual((r["parsed"], r["errors"]), (2, 0))
        self.assertEqual(oct(os.stat(sbme.me_dir(self.home)).st_mode & 0o777), "0o700")
        self.assertEqual(oct(os.stat(sbme.db_path(self.home)).st_mode & 0o777), "0o600")
        self.assertTrue(os.path.exists(os.path.join(self.home, "me", "AGENTS.md")))
        self.assertEqual(sbme.ingest(self.home, "boxa")["parsed"], 0)      # unchanged: skipped
        rows = sbme.search(self.home, "gate")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "boxa")
        # the file grows: parsed again, old rows replaced (no duplicates in the index)
        write(self.cl, claude_lines() + [{"type": "user", "timestamp": "2026-11-21T10:01:00.000Z",
                                          "message": {"content": "and the gate tests"}}])
        self.assertEqual(sbme.ingest(self.home, "boxa")["parsed"], 1)
        self.assertEqual(len(sbme.search(self.home, "gate")), 2)
        cols, rows = sbme.sql(self.home, "SELECT count(*) FROM messages WHERE sid='boxa:claude:s1'")
        self.assertEqual(rows[0][0], 4)
        with self.assertRaises(Exception):
            sbme.sql(self.home, "DELETE FROM messages")                 # read-only

    def test_export_import_between_boxes(self):
        sbme.ingest(self.home, "boxb")
        lines = list(sbme.export_since(self.home, 0, "boxb"))
        self.assertEqual(len(lines), 2)
        with tempfile.TemporaryDirectory() as other:
            sbme.ensure_dir(other)
            con = sbme.connect(sbme.db_path(other))
            self.assertEqual(sbme.import_lines(con, lines), 2)
            self.assertEqual(sbme.import_lines(con, lines), 2)          # idempotent
            con.close()
            st = sbme.stats(other)
            self.assertEqual((st["sessions"], st["messages"]), (2, 5))
            self.assertEqual(len(sbme.search(other, "beads")), 2)

    def test_keep_history(self):
        p = os.path.join(self.home, ".claude/settings.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump({"model": "opus", "cleanupPeriodDays": 30}, f)
        self.assertEqual(sbme.keep_history(self.home), [".claude/settings.json"])
        with open(p) as f:
            d = json.load(f)
        self.assertEqual(d, {"model": "opus", "cleanupPeriodDays": sbme.KEEP_DAYS})
        self.assertEqual(sbme.keep_history(self.home), [])               # already forever
        with open(p, "w") as f:
            f.write("{not json")
        self.assertEqual(sbme.keep_history(self.home), [])               # never rewrites what it can't parse

    def test_due(self):
        self.assertTrue(sbme.due(self.home))
        sbme.ingest(self.home, "boxa")
        self.assertTrue(sbme.due(self.home))
        self.assertFalse(sbme.due(self.home))


if __name__ == "__main__":
    unittest.main()
