"""Writes docs/glossary.md from sbcommon.GLOSSARY (the one list of words the dashboard, `box help words` and agent
rules use). Run it after changing GLOSSARY: python3 tests/glossary_doc.py; test_vocabulary checks they match."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import sbcommon as sb  # noqa: E402

RULES = """
## Rules for anything people read

- Use these words and no others for these things: the dashboard, `box help`, `box` messages, agent rules, emails.
  Say "machine", never "box", in text people read (`box` stays the command's name). Say "server" for something
  running with a link, "database or container" for Docker things, "agent session" for one chat with an agent.
- Name machines by their real name (the name in BOXES, else the hostname), the same on every page, and add
  "(this machine)" for the one the page comes from.
- "Sign in" and "Sign out", never "log in"/"log out". Say what you sign out of: "Sign out of Claude on treebox",
  "Sign out of treehouse on this device".
- T3 is "T3". The button that opens it is "Open T3"; the phone and laptop app is "the T3 app" ("T3 Code" only when
  pointing at the app stores). Google's agent is "Google Gemini" with "Antigravity" explained where it appears.
- Every number says whose it is: "Your memory", "Whole machine", "People's programs". Sizes are "9.6 GB",
  "600 MB", never "9.6G" or "0B". Times carry a time zone.
- One state word per metric: Normal, Busy or Overloaded.
- No internal names in user-facing text: config keys (ADMIN_USERS, JOIN_MODE, CF_SEAT_LIMIT, ...), `sharedbox`,
  `sharedbox-admin`, "provisioner", `sudo` commands, raw error strings. tests/test_ui_lint.py enforces this.
- Messages say what happened, then what to do next. Internals go to the issue log, never to the person.
"""


def render():
    out = ["# treehouse glossary", "",
           "Generated from `GLOSSARY` in lib/sbcommon.py by tests/glossary_doc.py. The dashboard's Home tab",
           "(\"What the words here mean\") and `box help words` show the same list.", ""]
    for term, text in sb.GLOSSARY:
        out.append(f"- **{term}**: {text.format(machine='treebox')}")
    return "\n".join(out) + "\n" + RULES


if __name__ == "__main__":
    with open(os.path.join(ROOT, "docs", "glossary.md"), "w") as f:
        f.write(render())
    print("wrote docs/glossary.md")
