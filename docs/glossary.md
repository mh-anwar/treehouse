# treehouse glossary

Generated from `GLOSSARY` in lib/sbcommon.py by tests/glossary_doc.py. The dashboard's Home tab
("What the words here mean") and `box help words` show the same list.

- **Machine**: A computer treehouse runs on, called by its name (for example treebox). Each machine has its own memory and disk, and your files stay on the machine where they were made.
- **Dashboard**: This website. Its tabs are Home, Messages, Servers, Machines and Account.
- **T3**: The chat app where you talk to your AI agent. Open it in this browser with Open T3, or use the T3 app on your phone or laptop (it's called T3 Code in the app stores).
- **AI account**: Your own Claude, ChatGPT or Google subscription. Agents use it to think. You need only one, and that company bills you for it, not treehouse.
- **Agent**: An AI coding assistant (Claude Code, Codex, or Google's Antigravity, which uses Gemini) that works on the machine for you. An agent session is one chat with it.
- **Server**: An app, site or bot of yours that keeps running on the machine, with its own link.
- **Group server**: A server the whole group shares. Anyone in the group can start, stop or change it.
- **Database or container**: A program your agent started with Docker, such as a Postgres or Supabase database. It keeps running until someone stops it.
- **Friend**: Someone with an account on the machine. The owner runs the machines and invites friends.
- **Request**: Something an agent asks the owner for: a program to install for everyone, or a setting to change.
- **Archive**: Where a project nobody opened for a while goes, once all its work is saved on GitHub. You can bring it back from your Account tab.

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
