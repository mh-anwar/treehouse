"""`~/me`: each account's own searchable history of every agent session (prompts, replies, tool calls) plus
per-session efficiency numbers, in one SQLite file: ~/me/agents.db.

Why: Claude Code deletes its transcripts after 30 days (unless cleanupPeriodDays is raised), Codex and the others
keep theirs in different formats, and nobody can ask "why was my agent slow yesterday" or "find the prompt where I
explained X" across all of them. This module reads the transcripts the agents already write and keeps a compact,
indexed copy forever. The raw files stay where they are.

Sources (read-only):
- Claude Code: ~/.claude/projects/*/*.jsonl and their subagents/*.jsonl
- Codex: ~/.codex/sessions/YYYY/MM/DD/*.jsonl and ~/.codex/archived_sessions/*.jsonl

What is stored: user prompts and assistant replies in full; each tool call's name, its input (first 2,000
characters), its output (first 500), how long it took and whether it failed. Thinking blocks and full tool outputs
are not stored. Per session: box, agent, model, folder, title, start/end, active time, counts, tokens.

Runs AS THE ACCOUNT (the janitor calls `box _me-ingest` at low priority, `box me ingest` by hand). Incremental: a
file is parsed again only when its size or mtime changed. Other boxes: `box me pull` copies the sessions the
account's other boxes indexed (over the account's own fed SSH login) so one agents.db holds every box.
Stdlib only.
"""

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

ME_DIR = "me"
DB_NAME = "agents.db"
SCHEMA_VERSION = 1
TOOL_IN_MAX = 2000
TOOL_OUT_MAX = 500
ACTIVE_GAP = 300          # gaps shorter than this count as active time
NICE = ("nice", "-n", "15", "ionice", "-c", "3")

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

- `agents.db`: SQLite, every agent session on every box they have (Claude Code, Codex), kept forever.
  Tables: `sessions` (box, agent, model, cwd, title, started/ended as unix time, wall_s, active_s, prompts,
  tool_calls, tool_s, errors, interrupts, out_tokens), `messages` (sid, ts, role user|assistant, text; full-text
  index `messages_fts`), `tool_calls` (sid, ts, name, input, output, dur_s, is_error).
  Query read-only: `box me sql "SELECT ..."` or `sqlite3 -readonly ~/me/agents.db`. Search: `box me search WORDS`.
  `box me stats` shows size and counts. Ready-made queries: `queries.md`.
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

## Usage per day
SELECT date(started,'unixepoch','localtime') AS day, count(*) AS sessions, round(sum(active_s)/3600,1) AS active_h,
       sum(out_tokens) AS out_tokens FROM sessions GROUP BY day ORDER BY day DESC LIMIT 30;
