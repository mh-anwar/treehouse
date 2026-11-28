"""`~/me`: each account's own searchable history of every agent session (prompts, replies, reasoning, tool calls)
plus per-session efficiency numbers, in one SQLite file: ~/me/agents.db, and an xz-compressed copy of the raw
transcripts in ~/me/raw/.

Why: Claude Code deletes its transcripts after 30 days (unless cleanupPeriodDays is raised), Codex and the others
keep theirs in different formats, and nobody can ask "why was my agent slow yesterday", "find the prompt where I
explained X" or "what was the agent thinking when it did Y" across all of them. This module reads the transcripts the
agents already write and keeps an indexed copy forever, plus an xz copy of every raw file so nothing is lost even when an
agent deletes its own transcripts.

Sources (read-only):
- Claude Code: ~/.claude/projects/*/*.jsonl, their subagents/*.jsonl (and tool-results/ files, raw archive only)
- Codex: ~/.codex/sessions/YYYY/MM/DD/*.jsonl and ~/.codex/archived_sessions/*.jsonl
- T3 Code: ~/.t3/userdata/statev2.sqlite: every thread's title, provider and link (table t3_threads), and the
  messages of threads run by providers that have no transcript here (OpenCode, Antigravity, ...).

What is stored (agents.db):
- messages: user prompts and assistant replies in full; the model's reasoning as role 'thinking' (Claude Code
  thinking blocks in full, Codex reasoning summaries; Codex keeps raw reasoning encrypted, so keep_history() turns
  on readable summaries). All of it is in the full-text index; `box me search` leaves thinking out unless asked.
- tool_calls: name, a readable input (first 2,000 characters of the command / path / query), the output's first
  500 characters (full-text searchable through SQL LIKE), how long it took, whether it failed.
- tool_blobs: the full tool input (JSON) and full output, zlib-compressed, up to 256 KB each before compression,
  only when they don't fit in tool_calls already. `box me sql` has unz(blob) to read them.
- sessions: box, agent, model, folder, title, start/end, active time, counts, tokens.
- raw/: xz copies of every transcript file at its path relative to the home folder (raw/claude/projects/...,
  raw/codex/sessions/...), refreshed when the source's size or mtime changes, never deleted when the source goes.

Runs AS THE ACCOUNT (the janitor calls `box _me-tick` at the lowest priority every ~15 minutes; `box me ingest` by
hand). Incremental: a file is parsed and archived again only when its size or mtime changed; PARSER_VERSION bumps
re-parse every file once (the old rows stay until the new ones replace them, and a pass that runs out of time
continues on the next one). Other boxes: `box me pull` copies the sessions the account's other boxes indexed (over
the account's own fed SSH login) so one agents.db holds every box. Stdlib only.
"""

import base64
import lzma
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import tomllib
import zlib
from datetime import datetime

ME_DIR = "me"
DB_NAME = "agents.db"
RAW_DIR = "raw"
RAW_PRESET = 1            # xz -1: ~3.7x on real transcripts (gzip: ~2.4x; repeated base64 screenshots sit further
                          # apart than gzip's 32 KB window), ~15 MB/s on one core
SCHEMA_VERSION = 2
PARSER_VERSION = 2        # bump when parsing changes: every file is parsed again once
TOOL_IN_MAX = 2000
TOOL_OUT_MAX = 500
TOOL_FULL_MAX = 256 * 1024  # full tool input/output kept (compressed) up to this many characters
FULL_IN_SLACK = 100       # store the full input only when it adds more than this over the readable one
ACTIVE_GAP = 300          # gaps shorter than this count as active time
FRESH_S = 20              # files written in the last FRESH_S seconds wait for the next pass
NICE = ("nice", "-n", "15", "ionice", "-c", "3")
T3_DB = os.path.join(".t3", "userdata", "statev2.sqlite")
T3_COVERED = {"claudeAgent": "claude", "codex": "codex"}  # providers whose own transcripts we already read

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INTEGER, mtime REAL, session TEXT, box TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY,          -- box:agent:session-id
  box TEXT, agent TEXT, session TEXT, parent TEXT, path TEXT, cwd TEXT, title TEXT, model TEXT,
  started REAL, ended REAL, wall_s REAL, active_s REAL, idle_gaps INTEGER,
  prompts INTEGER, tool_calls INTEGER, tool_s REAL, errors INTEGER, interrupts INTEGER, compactions INTEGER,
  out_tokens INTEGER, cache_read_tokens INTEGER, updated REAL);
CREATE INDEX IF NOT EXISTS sessions_started ON sessions(started);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, sid TEXT NOT NULL, seq INTEGER, ts REAL, role TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS messages_sid ON messages(sid);
CREATE TABLE IF NOT EXISTS tool_calls(
  id INTEGER PRIMARY KEY, sid TEXT NOT NULL, seq INTEGER, ts REAL, name TEXT, input TEXT, output TEXT,
  dur_s REAL, is_error INTEGER);
CREATE INDEX IF NOT EXISTS tool_calls_sid ON tool_calls(sid);
CREATE TABLE IF NOT EXISTS tool_blobs(
  sid TEXT NOT NULL, seq INTEGER NOT NULL, in_len INTEGER, out_len INTEGER, input_z BLOB, output_z BLOB,
  PRIMARY KEY(sid, seq));
