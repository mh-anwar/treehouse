<div align="center">

# treehouse

### Build your friends an AI treehouse.

Turn a spare computer into a shared home for your friends' AI coding agents.
They join from a link, log in to Claude Code / Codex / opencode with a click,
and get a fair slice of the machine. You never touch a terminal for them.

`stdlib Python` · `systemd` · `cgroup v2` · no containers, no database

**Ubuntu / Debian** ✅  **Omarchy / Arch** 🧪 (experimental)

</div>

---

## Why

AI agents made "I'll just code on my laptop" expensive: builds, dev servers, and five agent
sessions at once. Meanwhile there's an old desktop in your closet with 32 GB of RAM doing nothing.

treehouse turns that machine into a box your friends can actually use, including the
non-technical ones:

1. You share the machine with them on Tailscale.
2. They open `http://<box>:8090/join`, pick a username, click **Create**.
3. They click **Log in with Claude** (or ChatGPT/Codex), click **Open T3 Code**, and they're chatting
   with an agent that runs on your box, with everything installed and the house rules already explained.

No SSH keys, no terminal, no "can you run this command for me".

## What you get

**For friends**
- 🌐 **Browser-only onboarding.** Signup, agent logins, GitHub login and a personal
  [T3 Code](https://t3.codes) web UI, all from one page (`/me`). Codes are detected automatically.
- 🤖 **Agents that know the house rules.** Every account's `CLAUDE.md` / `AGENTS.md` gets a managed block:
  limits, ports, what gets cleaned up, how to install things without sudo, and live notices
  ("project X will be deleted Friday", "the box reboots at 4 AM"). Agents are told to do setup
  themselves and never send a non-technical user to a terminal.
- 🧰 **Ready on first login.** Claude Code, Codex, opencode, node, Python + uv, git + gh, Go, rustup,
  build tools, headless-browser libs, optional JDK + Android SDK. `npm i -g` works without sudo.
- 💻 **Terminal still works.** Technical users can add an SSH key and use the `box` CLI.

**For you (the owner)**
- ⚖️ **Dynamic fair-share limits.** Per-friend cgroups: up to **15 GB / 5 cores while the box is quiet**,
  falling back to **7 GB / 3 cores when it's busy**. You and system services always win.
  Fork bombs, `-j64` builds and runaway agents stay inside their own slice.
- 🧹 **A janitor.** It cleans caches and build output in idle projects, deletes projects untouched for
  7 days (with days of warning), and enforces a per-user disk budget. Root never deletes inside a home;
  cleanup runs *as the user*.
- 📊 **Dashboard.** Live CPU/RAM per person with sparklines, disk vs. budget, running servers.
- 📬 **Health email.** A weekly report plus hourly urgent checks: disk, failed services, SMART, pending
  updates, security audit, and a section for **what slowed agents down**, logged by the agents themselves
  via `box issue`.
- 🪤 **Tripwire + trap.** Decoy secrets on disk: if anything opens them (say, a malicious npm package
  snooping), both you *and* the affected friend are told, by email, terminal banner and their agent.
  Decoy ports and an AI-crawler maze catch outside scanners.
- 🔁 **Self-updating.** Agents are updated daily. Optional weekly reboots clear leaks.

## Quick start

You need a Linux machine (Ubuntu 24.04 or Omarchy) and a coding agent that can reach it.
Then tell your agent:

```text
Set up treehouse on this machine. Follow https://github.com/mh-anwar/treehouse/blob/main/INSTALL.md
```

That's it. Your agent checks the machine, asks you three questions, installs everything, sets up email
alerts and a public join link, checks it all works, and hands you an invite message for your first friend.
The only things you do yourself are click "approve" on a login page and maybe type your sudo password.

<details>
<summary>Doing it by hand instead</summary>

```bash
git clone https://github.com/mh-anwar/treehouse && cd treehouse
sudo ./install.sh                                    # dry run: prints every action, changes nothing
sudo ./install.sh --apply --admins you --mail-to you@gmail.com
sudo sharedbox-admin invite                          # a message to paste to your first friend
```
</details>

The installer is additive and safe on a live machine. It **never** touches sshd, tailscaled, networking
or the firewall, never changes limits for existing users, and never reboots. `./uninstall.sh` reverses it.

**Requirements:** Linux with systemd + cgroup v2, Python ≥ 3.12, Tailscale on the server (friends don't
need it). Tested on Ubuntu 24.04; Omarchy/Arch support is new. Please [open an issue](../../issues) with
what breaks.

## How it works

```
 friend's browser ──Tailscale──▶ sharedbox-web (unprivileged, sandboxed)
                                     │  drops a request in a spool dir
                                     ▼
                           sharedbox-admin (root, path-activated)
                           re-verifies identity with `tailscale whois`,
                           creates the account / runs the action AS the friend
                                     │
              ┌──────────────────────┼───────────────────────┐
              ▼                      ▼                       ▼
     user-UID.slice limits     agent rule files        T3 Code service
  (guard rebalances / min)   (janitor refreshes)    (comes back after reboot)
```

| | Per friend (default) | Mechanism |
|---|---|---|
| CPU | 3 cores, 5 when quiet; weight 20 (owner 100) | `CPUQuota`, `CPUWeight` |
| RAM | 7G soft, 15G when quiet, 16G hard | `MemoryHigh`, `MemoryMax` |
| Disk | 40G budget, soft-enforced | janitor |
| Processes | 2048 | `TasksMax` |
| Heavy jobs | 2 at a time box-wide | `box heavy` queue + `nice`/`ionice` |

Everything is tunable in `/etc/sharedbox/sharedbox.conf` (documented inline), per user with
`sharedbox-admin limits NAME KEY=VALUE`.

<details>
<summary><b>The janitor, in detail</b></summary>

Every 6 hours it measures each home as root, then runs the cleanup **as that user** (`setpriv`),
so symlink tricks can't redirect deletions.

| What | When |
|---|---|
| Cache entries (`~/.cache/*`, npm, Gradle, Cargo, Go, uv, …) | unused 7 days, or now if over budget |
| Build outputs in `~/projects/*` | project idle 3 days, or now if over budget |
| A whole `~/projects/<name>` | untouched 7 days, no process, no service. Announced 3 days ahead |
| `~/services`, `~/keep`, dotfiles | never |

Below 20% free disk all TTLs halve; below 10% caches are wiped.
</details>

<details>
<summary><b>Admin cheat sheet</b></summary>

```
sharedbox-admin list | invite | issues | requests
sharedbox-admin limits NAME MEMORY_MAX=10G CPU_QUOTA=400% DISK_BUDGET_GB=50
sharedbox-admin suspend NAME | resume NAME | remove NAME --archive
sharedbox-tools add pkg ffmpeg          # install for everyone, remembered across reinstalls
sharedbox-health report | test-email
sharedbox-canary status
```
</details>

<details>
<summary><b>Security model</b></summary>

- Tailscale is the front door: the box is never on the public internet, and Tailscale identity is
  what signup and the `/me` page trust.
- The web service is unprivileged and sandboxed; a root path unit re-validates every request.
  Requests from the box itself are rejected, so a local user can't impersonate the owner.
- Friend homes are `0700`. No sudo, no docker group.
- See [SECURITY.md](SECURITY.md) to report a vulnerability.
</details>

## Roadmap

- 🔗 **Linking several boxes**: one signup, accounts and load spread across several machines
- Omarchy/Arch: first real-hardware install
- Public URLs for `box serve` via Tailscale Funnel
- Hard disk quotas where the filesystem supports them
- ntfy/Discord notifications

## Contributing

PRs welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md). Commands and paths are still named `sharedbox-*` (the project's old name). The codebase is plain stdlib Python and
bash, so there's nothing to build.

## License

[MIT](LICENSE)
