"""No internal names in what people read: the dashboard's text (lib/sharedbox-web), what `box` prints or answers
(lib/box), and the answers root sends back to pages and commands (lib/sharedbox-admin). Config keys, `sudo` commands,
"sharedbox", "provisioner" and raw error strings belong in logs and code, never in a sentence for a person
(docs/glossary.md). Stdlib only:  python3 -m unittest discover -s tests -v"""

import ast
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
BANNED = re.compile(r"\b(ADMIN_USERS|CF_SEAT_LIMIT|JOIN_MODE|REQUEST_DENY|DISK_BUDGET_GB|PUBLIC_MODE|CF_OWNER_EMAILS|"
                    r"OWNER_T3_USER|provisioner|sharedbox-admin|sharedbox|sudo|strerror|Errno)\b|"
                    r"\b(this|the|your|other|every) box(es)?\b", re.I)
NOT_FOR_PEOPLE = {"CSS", "DASH_JS", "START_JS", "FAVICON"}  # styles and scripts, not text


def text_parts(node):
    """The literal text inside a str constant or an f-string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
    return []


def prose(s):
    return " " in s.strip() and re.search(r"[A-Za-z]{3}", s)


def strings_in(node):
    out = []
    for n in ast.walk(node):
        out += [s for s in text_parts(n) if prose(s)]
    return out


def is_log(call):
    """print(..., file=sys.stderr) and the service's own start-up lines: logs, not text for a person."""
    return isinstance(call.func, ast.Name) and call.func.id == "print" and (
        any(k.arg == "file" for k in call.keywords) or LOG_PRINT)


LOG_PRINT = False


def web_texts():
    with open(os.path.join(LIB, "sharedbox-web")) as f:
        tree = ast.parse(f.read())
    skip = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in NOT_FOR_PEOPLE for t in n.targets):
            skip |= {id(x) for x in ast.walk(n)}
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print":
            skip |= {id(x) for x in ast.walk(n)}  # this service's log lines
        if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Module)) and n.body and isinstance(n.body[0], ast.Expr) \
                and isinstance(n.body[0].value, ast.Constant):
            skip.add(id(n.body[0].value))  # docstrings
        if isinstance(n, ast.Subscript):  # CFG["KEY"], dict["key"]: code, not text
            skip |= {id(x) for x in ast.walk(n.slice)}
    out = []
    for n in ast.walk(tree):
        if id(n) in skip:
            continue
        if isinstance(n, ast.JoinedStr):
            skip |= {id(v) for v in n.values}
        out += [(n.lineno, s) for s in text_parts(n) if prose(s)]
    return out


def said_texts(path, keys=("error", "message", "note", "hint"), calls=True):
    """What a CLI or root answer says to a person: print()/sys.exit()/fail() arguments (calls=True: lib/box, which
    friends' agents relay), and "error"/"message" values (root's answers, which pages and `box` show as they are).
    sharedbox-admin's own prints go to the owner's root terminal, so they aren't checked."""
    with open(os.path.join(LIB, path)) as f:
        tree = ast.parse(f.read())
    out = []
    for n in ast.walk(tree):
        if calls and isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name in ("exit", "fail") or name == "print" and not any(
                    isinstance(k.value, ast.Attribute) and getattr(k.value, "attr", "") == "stderr" and
                    path != "box" for k in n.keywords):
                for a in n.args:
                    out += [(n.lineno, s) for s in strings_in(a)]
        if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call) and isinstance(n.exc.func, ast.Name) \
                and n.exc.func.id in ("FriendError", "InviteError", "RequestError"):  # shown as the answer's error
            for a in n.exc.args:
                out += [(n.lineno, s) for s in strings_in(a)]
        if isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value in keys:
                    out += [(n.lineno, s) for s in strings_in(v)]
    return out


# Lines that really must name these: what `box` says to the owner's own agent about the owner's own machine setup,
# where the command is the only way to do it. Each stays short and goes to the owner, never to a friend.
OWNER_ONLY = re.compile(r"owner's (own )?agent|on the machine itself|OWNER_T3_USER|ssh_keys|fed-init")


class NoInternalNames(unittest.TestCase):
    def check(self, rows, where):
        bad = [(ln, s.strip()[:120]) for ln, s in rows if BANNED.search(s) and not OWNER_ONLY.search(s)]
        self.assertEqual(bad, [], f"internal names in what people read ({where}); see docs/glossary.md")

    def test_dashboard(self):
        self.check(web_texts(), "lib/sharedbox-web")

    def test_box_messages(self):
        self.check(said_texts("box"), "lib/box")

    def test_root_answers(self):
        self.check(said_texts("sharedbox-admin", calls=False), "lib/sharedbox-admin")


if __name__ == "__main__":
    unittest.main()