CREATE TABLE IF NOT EXISTS t3_threads(
  box TEXT NOT NULL, thread_id TEXT NOT NULL, provider TEXT, native_id TEXT, sid TEXT, title TEXT, cwd TEXT,
  created REAL, updated REAL, updated_iso TEXT, archived REAL, deleted REAL, link TEXT,
  PRIMARY KEY(box, thread_id));
CREATE INDEX IF NOT EXISTS t3_threads_native ON t3_threads(native_id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(text, content='messages', content_rowid='id',
  tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text); END;
"""

AGENTS_MD = """# ~/me: you, your agents, your boxes

This folder is about the person who owns this account. Use it when they ask about their own history, habits,
efficiency, inbox or boxes. It is private to them (mode 0700).

- `agents.db`: SQLite, every agent session on every box they have (Claude Code, Codex, and T3 threads of other
  providers), kept forever. Tables: `sessions` (box, agent, model, cwd, title, started/ended as unix time, wall_s,
  active_s, prompts, tool_calls, tool_s, errors, interrupts, out_tokens), `messages` (sid, seq, ts, role
  user|assistant|thinking, text; full-text index `messages_fts`; thinking = the model's reasoning), `tool_calls`
  (sid, seq, ts, name, input, output (first 500 chars), dur_s, is_error), `tool_blobs` (sid, seq, in_len, out_len,
  input_z, output_z: full input/output, compressed; read with `unz(output_z)` in `box me sql`), `t3_threads`
  (thread_id, provider, native_id = the agent's session id, title, link).
  Query read-only: `box me sql "SELECT ..."` or `sqlite3 -readonly ~/me/agents.db`. Search: `box me search WORDS`
  (`--thinking` to include reasoning). `box me stats` shows size and counts. Ready-made queries: `queries.md`.
- `raw/`: xz copies of the agents' original transcript files (`xzcat raw/claude/projects/.../ID.jsonl.xz`), kept
  even after the agent deletes its own.
- Refreshed every ~15 minutes automatically (`box me ingest` and `box me pull` to do it now).
- Never paste their prompts anywhere outside this box.
"""

QUERIES_MD = """# Ready-made queries for agents.db (run with `box me sql "..."`)

## Slowest sessions this week (wall vs active time)
SELECT datetime(started,'unixepoch','localtime') AS start, box, agent, round(wall_s/60) AS wall_min,
       round(active_s/60) AS active_min, tool_calls, errors, substr(title,1,60) AS title
FROM sessions WHERE started > strftime('%s','now','-7 days') ORDER BY wall_s DESC LIMIT 20;

## Where tool time went (by tool, last 7 days)
SELECT name, count(*) AS calls, round(sum(dur_s)/3600,1) AS hours, sum(is_error) AS errors
FROM tool_calls WHERE ts > strftime('%s','now','-7 days') GROUP BY name ORDER BY hours DESC LIMIT 20;

## Sleep / poll loops (time spent waiting in shell commands)
SELECT count(*) AS calls, round(sum(dur_s)/3600,1) AS hours FROM tool_calls
WHERE input LIKE '%sleep %' OR input LIKE '%until %' OR input LIKE '%--watch%';

## Most common failing commands
SELECT substr(input,1,80) AS cmd, count(*) AS n FROM tool_calls WHERE is_error=1
GROUP BY cmd ORDER BY n DESC LIMIT 20;

## Find a prompt I wrote
SELECT datetime(m.ts,'unixepoch','localtime'), s.box, substr(m.text,1,200) FROM messages_fts f
JOIN messages m ON m.id=f.rowid JOIN sessions s ON s.id=m.sid
WHERE messages_fts MATCH 'machina AND rules' AND m.role='user' ORDER BY m.ts DESC LIMIT 20;

## What was the agent thinking (reasoning that mentions a word)
SELECT datetime(m.ts,'unixepoch','localtime'), s.agent, substr(m.text,1,300) FROM messages_fts f
JOIN messages m ON m.id=f.rowid JOIN sessions s ON s.id=m.sid
WHERE messages_fts MATCH 'deploy' AND m.role='thinking' ORDER BY m.ts DESC LIMIT 20;

## A whole session in order (prompts, reasoning, replies)
SELECT seq, role, substr(text,1,300) FROM messages WHERE sid='BOX:claude:SESSION-ID' ORDER BY seq;

## Full output of a tool call (tool_calls.output is only the first 500 characters)
SELECT t.name, t.input, b.out_len, unz(b.output_z) FROM tool_calls t
JOIN tool_blobs b ON b.sid=t.sid AND b.seq=t.seq WHERE t.sid='BOX:claude:SESSION-ID' AND t.seq=12;

## T3 thread for a session (title and link)
SELECT t.title, t.link, t.provider FROM t3_threads t JOIN sessions s ON s.session=t.native_id
WHERE s.id='BOX:claude:SESSION-ID';

## Usage per day
SELECT date(started,'unixepoch','localtime') AS day, count(*) AS sessions, round(sum(active_s)/3600,1) AS active_h,
       sum(out_tokens) AS out_tokens FROM sessions GROUP BY day ORDER BY day DESC LIMIT 30;
"""

# sha256 of every guide text ever shipped: a file still matching one was never edited, so it is safe to refresh
OLD_GUIDES = {"7abf33f7262628897cbc48c61dc9fd7307a93fd4841183f0e3c4fba9e8607f0b",   # AGENTS.md, schema 1
              "366e2c7ec7d3e1d29b2c8e4e8b684ca4ec003dbd7d5be6a0512b66d208370b57"}   # queries.md, schema 1


def me_dir(home=None):
    return os.path.join(home or os.path.expanduser("~"), ME_DIR)


def db_path(home=None):
    return os.path.join(me_dir(home), DB_NAME)


def raw_dir(home=None):
    return os.path.join(me_dir(home), RAW_DIR)


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def ensure_dir(home=None):
    """~/me (0700) with its guide files. A guide is (re)written only when missing or still exactly a text we shipped
    earlier: never overwrites one the user or their agent edited."""
    d = me_dir(home)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    for name, text in (("AGENTS.md", AGENTS_MD), ("queries.md", QUERIES_MD)):
        p = os.path.join(d, name)
        try:
            with open(p, "rb") as f:
                if _sha(f.read()) not in OLD_GUIDES:
                    continue
        except FileNotFoundError:
            pass
        tmp = p + ".sbme-tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, p)
    link = os.path.join(d, "CLAUDE.md")
    if not os.path.lexists(link):
        os.symlink("AGENTS.md", link)
    return d


def migrate(con):
    """Bring an older agents.db up to date (idempotent; every new table is CREATE IF NOT EXISTS in SCHEMA). A newer
    parser marks every file as changed so the next passes parse it again (old rows stay until replaced)."""
    row = con.execute("SELECT v FROM meta WHERE k='parser'").fetchone()
    if not row or int(row[0]) < PARSER_VERSION:
        with con:
            con.execute("UPDATE files SET size=-1")
            con.execute("INSERT OR REPLACE INTO meta VALUES('parser', ?)", (str(PARSER_VERSION),))
    con.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (str(SCHEMA_VERSION),))
    con.commit()


def unz(b):
    """zlib blob -> text (registered as SQL function unz() for `box me sql`)."""
    if b is None:
        return None
    try:
        return zlib.decompress(b).decode("utf-8", "replace")
    except (zlib.error, TypeError):
        return None


def connect(path, readonly=False):
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    else:
        new = not os.path.exists(path)
        con = sqlite3.connect(path, timeout=30)
        if new:
            os.chmod(path, 0o600)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.executescript(SCHEMA)
        migrate(con)
    con.create_function("unz", 1, unz, deterministic=True)
    return con


# ---------- parsing (pure: path -> session dict) ----------

def _ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in c
                         if not isinstance(x, dict) or x.get("type") in (None, "text", "input_text", "output_text"))
    return "" if c is None else str(c)


def _cut(s, n):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


def _desc(inp):
    if not isinstance(inp, dict):
        return _cut(inp, TOOL_IN_MAX)
    for k in ("command", "cmd", "file_path", "pattern", "url", "query", "prompt", "description"):
        if isinstance(inp.get(k), str):
            return _cut(inp[k], TOOL_IN_MAX)
    return _cut(inp, TOOL_IN_MAX)


def _tool(seq, ts, name, inp):
    """A tool call: readable input for tool_calls, plus the full input when it says meaningfully more."""
    desc = _desc(inp)
    full = inp if isinstance(inp, str) else json.dumps(inp, ensure_ascii=False)
    tc = {"seq": seq, "ts": ts, "name": name, "input": desc, "output": "", "dur_s": None, "is_error": 0,
          "full_in": None, "in_len": len(full), "full_out": None, "out_len": 0}
    if full != desc and (desc.endswith("…") or len(full) > len(desc) + FULL_IN_SLACK):
        tc["full_in"] = full[:TOOL_FULL_MAX]
    return tc


def _set_output(tc, txt):
    tc["output"] = _cut(txt, TOOL_OUT_MAX)
    tc["out_len"] = len(txt)
    tc["full_out"] = txt[:TOOL_FULL_MAX] if len(txt) > TOOL_OUT_MAX else None


def _new(agent, path):
    return {"agent": agent, "session": os.path.basename(path)[:-6], "messages": [], "tools": [], "title": None,
            "cwd": None, "model": None, "interrupts": 0, "compactions": 0, "out_tokens": 0, "cache_read_tokens": 0,
            "times": []}


def parse_claude(path):
    s = _new("claude", path)
    if os.sep + "subagents" + os.sep in path:
        s["parent"] = os.path.basename(os.path.dirname(os.path.dirname(path)))
        s["agent"] = "claude-sub"
    calls = {}
    seq = 0
    with open(path, errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            ty = e.get("type")
            if ty == "custom-title":
                s["title"] = e.get("customTitle") or s["title"]
            elif ty == "summary" and not s["title"]:
                s["title"] = e.get("summary")
            if e.get("cwd") and not s["cwd"]:
                s["cwd"] = e["cwd"]
            if e.get("isCompactSummary"):
                s["compactions"] += 1
            t = _ts(e.get("timestamp"))
            m = e.get("message") if isinstance(e.get("message"), dict) else {}
            if ty == "assistant":
                if t:
                    s["times"].append(t)
                u = m.get("usage") or {}
                s["out_tokens"] += u.get("output_tokens") or 0
                s["cache_read_tokens"] += u.get("cache_read_input_tokens") or 0
                if m.get("model") and m["model"] != "<synthetic>":
                    s["model"] = m["model"]
                for c in m.get("content") or []:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_use":
                        seq += 1
                        tc = _tool(seq, t, c.get("name"), c.get("input"))
                        calls[c.get("id")] = tc
                        s["tools"].append(tc)
                    elif c.get("type") == "text" and c.get("text", "").strip():
                        seq += 1
                        s["messages"].append({"seq": seq, "ts": t, "role": "assistant", "text": c["text"]})
                    elif c.get("type") == "thinking" and isinstance(c.get("thinking"), str) and c["thinking"].strip():
                        seq += 1      # redacted_thinking is encrypted: nothing readable to keep
                        s["messages"].append({"seq": seq, "ts": t, "role": "thinking", "text": c["thinking"]})
            elif ty == "user":
                if t:
                    s["times"].append(t)
                c = m.get("content")
                items = [{"type": "text", "text": c}] if isinstance(c, str) else (c if isinstance(c, list) else [])
                for x in items:
                    if not isinstance(x, dict):
                        continue
                    if x.get("type") == "tool_result":
                        tc = calls.get(x.get("tool_use_id"))
                        if tc:
                            _set_output(tc, _text(x.get("content")))
                            tc["is_error"] = 1 if x.get("is_error") else 0
                            if t and tc["ts"]:
                                tc["dur_s"] = round(max(0.0, t - tc["ts"]), 1)
                    elif x.get("type") == "text":
                        tx = x.get("text") or ""
                        if "[Request interrupted" in tx:
                            s["interrupts"] += 1
                        elif tx.strip() and not e.get("isMeta") and not tx.lstrip().startswith("<"):
                            seq += 1
                            s["messages"].append({"seq": seq, "ts": t, "role": "user", "text": tx})
    return s


_ERR_RE = re.compile(r"(exit code|Process exited with code) [1-9]|^Error:|Script failed", re.I | re.M)


def _texts(items):
    """Text parts of a reasoning summary/content list (dicts with "text", or plain strings)."""
    out = []
    for x in items if isinstance(items, list) else []:
        tx = x.get("text") if isinstance(x, dict) else x
        if isinstance(tx, str) and tx.strip():
            out.append(tx)
    return "\n\n".join(out)


def parse_codex(path):
    s = _new("codex", path)
    calls = {}
    seq = 0
    reasoned = False      # saw readable reasoning in response_items (the record of truth)
    ev_reasoning = []     # reasoning seen only in event_msgs: used when the response_items had none
    with open(path, errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            ty = e.get("type")
            p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
            pt = p.get("type")
            t = _ts(e.get("timestamp"))
            if ty in ("session_meta", "turn_context"):
                s["cwd"] = s["cwd"] or p.get("cwd")
                s["model"] = p.get("model") or s["model"]
                if ty == "session_meta" and p.get("id"):
                    s["session"] = p["id"]
            if ty == "compacted" or pt == "context_compacted":
                s["compactions"] += 1
            if pt == "turn_aborted":
                s["interrupts"] += 1
            if pt == "token_count":
                tot = ((p.get("info") or {}).get("total_token_usage") or {})
                s["out_tokens"] = tot.get("output_tokens") or s["out_tokens"]
                s["cache_read_tokens"] = tot.get("cached_input_tokens") or s["cache_read_tokens"]
            if ty == "event_msg":
                txt = ""
                if pt in ("agent_reasoning", "agent_reasoning_raw_content"):
                    txt = p.get("text") if isinstance(p.get("text"), str) else ""
                elif pt == "item_completed" and isinstance(p.get("item"), dict) \
                        and p["item"].get("type") in ("Reasoning", "reasoning"):
                    it = p["item"]
                    txt = _texts(it.get("summary_text")) or _texts(it.get("raw_content"))
                if txt.strip():
                    seq += 1
                    ev_reasoning.append({"seq": seq, "ts": t, "role": "thinking", "text": txt})
                continue
            if ty != "response_item":
                continue
            if t:
                s["times"].append(t)
            if pt in ("function_call", "custom_tool_call", "local_shell_call"):
                a = p.get("arguments") or p.get("input") or p.get("action") or ""
                if isinstance(a, str) and a.startswith("{"):
                    try:
                        a = json.loads(a)
                    except ValueError:
                        pass
                seq += 1
                tc = _tool(seq, t, p.get("name") or "shell", a)
                calls[p.get("call_id")] = tc
                s["tools"].append(tc)
            elif pt in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
                tc = calls.get(p.get("call_id"))
                if tc:
                    o = p.get("output")
                    txt = o if isinstance(o, str) else _text(o)
                    _set_output(tc, txt)
                    tc["is_error"] = 1 if _ERR_RE.search(txt[:600]) else 0
                    if t and tc["ts"]:
                        tc["dur_s"] = round(max(0.0, t - tc["ts"]), 1)
            elif pt == "reasoning":
                txt = _texts(p.get("summary"))
                raw = _texts([x for x in (p.get("content") or []) if isinstance(x, dict)
                              and x.get("type") in ("reasoning_text", "text")])
                txt = "\n\n".join(x for x in (txt, raw) if x)
                if txt:
                    reasoned = True
                    seq += 1
                    s["messages"].append({"seq": seq, "ts": t, "role": "thinking", "text": txt})
            elif pt == "message":
                role = p.get("role")
                txt = _text(p.get("content"))
                if role == "user" and txt.strip() and not txt.lstrip().startswith("<"):
                    seq += 1
                    s["messages"].append({"seq": seq, "ts": t, "role": "user", "text": txt})
                elif role == "assistant" and txt.strip():
                    seq += 1
                    s["messages"].append({"seq": seq, "ts": t, "role": "assistant", "text": txt})
    if ev_reasoning and not reasoned:
        s["messages"] = sorted(s["messages"] + ev_reasoning, key=lambda m: m["seq"])
    return s


def summarize(s):
    """Fill the per-session numbers from the parsed lists."""
    times = sorted(t for t in s.pop("times") if t)
    gaps = [b - a for a, b in zip(times, times[1:])]
    s["started"] = times[0] if times else None
    s["ended"] = times[-1] if times else None
    s["wall_s"] = round(times[-1] - times[0], 1) if times else 0
    s["active_s"] = round(sum(g for g in gaps if g < ACTIVE_GAP), 1)
    s["idle_gaps"] = sum(1 for g in gaps if g >= ACTIVE_GAP)
    s["prompts"] = sum(1 for m in s["messages"] if m["role"] == "user")
    s["tool_calls"] = len(s["tools"])
    s["tool_s"] = round(sum(tc["dur_s"] or 0 for tc in s["tools"]), 1)
    s["errors"] = sum(tc["is_error"] for tc in s["tools"])
    return s


def parse(path):
    s = parse_codex(path) if f"{os.sep}.codex{os.sep}" in path else parse_claude(path)
    return summarize(s)


# ---------- storing ----------

def sources(home):
    out = []
    root = os.path.join(home, ".claude", "projects")
    for dp, dns, fns in os.walk(root):
        out += [os.path.join(dp, f) for f in fns if f.endswith(".jsonl")]
    for sub in ("sessions", "archived_sessions"):
        for dp, dns, fns in os.walk(os.path.join(home, ".codex", sub)):
            out += [os.path.join(dp, f) for f in fns if f.endswith(".jsonl")]
    return out


def raw_sources(home):
    """What the raw archive keeps: every transcript, plus Claude Code's saved large tool outputs (tool-results/)."""
    out = sources(home)
    for dp, dns, fns in os.walk(os.path.join(home, ".claude", "projects")):
        if os.path.basename(dp) == "tool-results":
            out += [os.path.join(dp, f) for f in fns]
    return out


def _z(s):
    return zlib.compress(s.encode("utf-8", "replace"), 6) if s else None


def _blob_rows(sid, tools):
    return [(sid, t["seq"], t.get("in_len"), t.get("out_len"), _z(t.get("full_in")), _z(t.get("full_out")))
            for t in tools if t.get("full_in") or t.get("full_out")]


def _replace_session(con, sid, row, messages, tool_rows, blob_rows):
    con.execute("DELETE FROM messages WHERE sid=?", (sid,))
    con.execute("DELETE FROM tool_calls WHERE sid=?", (sid,))
    con.execute("DELETE FROM tool_blobs WHERE sid=?", (sid,))
    con.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
    con.executemany("INSERT INTO messages(sid,seq,ts,role,text) VALUES(?,?,?,?,?)",
                    [(sid, m["seq"], m["ts"], m["role"], m["text"]) for m in messages])
    con.executemany("INSERT INTO tool_calls(sid,seq,ts,name,input,output,dur_s,is_error) VALUES(?,?,?,?,?,?,?,?)",
                    tool_rows)
    con.executemany("INSERT OR REPLACE INTO tool_blobs VALUES(?,?,?,?,?,?)", blob_rows)


def store(con, box, path, s):
    sid = f"{box}:{s['agent']}:{s['session']}"
    row = (sid, box, s["agent"], s["session"], s.get("parent"), path, s["cwd"], s["title"], s["model"],
           s["started"], s["ended"], s["wall_s"], s["active_s"], s["idle_gaps"], s["prompts"],
           s["tool_calls"], s["tool_s"], s["errors"], s["interrupts"], s["compactions"], s["out_tokens"],
           s["cache_read_tokens"], time.time())
    _replace_session(con, sid, row, s["messages"],
                     [(sid, t["seq"], t["ts"], t["name"], t["input"], t["output"], t["dur_s"], t["is_error"])
                      for t in s["tools"]], _blob_rows(sid, s["tools"]))
    return sid


def _left(deadline):
    return deadline is None or time.time() < deadline


def archive_raw(home, deadline=None):
    """xz copy of every raw transcript into ~/me/raw/<path relative to home, leading dot dropped>.xz. A copy is
    made again when the source's mtime differs from the copy's (the copy carries the source's mtime); copies are
    never deleted, so the archive outlives the agents' own cleanup. Returns {"files", "copied", "mb_in"}."""
    root = raw_dir(home)
    rep = {"files": 0, "copied": 0, "mb_in": 0.0}
    now = time.time()
    os.makedirs(root, mode=0o700, exist_ok=True)     # ~/me is 0700 already; the archive is too
    os.chmod(root, 0o700)
    made = set()
    for src in raw_sources(home):
        if not _left(deadline):
            rep["partial"] = True
            break
        rep["files"] += 1
        rel = os.path.relpath(src, home)
        dst = os.path.join(root, rel.lstrip(".")) + ".xz"
        try:
            st = os.stat(src)
            if st.st_mtime > now - FRESH_S:
                continue
            try:
                if os.stat(dst).st_mtime_ns == st.st_mtime_ns:
                    continue
            except FileNotFoundError:
                pass
            d = os.path.dirname(dst)
            if d not in made:
                os.makedirs(d, mode=0o700, exist_ok=True)
                made.add(d)
            tmp = dst + ".sbme-tmp"
            with open(src, "rb") as fi, open(tmp, "wb") as raw:
                os.fchmod(raw.fileno(), 0o600)
                with lzma.LZMAFile(raw, "wb", preset=RAW_PRESET) as fo:
                    shutil.copyfileobj(fi, fo, 1 << 20)
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.replace(tmp, dst)
            rep["copied"] += 1
            rep["mb_in"] += st.st_size / 1e6
        except OSError as e:
            print(f"me raw: {src}: {e!r}", file=sys.stderr)
    rep["mb_in"] = round(rep["mb_in"], 1)
    return rep


def _t3_rows(t3, since):
    return t3.execute("""
        SELECT t.thread_id, t.title, t.default_provider, t.created_at, t.updated_at, t.archived_at, t.deleted_at,
               coalesce(CASE WHEN json_valid(t.payload_json) THEN json_extract(t.payload_json, '$.worktreePath') END,
                        p.workspace_root),
               (SELECT CASE WHEN json_valid(pt.payload_json)
                            THEN json_extract(pt.payload_json, '$.nativeThreadRef.nativeId') END
                  FROM orchestration_v2_projection_provider_threads pt
                 WHERE pt.thread_id = t.thread_id ORDER BY pt.updated_at DESC LIMIT 1)
          FROM orchestration_v2_projection_threads t LEFT JOIN projection_projects p ON p.project_id = t.project_id
         WHERE t.updated_at > ? ORDER BY t.updated_at""", (since,)).fetchall()


def ingest_t3(con, home, box, deadline=None):
    """T3 Code's own thread store: titles/links of every thread (t3_threads) and, for providers whose transcripts we
    don't read (OpenCode, Antigravity, ...), the thread's messages as a session (agent = provider). Read-only and
    incremental (threads updated since the last pass); a T3 schema we don't know is skipped quietly."""
    path = os.path.join(home, T3_DB)
    rep = {"threads": 0, "sessions": 0}
    if not os.path.exists(path):
        return rep
    try:
        with open(os.path.join(home, ".t3", "userdata", "environment-id")) as f:
            env = f.read().strip()
    except OSError:
        env = ""
    row = con.execute("SELECT v FROM meta WHERE k=?", (f"t3_since:{box}",)).fetchone()
    since = row[0] if row else ""
    try:
        t3 = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        rows = _t3_rows(t3, since)
    except sqlite3.Error as e:
        print(f"me t3: {e!r}", file=sys.stderr)
        return rep
    try:
        for tid, title, prov, created, updated, archived, deleted, cwd, native in rows:
            if not _left(deadline):
                rep["partial"] = True
                break
            agent = T3_COVERED.get(prov) or re.sub(r"[^a-z0-9-]", "", str(prov).lower()) or "t3"
            sid = f"{box}:{agent}:{native if prov in T3_COVERED and native else tid}"
            with con:
                if prov not in T3_COVERED:
                    msgs = t3.execute(
                        "SELECT role, created_at, CASE WHEN json_valid(payload_json) THEN "
                        "json_extract(payload_json, '$.text') END FROM orchestration_v2_projection_messages "
                        "WHERE thread_id=? ORDER BY created_at, message_id", (tid,)).fetchall()
                    s = {"agent": agent, "session": tid, "messages": [], "tools": [], "title": title, "cwd": cwd,
                         "model": None, "interrupts": 0, "compactions": 0, "out_tokens": 0, "cache_read_tokens": 0,
                         "times": []}
                    for i, (role, at, text) in enumerate(msgs, 1):
                        t = _ts(at)
                        if t:
                            s["times"].append(t)
                        if role in ("user", "assistant") and isinstance(text, str) and text.strip():
                            s["messages"].append({"seq": i, "ts": t, "role": role, "text": text})
                    if s["messages"]:
                        store(con, box, f"t3:{tid}", summarize(s))
                        rep["sessions"] += 1
                con.execute("INSERT OR REPLACE INTO t3_threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (box, tid, prov, native, sid, title, cwd, _ts(created), _ts(updated), updated,
                             _ts(archived), _ts(deleted), f"t3-thread://v2/{env}/{tid}" if env else None))
                con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"t3_since:{box}", updated))
            rep["threads"] += 1
    except sqlite3.Error as e:
        print(f"me t3: {e!r}", file=sys.stderr)
    finally:
        t3.close()
    return rep