"""


def me_dir(home=None):
    return os.path.join(home or os.path.expanduser("~"), ME_DIR)


def db_path(home=None):
    return os.path.join(me_dir(home), DB_NAME)


def ensure_dir(home=None):
    """~/me (0700) with its guide files; never overwrites a guide the user or their agent edited."""
    d = me_dir(home)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    for name, text in (("AGENTS.md", AGENTS_MD), ("queries.md", QUERIES_MD)):
        p = os.path.join(d, name)
        if not os.path.exists(p):
            with open(p, "w") as f:
                f.write(text)
    link = os.path.join(d, "CLAUDE.md")
    if not os.path.lexists(link):
        os.symlink("AGENTS.md", link)
    return d


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
        con.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (str(SCHEMA_VERSION),))
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


def parse_claude(path):
    s = {"agent": "claude", "messages": [], "tools": [], "title": None, "cwd": None, "model": None,
         "interrupts": 0, "compactions": 0, "out_tokens": 0, "cache_read_tokens": 0, "times": []}
    s["session"] = os.path.basename(path)[:-6]
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
            m = e.get("message") or {}
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
                        tc = {"seq": seq, "ts": t, "name": c.get("name"), "input": _desc(c.get("input")),
                              "output": "", "dur_s": None, "is_error": 0}
                        calls[c.get("id")] = tc
                        s["tools"].append(tc)
                    elif c.get("type") == "text" and c.get("text", "").strip():
                        seq += 1
                        s["messages"].append({"seq": seq, "ts": t, "role": "assistant", "text": c["text"]})
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
                            tc["output"] = _cut(_text(x.get("content")), TOOL_OUT_MAX)
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


def parse_codex(path):
    s = {"agent": "codex", "messages": [], "tools": [], "title": None, "cwd": None, "model": None,
         "interrupts": 0, "compactions": 0, "out_tokens": 0, "cache_read_tokens": 0, "times": []}
    s["session"] = os.path.basename(path)[:-6]
    calls = {}
    seq = 0
    with open(path, errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
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
                tc = {"seq": seq, "ts": t, "name": p.get("name") or "shell", "input": _desc(a), "output": "",
                      "dur_s": None, "is_error": 0}
                calls[p.get("call_id")] = tc
                s["tools"].append(tc)
            elif pt in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
                tc = calls.get(p.get("call_id"))
                if tc:
                    o = p.get("output")
                    txt = o if isinstance(o, str) else _text(o)
                    tc["output"] = _cut(txt, TOOL_OUT_MAX)
                    tc["is_error"] = 1 if _ERR_RE.search(txt[:600]) else 0
                    if t and tc["ts"]:
                        tc["dur_s"] = round(max(0.0, t - tc["ts"]), 1)
            elif pt == "message":
                role = p.get("role")
                txt = _text(p.get("content"))
                if role == "user" and txt.strip() and not txt.lstrip().startswith("<"):
                    seq += 1
                    s["messages"].append({"seq": seq, "ts": t, "role": "user", "text": txt})
                elif role == "assistant" and txt.strip():
                    seq += 1
                    s["messages"].append({"seq": seq, "ts": t, "role": "assistant", "text": txt})
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


def store(con, box, path, s):
    sid = f"{box}:{s['agent']}:{s['session']}"
    con.execute("DELETE FROM messages WHERE sid=?", (sid,))
    con.execute("DELETE FROM tool_calls WHERE sid=?", (sid,))
    con.execute("""INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, box, s["agent"], s["session"], s.get("parent"), path, s["cwd"], s["title"], s["model"],
                 s["started"], s["ended"], s["wall_s"], s["active_s"], s["idle_gaps"], s["prompts"],
                 s["tool_calls"], s["tool_s"], s["errors"], s["interrupts"], s["compactions"], s["out_tokens"],
                 s["cache_read_tokens"], time.time()))
    con.executemany("INSERT INTO messages(sid,seq,ts,role,text) VALUES(?,?,?,?,?)",
                    [(sid, m["seq"], m["ts"], m["role"], m["text"]) for m in s["messages"]])
    con.executemany("INSERT INTO tool_calls(sid,seq,ts,name,input,output,dur_s,is_error) VALUES(?,?,?,?,?,?,?,?)",
                    [(sid, t["seq"], t["ts"], t["name"], t["input"], t["output"], t["dur_s"], t["is_error"])
                     for t in s["tools"]])
    return sid


def ingest(home=None, box=None, limit=None, verbose=False):
    """Index new or changed transcripts. Returns {"files", "parsed", "skipped", "errors", "seconds"}."""
    home = home or os.path.expanduser("~")
    box = box or socket.gethostname()
    ensure_dir(home)
    con = connect(db_path(home))
    t0 = time.time()
    seen = {p: (sz, mt) for p, sz, mt in con.execute("SELECT path,size,mtime FROM files")}
    rep = {"files": 0, "parsed": 0, "skipped": 0, "errors": 0}
    for path in sources(home):
        rep["files"] += 1
        try:
            st = os.stat(path)
        except OSError:
            continue
        if seen.get(path) == (st.st_size, st.st_mtime):
            rep["skipped"] += 1
            continue
        if st.st_mtime > time.time() - 20:      # still being written: next pass
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
    con.close()
    rep["seconds"] = round(time.time() - t0, 1)
    return rep


# ---------- other boxes ----------

