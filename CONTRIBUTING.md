# Contributing to sharedbox

Thanks for helping! sharedbox is deliberately boring tech: stdlib Python 3.12+, bash and systemd.
No build step, no dependencies to install.

## Ground rules

- **Never break a live box.** The installer must stay additive and idempotent. It must never restart or
  edit sshd, tailscaled, networking or the firewall, and never reboot.
- **Root does as little as possible.** Anything that touches a friend's files runs *as that friend*
  (`setpriv`), never as root.
- **Friends are non-technical by default.** If a feature needs a terminal, it also needs a browser path
  or an agent path.
- **Stdlib only** in `lib/`. If you really need a dependency, open an issue first.
- **Both distros.** Package work goes through `lib/sbdistro.py`; don't call `apt` or `pacman` directly.

## Checking your change

```bash
uv run --no-project --with ruff ruff check --select E9,F lib/*.py lib/box lib/sharedbox-*
bash -n install.sh uninstall.sh
python3 -m unittest discover -s tests   # stdlib-only unit tests, no root or network
python3 tests/e2e_gate.py .              # the real gate against fake logins and backends
# the real gate + web pages in Chromium: every link, button and form, per persona (--quick: one level deep)
uv run --no-project --with playwright python tests/e2e_browser.py . --shots /tmp/shots
sudo ./install.sh            # dry run: read what it would do
tests/container/run.sh       # install, update and uninstall in throwaway Ubuntu 24.04 + Debian 13 containers
```

Then test on a throwaway VM (Ubuntu 24.04 or Arch/Omarchy) with `--apply`, and walk through the
join page with a second Tailscale account.

## Commits and PRs

- One logical change per commit, with the subject prefixed by area: `web:`, `box:`, `admin:`,
  `janitor:`, `tools:`, `install:`, `tripwire:`, `rules:`, `docs:`.
- Say in the PR what you tested and on which distro.

## The design rule: agents do the work

People using treehouse (owners and friends) should never set things up by hand. Their AI agent, on the box or
on their own laptop, does ~all of it; the human only clicks a link or types a code they were emailed.

- Design the agent path first: a copyable prompt plus non-interactive commands with `--json` output.
  Then the smallest possible human step.
- No screens that ask people to paste tokens, open a terminal, run sudo, or know what SSH is.
- The website is a dashboard first. Every page needs an obvious next step; no dead ends, no jargon.
- Agent rules stay lean (`share/AGENTS.md`, ~1k tokens, loaded every session); details go in
  `share/HELP.md` (`box help <topic>`), read on demand.
- Before shipping, walk the flow as a non-technical friend and as their agent.