def ingest(home=None, box=None, limit=None, verbose=False, budget=None):
    """Archive raw transcripts, then index new or changed ones, then T3 threads. `budget` (seconds) stops a long
    first pass early; the next pass continues where it stopped. Returns {"files", "parsed", "skipped", "errors",
    "raw", "t3", "seconds"}."""
    home = home or os.path.expanduser("~")
    box = box or socket.gethostname()
    ensure_dir(home)
    t0 = time.time()
    deadline = t0 + budget if budget else None
    rep = {"files": 0, "parsed": 0, "skipped": 0, "errors": 0}
    rep["raw"] = archive_raw(home, deadline)
    con = connect(db_path(home))
    seen = {p: (sz, mt) for p, sz, mt in con.execute("SELECT path,size,mtime FROM files")}
    for path in sources(home):
        rep["files"] += 1
        try:
            st = os.stat(path)
        except OSError:
            continue
        if seen.get(path) == (st.st_size, st.st_mtime):
            rep["skipped"] += 1
            continue
        if st.st_mtime > time.time() - FRESH_S:      # still being written: next pass
            continue
        if not _left(deadline):
            rep["partial"] = True
            continue
        try:
            s = parse(path)
            if not s["messages"] and not s["tools"]:
                con.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)", (path, st.st_size, st.st_mtime, "", box))
                continue
            with con:
                sid = store(con, box, path, s)
                con.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)", (path, st.st_size, st.st_mtime, sid, box))
            rep["parsed"] += 1
            if verbose:
                print(f"indexed {sid}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the rest
            rep["errors"] += 1
            print(f"me: {path}: {e!r}", file=sys.stderr)
        if limit and rep["parsed"] >= limit:
            break
    con.commit()
    rep["t3"] = ingest_t3(con, home, box, deadline)
    con.commit()
    con.close()
    rep["seconds"] = round(time.time() - t0, 1)
    return rep


# ---------- other boxes ----------

def _b64(b):
    return base64.b64encode(b).decode() if b else None


def _unb64(s):
    try:
        return base64.b64decode(s) if s else None
    except ValueError:
        return None


def export_since(home, since, box=None):
    """JSON lines of this box's sessions updated after `since` (with messages incl. thinking, tool calls and their
    full compressed input/output as base64 "input_z"/"output_z"), for `box me pull`. Opens read-write so an
    agents.db from before this version gets its new tables first."""
    con = connect(db_path(home))
    con.row_factory = sqlite3.Row
    box = box or socket.gethostname()
    for row in con.execute("SELECT * FROM sessions WHERE box=? AND updated>? ORDER BY updated", (box, since)):
        d = dict(row)
        d["messages"] = [dict(r) for r in con.execute(
            "SELECT seq,ts,role,text FROM messages WHERE sid=? ORDER BY seq", (d["id"],))]
        d["tools"] = []
        for r in con.execute("SELECT t.seq,t.ts,t.name,t.input,t.output,t.dur_s,t.is_error,b.in_len,b.out_len,"
                             "b.input_z,b.output_z FROM tool_calls t LEFT JOIN tool_blobs b "
                             "ON b.sid=t.sid AND b.seq=t.seq WHERE t.sid=? ORDER BY t.seq", (d["id"],)):
            t = dict(r)
            t["input_z"], t["output_z"] = _b64(t["input_z"]), _b64(t["output_z"])
            d["tools"].append(t)
        yield json.dumps(d, ensure_ascii=False)


SESSION_COLS = ["id", "box", "agent", "session", "parent", "path", "cwd", "title", "model", "started", "ended",
                "wall_s", "active_s", "idle_gaps", "prompts", "tool_calls", "tool_s", "errors", "interrupts",
                "compactions", "out_tokens", "cache_read_tokens", "updated"]


def import_lines(con, lines):
    """Store what export_since printed on another box (older boxes send no blobs: those sessions just lack them)."""
    n = 0
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        sid = d["id"]
        tools = d.get("tools", [])
        blobs = [(sid, t["seq"], t.get("in_len"), t.get("out_len"), _unb64(t.get("input_z")),
                  _unb64(t.get("output_z"))) for t in tools if t.get("input_z") or t.get("output_z")]
        with con:
            _replace_session(con, sid, [d.get(c) for c in SESSION_COLS], d.get("messages", []),
                             [(sid, t["seq"], t["ts"], t["name"], t["input"], t["output"], t["dur_s"], t["is_error"])
                              for t in tools], blobs)
        n += 1
    return n


def pull(home, dests, ssh_opts=()):
    """Copy what each other box indexed into this box's agents.db. `dests` = {box: ssh destination} for this account
    (lib/box projects_peers). Only sessions updated since the last pull travel."""
    con = connect(db_path(home))
    out = {}
    for name, dest in sorted(dests.items()):
        key = f"pulled:{name}"
        row = con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        since = float(row[0]) if row else 0.0
        started = time.time()
        try:
            p = subprocess.Popen(["ssh", *ssh_opts, dest, f"{' '.join(NICE)} box _me-export --since {since:.0f}"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True)
            n = import_lines(con, p.stdout)
            if p.wait() == 0:
                con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(started - 60)))
                con.commit()
                out[name] = n
            else:
                out[name] = f"error (exit {p.returncode})"
        except OSError as e:
            out[name] = f"error ({e})"
    con.close()
    return out


