<div align="center">

# treehouse

### One spare computer, shared by your friends' AI coding agents.

Friends sign in from a browser. Their Claude Code, Codex and opencode sessions run on your machine,
each with the same generous, enforced limits as the owner. No VPN, no SSH keys, no per-friend setup.

`stdlib Python` · `systemd` · `cgroup v2` · no containers, no database

**Ubuntu / Debian** ✅  **Omarchy / Arch** 🧪 (experimental)

</div>

---

## Why

AI agents made "I'll just code on my laptop" expensive: builds, dev servers, and five agent
sessions at once. Meanwhile there's an old desktop in your closet with 32 GB of RAM doing nothing.

treehouse turns that machine into a box your friends can actually use, including the
non-technical ones:

1. You (or your agent) run `box invite --email friend@example.com`. Their account exists right away.
2. They open your site on any laptop or phone and type the code that gets emailed to them.
   No Tailscale, no app, no SSH key.
3. They land on their dashboard, log in to Claude or ChatGPT with a click, and open T3 Code: an agent
   running on your box, with everything installed and the rules of the machine already explained.

No terminal, no VPN invites, no "can you run this command for me".

## What you get

**For friends**
- 🌐 **Browser-only onboarding.** Signup, agent logins, GitHub login and a personal
  [T3 Code](https://t3.codes) web UI, all from one page (`/me`). Codes are detected automatically.
- 🤖 **Agents that know the house rules.** Every account's `CLAUDE.md` / `AGENTS.md` gets a managed block:
  limits, ports, what gets cleaned up, how to install things without sudo, and live notices
  ("project X will be archived Friday", "the box reboots at 4 AM"). Claude Code also gets a live
  snapshot of the box at the start of every session. Agents are told to do setup
  themselves and never send a non-technical user to a terminal.
- 🧰 **Ready on first login.** Claude Code, Codex, opencode, node, Python + uv, git + gh, Go, rustup,
  build tools, headless-browser libs, optional JDK + Android SDK. `npm i -g` works without sudo.
- 🏕️ **Shared servers the whole group owns.** A group chat bot or game server lives in `/srv/shared/NAME`:
  any friend (or their agent) can edit its code, start/stop/restart it and read its logs with `box shared`.
  It runs as its own locked-down account, in one slice with a shared CPU/RAM cap, and survives reboots.
- 💻 **Terminal still works.** Technical users can add an SSH key and use the `box` CLI. With the
  Cloudflare login, a friend pastes one prompt into their laptop's own Claude Code / Codex and it sets up
  `ssh treehouse` (through Cloudflare, no VPN), VS Code Remote-SSH included.

**For you (the owner)**
- ⚖️ **Equal limits, owner included.** Every account gets the same cgroup slice: **16 GB RAM**, no CPU cap
  (equal CPU weight shares it when several people want it), 2048 processes. Runaway agents stay inside their
  own slice. Nothing slows agents down on purpose: no naps, no build throttling, no "busy box" limits.
- 🚀 **Emergency boost.** `box boost` (or the dashboard button) raises your own RAM to 28 GB for 4 hours when
  the box has it free. No approval needed; it ends by itself.
- 🧹 **A janitor that never loses work.** A project (a git repo in your home) is archived only when it's
  untouched for 20 days *and* fully pushed to a remote; `box restore NAME` brings it back for 14 days. Unused
  caches go after 30 days; build outputs only when someone is over their disk budget. Cleanup runs *as the user*.
- ⚡ **Tuned out of the box.** The installer makes the machine itself fast for agent work: compressed memory
  before swap (and a swap file if there's none), file-watch limits for many dev servers, network settings for the
  Cloudflare tunnel and phones, T3 Code reopening from the browser's cache, agents installed in parallel.
  `box tune` shows what's left on your machine; agents use it to find more and suggest it.
- 📊 **Dashboard.** Live CPU/RAM per person with sparklines, disk vs. budget, running servers.
- 📬 **Health email.** A weekly report plus hourly urgent checks: disk, failed services, SMART, pending
  updates, security audit, and a section for **what slowed agents down**, logged by the agents themselves
  via `box issue`.
- 🧩 **Shared skills.** A catalog of agent skills everyone's agents can see (`box skills`): a multi-model
  `council` review and Theo Browne's [tstack](https://github.com/t3dotgg/tstack) PR skills. Agents suggest
  one and install it only after their owner says yes. Admins publish their own with `box skills share DIR`.
- 🔁 **Self-updating.** Agents update nightly for everyone. Scheduled reboots twice a week (Wed and Sun at 04:00, configurable)
  clear leaks; they wait up to 2 hours for agents that are mid-turn, and the dashboards show the next one.

## Quick start

You need a Linux machine (Ubuntu 24.04 or Omarchy) and a coding agent that can reach it.
Then tell your agent:

```text
Set up treehouse on this machine. Follow https://github.com/mh-anwar/treehouse/blob/main/INSTALL.md
```

That's it. Your agent checks the machine, asks you three questions, installs everything, tunes the machine,
sets up email alerts and a public join link, checks it all works, and hands you an invite message for your
first friend.
The only things you do yourself are click "approve" on a login page and maybe type your sudo password.

<details>
<summary>Doing it by hand instead</summary>

```bash
git clone https://github.com/mh-anwar/treehouse && cd treehouse
sudo ./install.sh                                    # dry run: prints every action, changes nothing
sudo ./install.sh --apply --admins you --mail-to you@gmail.com
box invite --note "Sam"                              # a one-time link to send your first friend
```
</details>

The installer is additive and safe on a live machine. It **never** touches sshd, tailscaled or the firewall,
never restarts networking (its network tuning applies live), and never reboots. It does rewrite every account's
limits to the current config (equal for everyone). `./uninstall.sh` reverses it.

**Requirements:** Linux with systemd + cgroup v2, Python ≥ 3.12, Tailscale on the server (friends don't
need it), and for the recommended public site a domain on Cloudflare (free plan). Tested on Ubuntu 24.04; Omarchy/Arch support is new. Please [open an issue](../../issues) with
what breaks.

## How it works

The recommended setup puts the box behind **Cloudflare Access + Cloudflare Tunnel**. Friends open your
site (say `https://treehouse.example.com`), type their email and the code Cloudflare sends them, and only
emails you invited get through. The tunnel is outbound-only: nothing on the box listens on the internet.

```
 friend's browser ─▶ Cloudflare Access (email code; only invited emails)
                          │  Cloudflare Tunnel (cloudflared, outbound only)
                          ▼
                 sharedbox-gate (unprivileged, 127.0.0.1) checks Cloudflare's signed token on every request
                   │ /_box/*: dashboard, logins            │ everything else
                   ▼                                       ▼
          sharedbox-web (unprivileged)          the friend's own T3 Code (started on
                   │ drops a request in a spool dir      the first visit)
                   ▼
          sharedbox-admin (root, path-activated): re-verifies the token itself,
          creates the account / runs the action AS the friend
                   │
     ┌─────────────┼──────────────────────┐
     ▼             ▼                      ▼
 user-UID.slice  agent rule files     janitor: cleanup as the user,
 limits, boost   + session snapshot   archives, notices
```

Two other ways in, both still supported: **Tailscale only** (each friend joins your tailnet; the
dashboard and `/join` trust `tailscale whois`, nothing is public) and **Tailscale Funnel** (a public
`*.ts.net` link with one-time login links instead of Cloudflare; simpler, but the login pages face the
internet). The installer prints the steps for each; `INSTALL.md` walks an agent through choosing.

| | Per account, owner included (default) | Mechanism |
|---|---|---|
| CPU | no cap; equal weight (100) for everyone | `CPUWeight` |
| RAM | 16G soft, 17G hard; `box boost`: 28G for 4 h | `MemoryHigh`, `MemoryMax` |
| Disk | 40G budget, soft-enforced | janitor |
| Processes | 2048 | `TasksMax` |

Everything is tunable in `/etc/sharedbox/sharedbox.conf` (documented inline), per user with
`sharedbox-admin limits NAME KEY=VALUE`.

<details>
<summary><b>The janitor, in detail</b></summary>

Every 6 hours it measures each home as root, then runs the cleanup **as that user** (`setpriv`),
so symlink tricks can't redirect deletions.

| What | When |
|---|---|
| Cache entries (`~/.cache/*`, npm, Gradle, Cargo, Go, uv, …) | unused 30 days, or now if over budget |
| Build outputs (`node_modules`, `.venv`, `target`, …) | only when over budget |
| A project (a git repo directly in `~`) | untouched 20 days, no process, no service, **and fully pushed** (a remote, clean tree, no untracked files, every branch pushed, no stash): **archived** to `~/.sharedbox/archive/` (announced 3 days ahead). Non-git folders and unpushed work: never |
| An archived project | 14 days after archiving (`box restore NAME` until then), or first when over budget |
| `~/services`, `~/keep`, dotfiles | never |
| `/srv/shared` (shared servers' code) | never (`box shared rm` only moves code to `/srv/shared/.trash`) |

Below 20% free disk all TTLs halve; below 10% caches are wiped.
</details>

<details>
<summary><b>Solo mode (one person, no friends)</b></summary>

Set `SOLO=1` in `/etc/sharedbox/sharedbox.conf` and re-run `sudo ./install.sh --apply`. treehouse then runs as a
lean personal server: equal limits, `box boost`, the janitor, agent rules, `box serve`, health email and T3 all
work as usual, while the friend-only parts are off: signups and
invites (`/join`, `box invite`), and Cloudflare seat alerts.
</details>

<details>
<summary><b>Admin cheat sheet</b></summary>

```
box invite [--note WHO] [--json]        # owner accounts, no sudo: one-time invite link
box skills share DIR | unshare NAME     # owner accounts, no sudo: publish a skill to everyone
box shared ls | create NAME | set NAME -- CMD... | logs NAME   # anyone: group-owned servers in /srv/shared
sharedbox-admin list | invite | sessions | issues | requests
sharedbox-admin limits NAME MEMORY_HIGH=10G MEMORY_MAX=11G DISK_BUDGET_GB=50
sharedbox-admin boost NAME [--why TEXT] # same as `box boost` for that account
sharedbox-admin suspend NAME | resume NAME | remove NAME --archive
sharedbox-tools add pkg ffmpeg          # install for everyone, remembered across reinstalls
sharedbox-health report | test-email
```
</details>

<details>
<summary><b>Security model</b></summary>

- **Cloudflare mode (recommended):** Cloudflare Access is the first lock. Only emails on your allow list
  (`box invite --email`) ever reach the box, and the tunnel only makes outbound connections. The gate is
  the second lock: it verifies Cloudflare's signed token (audience, expiry, signing keys) on every request,
  and root verifies it again before any account or portal action. Self-signup is off by default here
  (`JOIN_MODE` unset = invites only).
- **Tailscale mode:** the box is never on the public internet; Tailscale identity (`tailscale whois`) is
  what signup and the `/me` page trust. Self-signups must give an email.
- T3 Code is the friend's own server, reachable only through the gate (or the tailnet) and paired per browser.
- The web service and the gate are unprivileged and sandboxed; a root path unit re-validates every request.
  Requests from the box itself are rejected, so a local user can't impersonate the owner.
- Friend homes are `0700`. No sudo, no docker group. Everything that touches a friend's files (cleanup,
  archiving, stopping an idle T3) runs as that friend.
- See [SECURITY.md](SECURITY.md) to report a vulnerability.
</details>

## Roadmap

- 🔗 **Linking several boxes**: one Cloudflare site and signup, accounts and load spread across several
  machines (the gate already has the hook: a per-account box, reached over the tailnet)
- Owner buttons on the dashboard for invite, suspend and remove (today the owner's agent does it with
  `box friends`, no sudo needed)
- Omarchy/Arch: first real-hardware install
- Hard disk quotas where the filesystem supports them
- ntfy/Discord notifications

## Contributing

PRs welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md). Commands and paths are still named `sharedbox-*` (the project's old name). The codebase is plain stdlib Python and
bash, so there's nothing to build.

## License

[MIT](LICENSE)
