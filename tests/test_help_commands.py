"""The agent rules and help name only real commands (share/HELP.md, share/AGENTS.md, lib/box): every `box ...` in
them parses with box's own parser, every quoted label or message is really in the code, `box help <unknown>` exits 2,
internal `_` commands never show, every command has a one-line description. Stdlib only:
    python3 -m unittest discover -s tests -v"""

import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import re
import shlex
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
_home, os.environ["HOME"] = os.environ.get("HOME"), tempfile.mkdtemp(prefix="sbhelp-")


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    argv, sys.argv = sys.argv, [os.path.basename(path)]
    try:
        loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


box = load("box_helpcmds", os.path.join(LIB, "box"))
if _home is not None:
    os.environ["HOME"] = _home


def read(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return f.read()


def spans():
    """(file, `box ...` code span) in the agent rules and help."""
    for rel in ("share/HELP.md", "share/AGENTS.md"):
        for m in re.finditer(r"`(box [^`]+)`", read(rel)):
            yield rel, m.group(1)


def concrete(span):
    """A doc span as one argv to try: the first of a|b choices, [optional] parts dropped, placeholders filled in."""
    t = re.sub(r"\|\s+", "|", span)                   # a choice list wrapped onto the next line
    t = re.sub(r"\[[^\]]*\]", " ", t)                 # [--json], [NAME]
    t = re.sub(r"\.\.\.|…", " ", t)
    t = re.sub(r"<([a-z-]+)(\|[a-z-]+)+>", r"\1", t)  # <slow|broken|...>: the first choice
    t = t.split(" < ")[0]                              # shell plumbing in examples
    try:
        words = shlex.split(t)
    except ValueError:
        return None
    out = []
    for w in words[1:]:
        w = w.split("|")[0]
        if w == "--":
            out.append(w)
            out.append("true")
            break
        if re.fullmatch(r"<[^>]*>|[A-Z][A-Z_-]*|[A-Z]+=[A-Z]+", w):  # NAME, <topic>, KEY=VALUE
            w = {"<topic>": "servers", "N": "1"}.get(w, "x")
        out.append(w)
    return out


class EveryDocumentedCommandParses(unittest.TestCase):
    def test_subcommands_exist(self):
        cmds = set(box.parser_commands()) | {"team"}
        bad = sorted({(rel, s) for rel, s in spans() if s.split()[1] not in cmds and not s.startswith("box <")})
        self.assertEqual(bad, [], "help names a `box` command that doesn't exist")

    def test_spans_parse(self):
        p = box.build_parser()
        bad = []
        for rel, span in spans():
            argv = concrete(span)
            if not argv or argv[0] in ("team", "<topic>", "<command>") or len(span.split()) == 2:
                continue  # `box serve` on its own names the command (test_subcommands_exist checks it)
            for _ in range(4):  # a placeholder where a choice goes: try the parser's first choice instead
                err = io.StringIO()
                try:
                    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                        p.parse_args(argv)
                    break
                except SystemExit as ex:
                    if ex.code in (0, None):
                        break
                    m = re.search(r"invalid choice: 'x' \(choose from ([^,)]+)", err.getvalue())
                    if not m or "x" not in argv:
                        bad.append((rel, span, err.getvalue().strip().splitlines()[-1:]))
                        break
                    argv[argv.index("x")] = m.group(1).strip("'")
        self.maxDiff = None
        self.assertEqual(bad, [], "these `box ...` lines in the help don't parse")

    def test_quoted_labels_and_messages_exist(self):
        """A label or message the help quotes ("AI accounts on each machine") must be one the code really shows."""
        examples = {"Telegram bot for the study group", "Discord bot that reminds the study group about deadlines",
                    "run X on my treehouse machine", "Mohammad: in class until 15:00, active 2h ago.",
                    "active on the machine"}
        code = "".join(read(os.path.join("lib", f)) for f in os.listdir(LIB)
                       if os.path.isfile(os.path.join(LIB, f))).lower()
        bad = [q for q in re.findall(r'"([^"`\n]{20,})"', read("share/HELP.md"))
               if "<" not in q and "--" not in q and q not in examples and q.lower() not in code]
        self.assertEqual(bad, [])


class CommandLine(unittest.TestCase):
    def test_unknown_help_topic_exits_2(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            box.cmd_help(argparse.Namespace(topic="no-such-topic"))
        self.assertEqual(cm.exception.code, 2)

    def test_generated_topics(self):
        t = box.help_topics()
        if not t:
            self.skipTest("no HELP.md in this environment")
        self.assertIn("Machine:", t["words"])
        self.assertIn("box status", t["commands"])
        self.assertNotIn("_portal", t["commands"])
        self.assertNotIn("box heavy", t["commands"])
        self.assertNotRegex(t["boxes"], r"i3|Dell|16 GB|Wi-Fi")  # no hard-coded hardware: it comes from BOXES

    def test_usage_shows_main_commands_only(self):
        out = box.build_parser().format_help()
        self.assertNotIn("_portal", out)
        self.assertNotIn("heavy ", out.split("main commands:")[1].split("Every command")[0])
        self.assertLess(out.count("\n    "), 16)

    def test_every_command_has_a_line(self):
        cmds = box.parser_commands()
        main = dict(box.MAIN_COMMANDS)
        missing = sorted(k for k, v in cmds.items() if not v and k not in main and not k.startswith("_")
                         and k not in box.HIDDEN_COMMANDS)
        self.assertEqual(missing, [], "commands with no description in `box help commands`")


if __name__ == "__main__":
    unittest.main()