def due(home, every=900):
    """True when the last automatic pass is older than `every` seconds (and records this one)."""
    path = db_path(home)
    if not os.path.exists(path):
        return True
    con = connect(path)
    row = con.execute("SELECT v FROM meta WHERE k='auto'").fetchone()
    now = time.time()
    if row and now - float(row[0]) < every:
        con.close()
        return False
    con.execute("INSERT OR REPLACE INTO meta VALUES('auto', ?)", (str(now),))
    con.commit()
    con.close()
    return True


KEEP_DAYS = 36500
CODEX_SUMMARY_KEY = "model_reasoning_summary"
CODEX_SUMMARY_LINE = 'model_reasoning_summary = "detailed"  # treehouse ~/me keeps readable reasoning (box help me)\n'


def _write_atomic(path, data, mode_from=None):
    tmp = path + ".sbme-tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    if mode_from and os.path.exists(mode_from):
        os.chmod(tmp, os.stat(mode_from).st_mode & 0o777)
    os.replace(tmp, path)


def keep_codex_reasoning(home):
    """Codex stores its reasoning only encrypted unless reasoning summaries are on. Adds top-level
    `model_reasoning_summary = "detailed"` (Codex accepts auto|concise|detailed|none) to ~/.codex/config.toml when the
    key is absent there; never changes a value the user set, never touches a file that isn't valid TOML, and checks
    the result parses to the same settings plus that one key before replacing the file atomically. Only for
    accounts that have used Codex (~/.codex exists). Returns True when the file changed."""
    cdir = os.path.join(home, ".codex")
    if not os.path.isdir(cdir):
        return False
    path = os.path.realpath(os.path.join(cdir, "config.toml"))
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        raw = b""
    except OSError:
        return False
    try:
        text = raw.decode("utf-8")
        cur = tomllib.loads(text)
    except (UnicodeDecodeError, ValueError):
        return False
    if CODEX_SUMMARY_KEY in cur:
        return False
    new = CODEX_SUMMARY_LINE + text       # top of the file: before any [table], so it is a top-level key
    try:
        after = tomllib.loads(new)
    except ValueError:
        return False
    if after.pop(CODEX_SUMMARY_KEY, None) != "detailed" or after != cur:
        return False
    _write_atomic(path, new.encode("utf-8"), path)
    return True


