# Changelog

Versions follow [semantic versioning](https://semver.org). A box updates from a git checkout of `main`
(`box friends update`, or the owner's Update button), so most boxes run whatever `main` was at their last
update. The version tags mark points worth naming.

## 1.0.0 (2026-11-16)

The first public release. Earlier versions ran on the author's own machines.

### What a box does

- Friends sign in from a browser through Cloudflare Access, or join over Tailscale. Each one gets an account,
  a dashboard at `/_box/me` and their own T3 Code, with Claude Code, Codex, opencode, Node, Python, uv, Go
  and the usual build tools installed.
- Every account, the owner's included, gets the same limits: no CPU cap, 16G soft and 17G hard RAM,
  2048 processes and a 40G disk budget. `box boost` raises RAM to 28G for 4 hours when the machine has
  it free.
- A janitor cleans unused caches and archives git projects untouched for 20 days, and only when everything
  in them is pushed. `box restore` brings one back within 14 days.
- `box serve` keeps a friend's server running across reboots. `box request install` installs ordinary
  packages right away and emails the owner about the rest.
- A weekly health email and hourly urgent checks cover disk, failed services, SMART, pending updates and
  problems agents report with `box issue`.
- `box tune` checks the machine's kernel, memory, disk and network settings. The ones every box should have
  ship with treehouse and are applied at install.
- Several boxes can share work. `box boxes` shows their load, `box run --anywhere` sends a command to the
  box with the most room, and federation keeps the same friend accounts on every box. A team made with
  `box team` on one box can be used from any of them.

### Changes since the private versions

- Removed: the decoy-port trap and canary tripwire, shared servers, the AI review of package requests,
  agent version pins, Tailscale Funnel public mode and the signup approval queue. Public access now needs
  Cloudflare.
- The owner's own T3 Code can move under `box t3` with `box t3 switch`, which waits until no agent is
  working. If the new T3 fails, it restores the database and the old service, and a T3 that crashes on
  start is caught in seconds.
- `go install` and `cargo install` put programs in `~/.local/bin`, which is on PATH, and rustup has stable
  as its default toolchain, so both work the way the agent rules say. Existing settings are left alone.
- Node.js 22 comes from NodeSource where the distro ships an older one (Ubuntu 24.04 has 18).
- A new box no longer goes to sleep: suspend and lid-close sleep are turned off (`TUNE_KEEP_AWAKE=0`
  keeps the system's own settings). On a machine with less than 20 GB of RAM a new install scales the
  memory limits to fit. The installer's preflight explains in plain words what the hardware means and stops
  on a 32-bit PC, where the agents can't run.
- Arch Linux is supported and tested in CI. On Arch the installer adds `pacman-contrib` so the weekly report
  can count pending updates.
- Tests: every pull request installs, updates and uninstalls treehouse in Ubuntu 24.04, Debian 13 and Arch
  Linux containers that boot systemd. Two more jobs on Debian 13 install every coding agent and toolchain
  and check each one as a friend, and walk `box t3 switch` through a refused version, a rollback, waiting
  for a busy agent and the final move. `FROM=<commit> tests/container/run.sh`
  tests an upgrade from an older version.
