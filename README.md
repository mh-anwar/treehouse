# sharedbox

Turn one Linux machine into a shared box for friends and their AI coding agents
(Claude Code, Codex, opencode, …). There's no per-person admin work, and nobody
can starve the machine.

- **Self-service signup.** Friends open `http://<box>:8090/join` over Tailscale. They're identified
  by their Tailscale login, paste an SSH key, and get an account. You don't run `useradd` 20 times.
- **Ready to work on first login.** Claude Code, Codex and opencode are preinstalled system-wide,
  along with node, Python + uv, git + gh, Go, rustup, build tools, headless-browser libraries, and
  optionally a JDK + Android SDK for APK builds. `box setup` walks a new user through git identity,
  GitHub login, and agent login in about 2 minutes. `npm i -g` works without sudo, and agents are updated daily.
- **Fair-share limits.** Every friend gets a cgroup with a CPU cap and low CPU weight, soft and hard RAM
  limits, a process cap, and lower IO priority. Friends' processes are first in line for the OOM killer,
  so the owner's sessions, sshd, and Tailscale keep running even when someone compiles an APK with `-j64`.
- **Janitor.** It wipes stale caches, deletes build outputs (`node_modules`, `build/`, `target/`, `.venv`, …)
  in idle projects, removes projects nobody touched for 7 days, and enforces a per-user disk budget.
  Pending deletions are announced days in advance. A project with a running process inside is never
  deleted.
- **Agents are told the rules automatically.** A managed block of house rules is kept in
  `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.config/opencode/AGENTS.md`,
  and `~/AGENTS.md`. Current notices (e.g. "project X will be deleted Friday") are added when relevant.
  Claude Code also gets a live `box status` at every session start through a hook.
- **`box` CLI** for users and agents:
  - `box status`: your usage, your limits, and how busy the box is
  - `box setup` / `box agents`: onboarding wizard and agent login status
  - `box t3`: the user's own [T3 Code](https://t3.codes) web UI on their reserved port, with a pairing link
  - `box request "..."`: ask the owner for a system package. Agents are told to do this instead of trying sudo
  - `box issue "..."`: agents log anything about the box that slowed them down or failed (missing tools, limits,
    flaky installs, confusing rules) to `~/.sharedbox/issues.jsonl`, without being asked. `box` also logs
    RAM/CPU throttling, OOM kills and long `box heavy` queue waits on its own. The owner sees everyone's in
    `sharedbox-admin issues` and in the weekly health email
  - `box heavy -- <cmd>`: a box-wide queue for builds, run at low priority with capped parallelism
  - `box serve add`: long-running servers that survive logout and reboot
  - `box ports`: your port range
- **Dashboard** at `http://<box>:8090/`: live CPU and RAM per person with 3-hour sparklines, disk vs. budget,
  running servers, warnings, and recent janitor activity.