def keep_history(home):
    """Agents keep their transcripts forever, with readable reasoning: Claude Code's cleanupPeriodDays (default 30
    days), T3's usage history retention and Codex's reasoning summaries. Edits only those keys, only when lower or
    absent; never rewrites a file it can't parse. Returns the list of files changed."""
    changed = []
    for rel, key, want, low in ((".claude/settings.json", "cleanupPeriodDays", KEEP_DAYS,
                                 lambda v: not isinstance(v, int) or v < KEEP_DAYS),
                                (".t3/userdata/settings.json", "usageHistoryRetentionDays", "unlimited",
                                 lambda v: v != "unlimited")):
        path = os.path.join(home, rel)
        if rel.startswith(".t3") and not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                d = json.load(f)
        except FileNotFoundError:
            d = {}
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or not low(d.get(key)):
            continue
        d[key] = want
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _write_atomic(path, (json.dumps(d, indent=2) + "\n").encode(), path)
        changed.append(rel)
    try:
        if keep_codex_reasoning(home):
            changed.append(".codex/config.toml")
    except OSError as e:
        print(f"me: codex config: {e!r}", file=sys.stderr)
    return changed


# ---------- reading ----------

def _du(path):
    total = 0
    for dp, dns, fns in os.walk(path):
        for f in fns:
            try:
                total += os.lstat(os.path.join(dp, f)).st_size
            except OSError:
                pass
    return total


