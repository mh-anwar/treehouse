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
sudo ./install.sh            # dry run: read what it would do
```

Then test on a throwaway VM (Ubuntu 24.04 or Arch/Omarchy) with `--apply`, and walk through the
join page with a second Tailscale account.

## Commits and PRs

- One logical change per commit, with the subject prefixed by area: `web:`, `box:`, `admin:`,
  `janitor:`, `tools:`, `install:`, `tripwire:`, `rules:`, `docs:`.
- Say in the PR what you tested and on which distro.