- **Reboot awareness**: agents are told the box may reboot at `REBOOT_TIME` after security updates
  (Ubuntu's unattended-upgrades does the actual updating and rebooting, nightly at `APT_UPGRADE_TIME`), and `box status` warns when a
  reboot is scheduled. Optional fixed reboots (`SCHEDULED_REBOOT_DAYS`, default Wed and Sun at 04:00)
  clear memory leaks and servers still running on old libraries.

Stdlib Python plus systemd. No containers, no database, no extra daemons beyond the dashboard.

## Requirements

- Linux with systemd and cgroup v2 (Ubuntu 22.04+, Debian 12+, Fedora, …), Python ≥ 3.12
- Tailscale, which handles identity for signup and keeps the box off the public internet
- sshd that only allows key auth. Ideally use `AllowGroups <group>` and point `SSH_GROUP` at that group.

## Install

```bash
git clone <this repo> && cd sharedbox
sudo ./install.sh                                   # dry run: shows every action, changes nothing
sudo ./install.sh --apply --admins alice,bob        # your own accounts: shown, never limited
```

By default `--apply` also runs `sharedbox-tools install`, which only adds apt and npm packages (no system
upgrade). Skip it with `--no-tools`. For APK builds, set `TOOLS_ANDROID=1` in
`/etc/sharedbox/sharedbox.conf` and rerun `sudo sharedbox-tools install`. Doing so accepts the Android SDK
license on your behalf.

The installer is additive and safe to run on a live machine. It **never** restarts or edits sshd,
tailscaled, networking, or the firewall. It never changes limits for existing users, and it never
reboots. It adds files, a `friends` group, and a `sharedbox` system user, runs `systemctl daemon-reload`,
and starts its own units. It also moves Ubuntu's daily security-update run to `APT_UPGRADE_TIME`
(03:00) with a timer drop-in, so updates land before a 04:00 auto-reboot. `./uninstall.sh` reverses it and keeps friend accounts unless you pass
`--remove-users`.

## Onboarding a friend

1. In the Tailscale admin console: **Machines → your box → Share…** and send them the invite.
   This is the only per-person step, and it's Tailscale's, not ours.
2. Send them `http://<box>:8090/join`. They pick a username and paste their public key.
3. They run `ssh <username>@<box>`, then `box setup`, then `box t3` for a web UI, or `claude` / `codex` / `opencode` in a terminal.

Each new home comes preconfigured: agent rule files, a Claude Code SessionStart hook, Gradle limits,
`~/.npmrc` (global installs go to `~/.local`), and a tmux config. The first login shows a short welcome.

Set `JOIN_MODE=approve` to review signups (`sharedbox-admin pending|approve|deny`), or `closed`.

## Administration

```
sharedbox-admin list                                   users, disk, ports, Tailscale login
sharedbox-admin limits NAME MEMORY_MAX=10G CPU_QUOTA=400% DISK_BUDGET_GB=50
sharedbox-admin suspend NAME | resume NAME             cut ssh + stop everything / undo
sharedbox-admin kill NAME                              stop all their processes (services come back)
sharedbox-admin remove NAME --archive                  tarball the home, then delete the account
sharedbox-janitor run --dry-run                        see what the janitor would delete right now
sharedbox-admin requests [--done FILE]                 what friends/agents asked for via `box request`
sharedbox-admin issues [--days N] [--user U] [--raw]   friction agents logged via `box issue`, grouped
sharedbox-tools add apt ffmpeg                         install an extra package for everyone (remembered)
sharedbox-tools status | update                        installed versions / update agents now
sharedbox-tools remove npm|apt NAME                    uninstall a package and stop installing it
```

All defaults live in `/etc/sharedbox/sharedbox.conf` and are documented inline.

## How the limits work

| | Default per friend | Mechanism |
|---|---|---|
| CPU | 3 cores max, weight 20 (owner = 100) | `CPUQuota`, `CPUWeight` on `user-UID.slice` |
| RAM | 5G soft (reclaim/throttle), 6G hard (OOM inside their cgroup) | `MemoryHigh`, `MemoryMax`, `MemorySwapMax` |
| Processes | 2048 | `TasksMax` (fork bombs stay contained) |
| IO | weight 20 | `IOWeight` |
| OOM priority | +500 | `user@UID.service` drop-in plus a 1-minute guard for SSH sessions |
| Disk | 20G budget, soft-enforced | janitor (see below) |
| Heavy jobs | 2 at a time box-wide | `box heavy` flock slots plus `nice`/`ionice` |
| Build parallelism | 2 | `MAKEFLAGS`, `CARGO_BUILD_JOBS`, `~/.gradle/gradle.properties` |

On contention the kernel shares CPU by weight. With 5 friends compiling at full tilt, the owner
still gets about half the machine and system services (sshd, tailscaled) are untouched.

RAM can't really be "cleaned". Instead, `MemoryHigh` makes the kernel reclaim from a heavy user's
own page cache and throttle them before anyone else feels it, Gradle/Kotlin daemons are configured
to exit after 15 idle minutes, and the OOM killer picks friends' processes first.

## How the janitor works

Every 6 hours (at idle CPU and IO priority) it measures each home with `du` as root, then runs the
cleanup **as that user** (`setpriv`). Root never deletes anything inside a user's home, so symlink
tricks can't redirect deletions.

| What | When |
|---|---|
| Cache entries (`~/.cache/*`, npm, Gradle, Maven, Cargo, Go, bun, pnpm, uv, …) | unused for 7 days, or immediately when over budget |
| Build outputs inside `~/projects/*` | project idle for 3 days, or immediately when over budget |
| Old T3 Code versions in `~/.t3/runtime/versions` | all but the 2 newest (never one that's running) |
| `/tmp` and `/var/tmp` entries owned by the user | unused for 2 days (tmux/ssh sockets are skipped) |
| A whole `~/projects/<name>` | not touched at all for 7 days (no file read or written, no process running from it, no `box serve` service pointing at it, even a stopped one). Shown as "will be deleted on …" for 3 days first |
| Oldest idle projects | still over budget 24 h after the warning, deleted until under budget |
| `~/services`, `~/keep`, dotfiles | never deleted automatically (they count toward the budget) |

When the disk drops below 20% free, all TTLs are halved. Below 10%, caches are wiped and the
over-budget grace period drops to zero.

"Unused"/"touched" means the newest file mtime/atime or directory mtime in the tree, so reading a file counts. Directory atime is ignored
because listing a directory (including our own scans) updates it.

## Layout

```
lib/sbcommon.py          config, registry, cgroup/PSI reads, validation
lib/sbclean.py           the per-user cleanup pass (runs as the user)
lib/box                  user CLI
lib/sharedbox-admin      root: accounts, limits, join-request provisioning
lib/sharedbox-janitor    root: disk measurement + scheduling cleanup; OOM guard
lib/sharedbox-web        unprivileged: dashboard + join page
lib/sharedbox-tools      root: preinstalled agents/toolchains, weekly agent updates
share/AGENTS.md          house rules template given to agents
share/skel/              per-user defaults (gradle.properties, Claude hook, env)
systemd/                 units and timers
```

### Security notes

- The web service runs as an unprivileged, sandboxed user. It drops join requests into a spool
  directory. A root path unit re-verifies the caller's identity with `tailscale whois` and validates
  everything again before creating an account. Requests from the box itself are rejected, so a local
  user can't sign up as the owner.
- Join forms carry an HMAC token, and cross-origin POSTs are refused.
- Friend homes are `0700`. Friends get no sudo and no Docker group. Docker group membership is root.
- The dashboard shows usernames and resource numbers, never process command lines.

## Known limits and roadmap

- **Disk is soft-enforced.** A user can exceed the budget until the next janitor pass. Hard quotas
  need filesystem support (ext4 `quota` feature or XFS project quotas), which can't be enabled on a
  mounted root without downtime. Planned: optional quota support when available.
- **Process visibility.** Users can see each other's process list (`ps aux`), which is fine among friends.
  Agents are still told not to pass secrets as command-line arguments.
- **Port ranges are advisory.** Planned: optional enforcement with `SocketBindAllow=` on each slice.
- Ideas: public URLs for `box serve` via Tailscale Funnel, Discord/ntfy notifications for deletions,
  "night queue" for heavy jobs, Prometheus metrics, GPU sharing, auto-sleep for idle servers.

## License

MIT