def stats(home=None):
    path = db_path(home)
    if not os.path.exists(path):
        return {"exists": False}
    con = connect(path, readonly=True)
    q = lambda sql: con.execute(sql).fetchall()  # noqa: E731
    size = sum(os.path.getsize(path + ext) for ext in ("", "-wal") if os.path.exists(path + ext))
    has = {r[0] for r in q("SELECT name FROM sqlite_master WHERE type='table'")}
    out = {"exists": True, "path": path, "size_mb": round(size / 1e6, 1),
           "raw_mb": round(_du(raw_dir(home)) / 1e6, 1),
           "sessions": q("SELECT count(*) FROM sessions")[0][0],
           "messages": q("SELECT count(*) FROM messages WHERE role!='thinking'")[0][0],
           "thinking": q("SELECT count(*) FROM messages WHERE role='thinking'")[0][0],
           "tool_calls": q("SELECT count(*) FROM tool_calls")[0][0],
           "full_tool_io": q("SELECT count(*) FROM tool_blobs")[0][0] if "tool_blobs" in has else 0,
           "by_box_agent": {f"{b} {a}": n for b, a, n in q(
               "SELECT box, agent, count(*) FROM sessions GROUP BY 1,2 ORDER BY 3 DESC")},
           "first": q("SELECT datetime(min(started),'unixepoch','localtime') FROM sessions")[0][0],
           "last": q("SELECT datetime(max(started),'unixepoch','localtime') FROM sessions")[0][0]}
    con.close()
    return out


def search(home, words, limit=20, thinking=False):
    """Full-text search, newest first. Reasoning (role 'thinking') is left out unless thinking=True."""
    con = connect(db_path(home), readonly=True)
    rows = con.execute(
        "SELECT datetime(m.ts,'unixepoch','localtime'), s.box, s.agent, m.role, coalesce(s.title,''), "
        "snippet(messages_fts, 0, '[', ']', '…', 24) FROM messages_fts JOIN messages m ON m.id=messages_fts.rowid "
        "JOIN sessions s ON s.id=m.sid WHERE messages_fts MATCH ? AND (? OR m.role!='thinking') "
        "ORDER BY m.ts DESC LIMIT ?", (words, 1 if thinking else 0, limit)).fetchall()
    con.close()
    return rows


def sql(home, query):
    con = connect(db_path(home), readonly=True)
    cur = con.execute(query)
    cols = [d[0] for d in cur.description or []]
    rows = cur.fetchall()
    con.close()
    return cols, rows
