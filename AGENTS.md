# Working on treehouse (for coding agents)

treehouse (commands and paths still say `sharedbox`) turns one Linux box into a shared machine for friends'
AI coding agents. It is live on the owner's box right now, so mistakes reach real people.
Human-facing rules are in CONTRIBUTING.md; this file is how an agent should work in this repo.

## Talking to the owner
- They are not a programmer and they reach you from another device. Short, plain replies. No jargon.
- Keep them in the loop: say in one line what you're doing before anything that takes more than a minute.
- **Never block the chat on tests.** Run anything slower than ~30s in the background, keep answering,
  report when it ends. If they send a new message, answer it; don't wait for the test first.
- Fix the thing they pointed at, then look for the same problem elsewhere (same helper, same pattern)
  and fix those too. Say what else you changed.

## Where things are
- `lib/sharedbox-web`: the website (dashboard `/_box/me`, owner pages, features). HTML is built as
  Python f-strings; shared CSS is the `CSS` string near the top. Reuse its classes (`card`, `tile`, `note`,
  `pill`, `btn`) rather than inline styles. Result banners after a button press go through `notice()`.
- `lib/sharedbox-gate`: the public front door (proxy, logins, routing to T3).
- `lib/sharedbox-admin`: root-side actions the website and `box` ask for (messages it returns show up
  on the site verbatim, so write them as finished sentences).
- `lib/box`: the CLI friends' agents use. `share/AGENTS.md` + `share/HELP.md`: the rules every agent on the
  box gets (keep AGENTS.md lean, ~1k tokens; details go in HELP.md).
- `install.sh`: must stay additive and idempotent. Never restart sshd/tailscale/network/firewall, never reboot.

## Checking a change
- Quick, inline: `python3 -c "import ast; ast.parse(open('lib/sharedbox-web').read())"`, the ruff line and
  unit tests from CONTRIBUTING.md.
- UI changes: render the piece you changed and look at a screenshot (dark and light, 1280 and 390 wide)
  before saying it's fixed. Chromium is at `~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome`, needs `--no-sandbox`.
- Full e2e (`tests/e2e_browser.py`, several minutes): background only, before a deploy, not after every edit.
- Say plainly what you did not check.

## UI taste (the owner notices)
- No emojis, no "AI-sounding" copy, no decorative highlight strips. Sentences start with a capital letter.
- Icons must not depend on fonts: draw small marks as inline SVG, not ✓/✗ characters.
- Don't show a success check for something that has only started; use the in-progress state.
- Every page needs an obvious next step. Nothing should require a terminal, sudo, or pasting a token.

## Shipping
- Commit with an area prefix (`web:`, `box:`, `admin:`, ...). Push only when asked.
- The agent account has no sudo. Deploying is the owner's admin running one line from
  `/tmp/sharedbox-install`; prepare that, never ask for root yourself.
