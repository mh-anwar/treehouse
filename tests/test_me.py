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
         "content": [{"type": "thinking", "thinking": "The gate probably fails on the redirect.", "signature": "x"},
                     {"type": "redacted_thinking", "data": "opaque"},
                     {"type": "text", "text": "Looking at it."},
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
        self.assertEqual([m["role"] for m in s["messages"]], ["user", "thinking", "assistant", "assistant"])
        self.assertEqual(s["messages"][1]["text"], "The gate probably fails on the redirect.")
        self.assertEqual(s["prompts"], 1)
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
        self.assertEqual(rows[0][0], 5)
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
        self.assertEqual(sbme.keep_history(self.home), [".claude/settings.json", ".codex/config.toml"])
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


LONG = "".join(f"line {i}: {'x' * 60}\n" for i in range(5000))        # ~340 KB: over the 256 KB cap


def claude_lines_v2():
    t = "2026-11-22T09:00:{:02d}.000Z"
    big = "def f():\n" + "    return 1\n" * 400
    return [
        {"type": "user", "timestamp": t.format(0), "cwd": "/home/x/q", "message": {"content": "write the module"}},
        {"type": "assistant", "timestamp": t.format(2), "message": {"model": "claude-opus-5-5", "content": [
            {"type": "thinking", "thinking": "Plan: write mod.py then run the zebra tests."},
            {"type": "tool_use", "id": "w1", "name": "Write", "input": {"file_path": "/home/x/q/mod.py", "content": big}},
            {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "ls", "description": "List"}},
            {"type": "tool_use", "id": "b2", "name": "Bash", "input": {"command": "cat big.log"}}]}},
        {"type": "user", "timestamp": t.format(3), "message": {"content": [
            {"type": "tool_result", "tool_use_id": "w1", "content": "File created"},
            {"type": "tool_result", "tool_use_id": "b1", "content": "a\nb"},
            {"type": "tool_result", "tool_use_id": "b2", "content": [{"type": "text", "text": LONG}]}]}},
    ]


