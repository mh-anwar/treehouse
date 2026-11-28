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
- Friends' T3 Code: `sb-t3.socket` holds their T3 port (first of their block) and starts T3 on the first connection;
  T3 listens on the block's last port on the same Tailscale IP (never loopback: T3 then revokes pairings and
  Connect). The owner's T3 runs the same way (OWNER_T3_PORT, default 3773, held by the socket) once the tick has
  moved it off T3's own `t3code.service`. `sharedbox-janitor tick` (every 10 min) runs `box _t3-tick` as each of
  those accounts: restarts T3 onto the box's version (T3_CHANNEL, stable by default; /etc/t3code/policy.json makes
  T3 refuse nightlies) once nothing has run in it for T3_RESTART_QUIET_MIN, puts it to sleep after
  T3_IDLE_STOP_HOURS with no client and no work (never in hackathon mode or with scheduled tasks on; the socket
  wakes it), and starts it for scheduled tasks. It also ends expired `box boost`s and refreshes agent rules.
- Owner rule: nothing may make agents slower. Equal limits for everyone (owner included), no CPU caps, no naps of
  anything in use (T3's idle sleep only fires with no client and no work), no build throttling; unpushed work is
  never archived or deleted.

## Checking a change
Run only what the change needs:
- Every change (~15 s): the ruff line and unit tests from CONTRIBUTING.md, plus `python3 tests/e2e_gate.py .`.
- Look-only changes (layout, CSS, copy), ~15 s: `tests/e2e_browser.py . --shots DIR --shots-only`, then
  actually look at the screenshots that changed (dark and light, 1280 and 390 wide) before saying it's fixed.
- Links, buttons, forms or flows changed: the full `tests/e2e_browser.py` (~8 min), in the background only,
  once before the deploy, not after every edit. Chromium: `~/.cache/ms-playwright/chromium-*/`, `--no-sandbox`.
- Say plainly what you did not check.

## Making boxes efficient
Every treehouse box should feel fast even when everyone is busy, and what one box learns, every box gets:
- Measure first and after (numbers in the commit message): `box tune`, `/proc/pressure/*`, the e2e benches.
- A setting that helps every box ships in the repo, never only on one machine: kernel settings in
  `etc/sysctl-*.conf` (one comment per setting saying why; install.sh installs every file there), runtime steps
  in `sharedbox-tune apply` (runs at install and every boot), and a matching check in `sharedbox-tune` CHECKS so
  `box tune` reports it. Must be safe live: no network restart, no reboot, nothing that drops security.
- Machine-specific findings (desktop services, swap size, CPU power mode) are "owner" checks: suggestions only.
- Nothing turns off things people may use to save resources (no Docker naps). The one idle timer is T3's sleep
  above: it never fires while anything runs, and the next visit wakes T3 in a few seconds.

## UI taste (the owner notices)
- No emojis, no "AI-sounding" copy, no decorative highlight strips. Sentences start with a capital letter.
- Icons must not depend on fonts: draw small marks as inline SVG, not ✓/✗ characters.
- Don't show a success check for something that has only started; use the in-progress state.
- Every page needs an obvious next step. Nothing should require a terminal, sudo, or pasting a token.

## Shipping
- Commit with an area prefix (`web:`, `box:`, `admin:`, ...). Push only when asked.
- The agent account has no sudo; never ask for root. To deploy: push to main, `git pull` the checkout in
  `UPDATE_SOURCE` (see /etc/sharedbox/sharedbox.conf), then `box friends update` (or the owner's "Update
  treehouse" button). It installs that checkout's committed main; check `/var/lib/sharedbox/update.json`.
  Changes to the installer itself that need root beyond that still go through the owner.