def export_since(home, since, box=None):
    """JSON lines of this box's sessions updated after `since` (with messages and tool calls), for `box me pull`."""
    con = connect(db_path(home), readonly=True)
    con.row_factory = sqlite3.Row
    box = box or socket.gethostname()
    for row in con.execute("SELECT * FROM sessions WHERE box=? AND updated>? ORDER BY updated", (box, since)):
        d = dict(row)
        d["messages"] = [dict(r) for r in con.execute(
            "SELECT seq,ts,role,text FROM messages WHERE sid=? ORDER BY seq", (d["id"],))]
        d["tools"] = [dict(r) for r in con.execute(
            "SELECT seq,ts,name,input,output,dur_s,is_error FROM tool_calls WHERE sid=? ORDER BY seq", (d["id"],))]
        yield json.dumps(d, ensure_ascii=False)


def import_lines(con, lines):
    n = 0
    cols = ["id", "box", "agent", "session", "parent", "path", "cwd", "title", "model", "started", "ended", "wall_s",
            "active_s", "idle_gaps", "prompts", "tool_calls", "tool_s", "errors", "interrupts", "compactions",
            "out_tokens", "cache_read_tokens", "updated"]
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        sid = d["id"]
        with con:
            con.execute("DELETE FROM messages WHERE sid=?", (sid,))
            con.execute("DELETE FROM tool_calls WHERE sid=?", (sid,))
            con.execute(f"INSERT OR REPLACE INTO sessions VALUES({','.join('?' * len(cols))})",
                        [d.get(c) for c in cols])
            con.executemany("INSERT INTO messages(sid,seq,ts,role,text) VALUES(?,?,?,?,?)",
                            [(sid, m["seq"], m["ts"], m["role"], m["text"]) for m in d.get("messages", [])])
            con.executemany("INSERT INTO tool_calls(sid,seq,ts,name,input,output,dur_s,is_error) "
                            "VALUES(?,?,?,?,?,?,?,?)",
                            [(sid, t["seq"], t["ts"], t["name"], t["input"], t["output"], t["dur_s"], t["is_error"])
                             for t in d.get("tools", [])])
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


def keep_history(home):
    """Agents keep their transcripts forever: Claude Code's cleanupPeriodDays (default 30 days) and T3's usage
    history retention. Edits only those keys, only when lower; never rewrites a file it can't parse.
    Returns the list of files changed."""
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
        tmp = path + ".sbme-tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2)
            f.write("\n")
        if os.path.exists(path):
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        os.replace(tmp, path)
        changed.append(rel)
    return changed


# ---------- reading ----------

def stats(home=None):
    path = db_path(home)
    if not os.path.exists(path):
        return {"exists": False}
    con = connect(path, readonly=True)
    q = lambda sql: con.execute(sql).fetchall()  # noqa: E731
    size = sum(os.path.getsize(path + ext) for ext in ("", "-wal") if os.path.exists(path + ext))
    out = {"exists": True, "path": path, "size_mb": round(size / 1e6, 1),
           "sessions": q("SELECT count(*) FROM sessions")[0][0],
           "messages": q("SELECT count(*) FROM messages")[0][0],
           "tool_calls": q("SELECT count(*) FROM tool_calls")[0][0],
           "by_box_agent": {f"{b} {a}": n for b, a, n in q(
               "SELECT box, agent, count(*) FROM sessions GROUP BY 1,2 ORDER BY 3 DESC")},
           "first": q("SELECT datetime(min(started),'unixepoch','localtime') FROM sessions")[0][0],
           "last": q("SELECT datetime(max(started),'unixepoch','localtime') FROM sessions")[0][0]}
    con.close()
    return out


def search(home, words, limit=20):
    con = connect(db_path(home), readonly=True)
    rows = con.execute(
        "SELECT datetime(m.ts,'unixepoch','localtime'), s.box, s.agent, m.role, coalesce(s.title,''), "
        "snippet(messages_fts, 0, '[', ']', '…', 24) FROM messages_fts JOIN messages m ON m.id=messages_fts.rowid "
        "JOIN sessions s ON s.id=m.sid WHERE messages_fts MATCH ? ORDER BY m.ts DESC LIMIT ?", (words, limit)).fetchall()
    con.close()
    return rows


def sql(home, query):
    con = connect(db_path(home), readonly=True)
    cur = con.execute(query)
    cols = [d[0] for d in cur.description or []]
    rows = cur.fetchall()
    con.close()
    return cols, rows