def codex_reasoning_lines():
    t = "2026-11-22T12:00:{:02d}.000Z"
    return [
        {"type": "session_meta", "timestamp": t.format(0), "payload": {"id": "cx2", "cwd": "/home/x/p3"}},
        {"type": "response_item", "timestamp": t.format(1), "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "deploy it"}]}},
        {"type": "response_item", "timestamp": t.format(2), "payload": {"type": "reasoning", "encrypted_content": "gAAA",
         "summary": [{"type": "summary_text", "text": "**Checking the walrus config**"},
                     {"type": "summary_text", "text": "Then deploy."}], "content": None}},
        {"type": "event_msg", "timestamp": t.format(2), "payload": {"type": "item_completed", "item": {
            "type": "Reasoning", "summary_text": ["**Checking the walrus config**"], "raw_content": []}}},
        {"type": "response_item", "timestamp": t.format(3), "payload": {"type": "reasoning", "encrypted_content": "gAAA",
         "summary": [], "content": None}},
        {"type": "response_item", "timestamp": t.format(4), "payload": {"type": "custom_tool_call", "name": "exec",
         "call_id": "e1", "input": "x" * 3000}},
        {"type": "response_item", "timestamp": t.format(5), "payload": {"type": "custom_tool_call_output",
         "call_id": "e1", "output": LONG[:4000]}},
        {"type": "response_item", "timestamp": t.format(6), "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "Deployed."}]}},
    ]


OLD_SCHEMA = """
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE files(path TEXT PRIMARY KEY, size INTEGER, mtime REAL, session TEXT, box TEXT);
CREATE TABLE sessions(id TEXT PRIMARY KEY, box TEXT, agent TEXT, session TEXT, parent TEXT, path TEXT, cwd TEXT,
  title TEXT, model TEXT, started REAL, ended REAL, wall_s REAL, active_s REAL, idle_gaps INTEGER, prompts INTEGER,
  tool_calls INTEGER, tool_s REAL, errors INTEGER, interrupts INTEGER, compactions INTEGER, out_tokens INTEGER,
  cache_read_tokens INTEGER, updated REAL);
CREATE TABLE messages(id INTEGER PRIMARY KEY, sid TEXT NOT NULL, seq INTEGER, ts REAL, role TEXT, text TEXT);
CREATE TABLE tool_calls(id INTEGER PRIMARY KEY, sid TEXT NOT NULL, seq INTEGER, ts REAL, name TEXT, input TEXT,
  output TEXT, dur_s REAL, is_error INTEGER);
CREATE VIRTUAL TABLE messages_fts USING fts5(text, content='messages', content_rowid='id', tokenize='porter unicode61');
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text); END;
INSERT INTO meta VALUES('schema', '1');
"""


class FullHistoryTest(unittest.TestCase):
    """Thinking, reasoning summaries, full tool output, raw archive, migration, Codex config, T3 threads."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.cl = os.path.join(self.home, ".claude/projects/-home-x-q/s2.jsonl")
        self.sub = os.path.join(self.home, ".claude/projects/-home-x-q/s2/subagents/agent-a1.jsonl")
        self.cx = os.path.join(self.home, ".codex/sessions/2026/11/22/rollout-cx2.jsonl")
        write(self.cl, claude_lines_v2())
        write(self.sub, claude_lines())
        write(self.cx, codex_reasoning_lines())

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_tool_io(self):
        s = sbme.parse(self.cl)
        w, ls, cat = s["tools"]
        self.assertEqual(w["input"], "/home/x/q/mod.py")
        self.assertIn("return 1", json.loads(w["full_in"])["content"])
        self.assertIsNone(ls["full_in"])                    # only a short description more: not worth a blob
        self.assertIsNone(ls["full_out"])
        self.assertEqual(len(cat["output"]), sbme.TOOL_OUT_MAX + 1)
        self.assertEqual(cat["out_len"], len(LONG))
        self.assertEqual(cat["full_out"], LONG[:sbme.TOOL_FULL_MAX])
        sbme.ingest(self.home, "boxa")
        cols, rows = sbme.sql(self.home, "SELECT t.name, b.out_len, unz(b.output_z), unz(b.input_z) FROM tool_calls t "
                                         "JOIN tool_blobs b ON b.sid=t.sid AND b.seq=t.seq "
                                         "WHERE t.sid='boxa:claude:s2' ORDER BY t.seq")
        self.assertEqual([r[0] for r in rows], ["Write", "Bash"])
        self.assertEqual(rows[1][1], len(LONG))
        self.assertEqual(rows[1][2], LONG[:sbme.TOOL_FULL_MAX])
        self.assertIsNone(rows[1][3])
        self.assertLess(os.path.getsize(sbme.db_path(self.home)), 400_000)   # compressed, not 340 KB+ raw

    def test_codex_reasoning(self):
        s = sbme.parse(self.cx)
        self.assertEqual([m["role"] for m in s["messages"]], ["user", "thinking", "assistant"])
        self.assertEqual(s["messages"][1]["text"], "**Checking the walrus config**\n\nThen deploy.")
        self.assertEqual(s["tools"][0]["full_in"], "x" * 3000)
        # older Codex: reasoning only as event_msgs -> used, in order
        lines = [x for x in codex_reasoning_lines() if x["payload"].get("type") != "reasoning"]
        lines.insert(2, {"type": "event_msg", "timestamp": "2026-11-22T12:00:02.000Z",
                         "payload": {"type": "agent_reasoning", "text": "Old style thought"}})
        write(self.cx, lines)
        s = sbme.parse(self.cx)
        self.assertEqual([m["role"] for m in s["messages"]], ["user", "thinking", "thinking", "assistant"])
        self.assertEqual(s["messages"][1]["text"], "Old style thought")

    def test_search_leaves_thinking_out_unless_asked(self):
        sbme.ingest(self.home, "boxa")
        self.assertEqual(sbme.search(self.home, "zebra"), [])
        rows = sbme.search(self.home, "zebra", thinking=True)
        self.assertEqual([r[3] for r in rows], ["thinking"])
        self.assertEqual(len(sbme.search(self.home, "walrus", thinking=True)), 1)
        st = sbme.stats(self.home)
        self.assertEqual(st["thinking"], 3)                 # claude s2, its subagent, codex
        self.assertEqual(st["full_tool_io"], 3)             # Write input, cat output, exec input+output

    def test_raw_archive(self):
        import lzma
        r = sbme.ingest(self.home, "boxa")["raw"]
        self.assertEqual((r["files"], r["copied"]), (3, 3))
        dst = os.path.join(self.home, "me/raw/claude/projects/-home-x-q/s2.jsonl.xz")
        with lzma.open(dst, "rb") as f, open(self.cl, "rb") as g:
            self.assertEqual(f.read(), g.read())
        self.assertEqual(oct(os.stat(dst).st_mode & 0o777), "0o600")
        self.assertTrue(os.path.exists(os.path.join(self.home, "me/raw/codex/sessions/2026/11/22/rollout-cx2.jsonl.xz")))
        self.assertTrue(os.path.exists(os.path.join(self.home,
                                                    "me/raw/claude/projects/-home-x-q/s2/subagents/agent-a1.jsonl.xz")))
        self.assertEqual(sbme.archive_raw(self.home)["copied"], 0)          # unchanged: nothing copied
        write(self.cl, claude_lines_v2() + [{"type": "user", "timestamp": "2026-11-22T09:01:00.000Z",
                                             "message": {"content": "more"}}])
        self.assertEqual(sbme.archive_raw(self.home)["copied"], 1)
        with lzma.open(dst, "rb") as f:
            self.assertIn(b'"more"', f.read())
        os.remove(self.cl)                                                  # the agent deletes its transcript
        sbme.ingest(self.home, "boxa")
        self.assertTrue(os.path.exists(dst))                                # the archive keeps it
        self.assertEqual(len(sbme.search(self.home, "module")), 1)          # and so does the index
        # tool-results files are archived too; a budget already spent copies nothing
        tr = os.path.join(self.home, ".claude/projects/-home-x-q/s2/tool-results/t1.txt")
        os.makedirs(os.path.dirname(tr))
        with open(tr, "w") as f:
            f.write("saved output")
        os.utime(tr, (time.time() - 120,) * 2)
        self.assertTrue(sbme.archive_raw(self.home, deadline=time.time() - 1).get("partial"))
        self.assertEqual(sbme.archive_raw(self.home)["copied"], 1)

    def test_budget_stops_and_resumes(self):
        r = sbme.ingest(self.home, "boxa", budget=1e-9)                    # out of time at once
        self.assertEqual((r["parsed"], r.get("partial"), r["raw"].get("partial")), (0, True, True))
        r = sbme.ingest(self.home, "boxa")                                  # the next pass does the rest
        self.assertEqual((r["parsed"], r["raw"]["copied"]), (3, 3))

    def test_migration_from_old_schema(self):
        import sqlite3
        sbme.ensure_dir(self.home)
        con = sqlite3.connect(sbme.db_path(self.home))
        con.executescript(OLD_SCHEMA)
        st = os.stat(self.cl)
        sid = "boxa:claude:s2"
        con.execute("INSERT INTO files VALUES(?,?,?,?,?)", (self.cl, st.st_size, st.st_mtime, sid, "boxa"))
        con.execute("INSERT INTO sessions(id, box, agent, session, updated) VALUES(?,?,?,?,?)",
                    (sid, "boxa", "claude", "s2", 1.0))
        con.execute("INSERT INTO messages(sid,seq,ts,role,text) VALUES(?,1,1,'user','write the module')", (sid,))
        con.commit()
        con.close()
        self.assertEqual(sbme.stats(self.home)["full_tool_io"], 0)          # readonly on an old db still works
        r = sbme.ingest(self.home, "boxa")
        self.assertEqual((r["parsed"], r["skipped"], r["errors"]), (3, 0, 0))   # old file parsed again
        cols, rows = sbme.sql(self.home, "SELECT k, v FROM meta WHERE k IN ('schema', 'parser') ORDER BY k")
        self.assertEqual(rows, [("parser", str(sbme.PARSER_VERSION)), ("schema", str(sbme.SCHEMA_VERSION))])
        cols, rows = sbme.sql(self.home, f"SELECT role FROM messages WHERE sid='{sid}' ORDER BY seq")
        self.assertEqual([r[0] for r in rows], ["user", "thinking"])
        self.assertEqual(len(sbme.search(self.home, "module")), 1)          # old rows replaced, FTS clean
        self.assertEqual(sbme.ingest(self.home, "boxa")["parsed"], 0)       # once only
        con = sbme.connect(sbme.db_path(self.home))                         # reconnecting migrates nothing again
        self.assertEqual(con.execute("SELECT count(*) FROM files WHERE size=-1").fetchone()[0], 0)
        con.close()

    def test_export_import_carries_thinking_and_blobs(self):
        sbme.ingest(self.home, "boxb")
        lines = list(sbme.export_since(self.home, 0, "boxb"))
        with tempfile.TemporaryDirectory() as other:
            sbme.ensure_dir(other)
            con = sbme.connect(sbme.db_path(other))
            self.assertEqual(sbme.import_lines(con, lines), 3)
            self.assertEqual(sbme.import_lines(con, lines), 3)
            old = []                                                        # what an older box sends: no blobs
            for x in lines:
                d = json.loads(x)
                for t in d["tools"]:
                    t.pop("input_z", None), t.pop("output_z", None)
                old.append(json.dumps(d))
            con.close()
            st = sbme.stats(other)
            self.assertEqual((st["thinking"], st["full_tool_io"]), (3, 3))
            cols, rows = sbme.sql(other, "SELECT unz(output_z) FROM tool_blobs WHERE sid='boxb:claude:s2' "
                                          "AND output_z IS NOT NULL")
            self.assertEqual(rows[0][0], LONG[:sbme.TOOL_FULL_MAX])
            con = sbme.connect(sbme.db_path(other))
            self.assertEqual(sbme.import_lines(con, old), 3)
            con.close()
            self.assertEqual(sbme.stats(other)["full_tool_io"], 0)

    def test_codex_config(self):
        import tomllib
        cfg = os.path.join(self.home, ".codex/config.toml")
        user = '# mine\nmodel = "gpt-6"\n\n[projects."/home/x"]\ntrust_level = "trusted"\n'
        with open(cfg, "w") as f:
            f.write(user)
        os.chmod(cfg, 0o640)
        self.assertTrue(sbme.keep_codex_reasoning(self.home))
        with open(cfg) as f:
            text = f.read()
        self.assertTrue(text.endswith(user))                                # the user's file kept byte for byte
        d = tomllib.loads(text)
        self.assertEqual(d["model_reasoning_summary"], "detailed")
        self.assertEqual(d["projects"]["/home/x"], {"trust_level": "trusted"})   # not inside the table
        self.assertEqual(oct(os.stat(cfg).st_mode & 0o777), "0o640")
        self.assertFalse(sbme.keep_codex_reasoning(self.home))              # idempotent
        with open(cfg) as f:
            self.assertEqual(f.read(), text)
        for mine in ('model_reasoning_summary = "none"\n', "{broken toml\n"):
            with open(cfg, "w") as f:
                f.write(mine)
            self.assertFalse(sbme.keep_codex_reasoning(self.home))          # never clobbers or rewrites junk
            with open(cfg) as f:
                self.assertEqual(f.read(), mine)
        os.remove(cfg)                                                      # used Codex, no config yet
        self.assertTrue(sbme.keep_codex_reasoning(self.home))
        with open(cfg, "rb") as f:
            self.assertEqual(tomllib.load(f), {"model_reasoning_summary": "detailed"})
        with tempfile.TemporaryDirectory() as fresh:                        # never used Codex: left alone
            self.assertFalse(sbme.keep_codex_reasoning(fresh))
            self.assertFalse(os.path.exists(os.path.join(fresh, ".codex")))

    def test_guides_refresh_only_when_unedited(self):
        d = sbme.me_dir(self.home)
        os.makedirs(d)
        v1 = sbme.AGENTS_MD.replace("thinking", "")                        # not a shipped text: kept
        with open(os.path.join(d, "AGENTS.md"), "w") as f:
            f.write(v1)
        sbme.ensure_dir(self.home)
        with open(os.path.join(d, "AGENTS.md")) as f:
            self.assertEqual(f.read(), v1)
        sbme.OLD_GUIDES.add(sbme._sha(v1.encode()))                        # now it is one we shipped
        try:
            sbme.ensure_dir(self.home)
        finally:
            sbme.OLD_GUIDES.discard(sbme._sha(v1.encode()))
        with open(os.path.join(d, "AGENTS.md")) as f:
            self.assertEqual(f.read(), sbme.AGENTS_MD)
        with open(os.path.join(d, "queries.md")) as f:
            self.assertIn("unz(b.output_z)", f.read())

    def test_t3_threads(self):
        import sqlite3
        ud = os.path.join(self.home, ".t3/userdata")
        os.makedirs(ud)
        with open(os.path.join(ud, "environment-id"), "w") as f:
            f.write("env1\n")
        t3 = sqlite3.connect(os.path.join(ud, "statev2.sqlite"))
        t3.executescript("""
            CREATE TABLE orchestration_v2_projection_threads(thread_id TEXT PRIMARY KEY, project_id TEXT, title TEXT,
              default_provider TEXT, created_at TEXT, updated_at TEXT, archived_at TEXT, deleted_at TEXT,
              payload_json TEXT);
            CREATE TABLE orchestration_v2_projection_provider_threads(provider_thread_id TEXT PRIMARY KEY,
              thread_id TEXT, provider TEXT, updated_at TEXT, payload_json TEXT);
            CREATE TABLE projection_projects(project_id TEXT PRIMARY KEY, title TEXT, workspace_root TEXT);
            CREATE TABLE orchestration_v2_projection_messages(message_id TEXT PRIMARY KEY, thread_id TEXT, role TEXT,
              created_at TEXT, payload_json TEXT);
            INSERT INTO projection_projects VALUES('p1', 'Q', '/home/x/q');
            INSERT INTO orchestration_v2_projection_threads VALUES
              ('th1', 'p1', 'Write the module', 'claudeAgent', '2026-11-22T09:00:00Z', '2026-11-22T09:05:00Z',
               NULL, NULL, '{}'),
              ('th2', 'p1', 'Review screenshots', 'opencode', '2026-11-22T10:00:00Z', '2026-11-22T10:05:00Z',
               NULL, NULL, '{"worktreePath": "/home/x/q-wt"}');
            INSERT INTO orchestration_v2_projection_provider_threads VALUES
              ('pt1', 'th1', 'claudeAgent', '2026-11-22T09:05:00Z', '{"nativeThreadRef": {"nativeId": "s2"}}'),
              ('pt2', 'th2', 'opencode', '2026-11-22T10:05:00Z', '{"nativeThreadRef": {"nativeId": "ses_9"}}');
            INSERT INTO orchestration_v2_projection_messages VALUES
              ('m1', 'th2', 'user', '2026-11-22T10:00:01Z', '{"text": "look at the octopus screenshot"}'),
              ('m2', 'th2', 'assistant', '2026-11-22T10:00:09Z', '{"text": "It looks fine."}'),
              ('m3', 'th2', 'system', '2026-11-22T10:00:10Z', '{"text": "ignored"}');
        """)
        t3.commit()
        r = sbme.ingest(self.home, "boxa")["t3"]
        self.assertEqual(r, {"threads": 2, "sessions": 1})
        cols, rows = sbme.sql(self.home, "SELECT t.title, t.link, s.id FROM t3_threads t "
                                         "JOIN sessions s ON s.session=t.native_id WHERE t.provider='claudeAgent'")
        self.assertEqual(rows, [("Write the module", "t3-thread://v2/env1/th1", "boxa:claude:s2")])
        rows = sbme.search(self.home, "octopus")
        self.assertEqual([(x[2], x[4]) for x in rows], [("opencode", "Review screenshots")])
        cols, rows = sbme.sql(self.home, "SELECT cwd, prompts, wall_s FROM sessions WHERE id='boxa:opencode:th2'")
        self.assertEqual(rows, [("/home/x/q-wt", 1, 9.0)])
        self.assertEqual(sbme.ingest(self.home, "boxa")["t3"], {"threads": 0, "sessions": 0})   # incremental
        t3.execute("UPDATE orchestration_v2_projection_threads SET title='Renamed', updated_at='2026-11-22T11:00:00Z' "
                   "WHERE thread_id='th1'")
        t3.commit()
        t3.close()
        self.assertEqual(sbme.ingest(self.home, "boxa")["t3"], {"threads": 1, "sessions": 0})
        cols, rows = sbme.sql(self.home, "SELECT title FROM t3_threads WHERE thread_id='th1'")
        self.assertEqual(rows, [("Renamed",)])
        with open(os.path.join(ud, "statev2.sqlite"), "w") as f:            # a T3 we don't understand: skipped
            f.write("not sqlite")
        self.assertEqual(sbme.ingest(self.home, "boxa")["errors"], 0)


if __name__ == "__main__":
    unittest.main()
