# Installing treehouse (instructions for an AI agent)

You are an AI coding agent (Claude Code, Codex, opencode, …) setting up **treehouse** for a person
(the *owner*) on a Linux machine they want to share with friends. Do every step yourself. The owner
may not be technical. Ask them only what is listed under "Ask first", plus anything needing a password
or a browser click. Keep your messages short and plain.

## Rules for you
- **Never ask the owner to paste a multi-line block into a terminal.** A pasted block can swallow
  password prompts, and lines get lost. If something has to run in their terminal, write a script to a
  file and give them **one short line** to type, e.g. `sudo bash /tmp/treehouse-step.sh`.
- **Never ask for passwords, tokens or app passwords in the chat.** If one is needed, write a script that
  reads it with `read -rs VAR </dev/tty` and saves it with mode 600, and have the owner run that.
- **sudo:** first try `sudo -n true`. If it works, run the root steps yourself. If it needs a password and
  you can't type it, bundle the root steps into one script and hand the owner the one-line command (see
  above). Never use `sudo -S` with a password from the chat.
- **Don't break a live machine.** Don't restart or edit sshd, tailscaled, networking or the firewall, and
  don't reboot. The installer already promises this; keep the promise in your own commands too.
- Show the dry run's summary before applying, and wait for the owner's OK.

## 0. Where are you?
Run `hostnamectl; cat /etc/os-release; id; systemctl --version | head -1; python3 --version`.
- If you are **not** on the server (e.g. you're on the owner's laptop), ask for the server's SSH address,
  check `ssh <server> true` works, and run every command below over `ssh <server> '…'`.
- Supported: Ubuntu 22.04+/Debian 12+ (tested) and Omarchy/Arch (newer, report what breaks). It needs
  systemd, cgroup v2 (`stat -fc %T /sys/fs/cgroup` prints `cgroup2fs`) and Python ≥ 3.12. If one is
  missing, stop and tell the owner plainly.

## 1. Ask first (one message, all at once)
1. **Admins:** which accounts on this machine are the owner's own? They are never limited. Suggest the
   current user (`id -un`).
2. **Email for alerts** (weekly health report, urgent problems, security alerts). Suggest skipping it
   for now if they don't have a Gmail address handy.
3. **How friends connect:**
   - **Public link (recommended):** friends need nothing but a browser, which also works on phones. It
     uses Tailscale Funnel on this machine only.
   - **Tailscale only:** each friend installs Tailscale and the owner shares the machine with them.
     Free Tailscale has a limited number of share invites.

## 2. Prerequisites
- `git`: install it with the system package manager if missing (`apt-get install -y git` or
  `pacman -S --needed --noconfirm git`; never `pacman -Sy` on its own).
- **Tailscale** on the server: needed for both connection options. If `tailscale status` fails, install it
  from the official script (`curl -fsSL https://tailscale.com/install.sh | sh`) or the distro package, then
  run `sudo tailscale up --qr=false` in the background, read the login URL it prints, and send the owner
  **only that link** to open and approve. Wait until `tailscale status` shows the machine online.
- **sshd:** only needed for friends who want a terminal. Don't install or configure it unless the owner
  asks.

## 3. Install
```
git clone https://github.com/mh-anwar/treehouse ~/treehouse && cd ~/treehouse
sudo ./install.sh            # dry run
```
Summarize what the dry run will do in 5–8 plain bullets (accounts and groups it creates, services,
limits, cleanup policy). Mention the parts that matter on a machine already in use: the janitor deletes
projects untouched for 7 days in *friends'* homes, and the scheduled reboot is Wed/Sun at 04:00. Then ask
for the OK, and run:
```
sudo ./install.sh --apply --admins <admins> [--mail-to <email>]
```
Installing the coding agents and toolchains takes a few minutes. Run it in the background and poll.
If it fails, read the error, fix the cause if you can (missing package, old Python), and re-run. It is
idempotent.

## 4. Email (only if they gave one)
Gmail needs an **app password**: the owner opens https://myaccount.google.com/apppasswords (2-Step
Verification must be on), creates one named `treehouse`, and keeps it to type in a moment. Write this
script and have the owner run it with one line (`sudo bash /tmp/treehouse-email.sh`):
```bash
#!/bin/bash
set -u
read -rsp "Paste the 16-letter app password, then Enter: " P </dev/tty; echo
P=${P// /}; [[ ${#P} -eq 16 ]] || { echo "Expected 16 letters, got ${#P}. Run it again."; exit 1; }
install -m 600 -o root -g root /dev/null /etc/sharedbox/smtp-password
printf '%s\n' "$P" > /etc/sharedbox/smtp-password; unset P
sharedbox-health test-email && echo "Sent. Check your inbox."
```
Ask the owner to confirm the test email arrived.

## 5. Public link (if they chose it)
Run `sudo sharedbox-admin public status` and follow what it prints. Turning the link on is a one-time
`tailscale funnel` command plus allowing Funnel in the owner's Tailscale admin console. Send the owner the
console link and the exact checkbox to tick. Then set `PUBLIC_URL` in `/etc/sharedbox/sharedbox.conf` as
it instructs.

## 6. Check it works
- `systemctl is-active sharedbox-web sharedbox-canary sharedbox-guard.timer sharedbox-janitor.timer`
- `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8090/` prints `200`
- `sudo sharedbox-health report | head -40` reads sensibly

Fix anything that's down (`journalctl -u <unit> -n 40`) before you report success.

## 7. Invite the first friend
Run `box invite --note "<who>"` as the owner's account (no sudo; it works for every account in
`--admins`) and give the owner the message it prints, ready to paste to a friend. Each link works once;
`box invite --json` returns `link`, `id` and `expires` if you're sending invites yourself.
Finish with a short summary:
- what's running
- the dashboard link (`http://<tailscale-name>:8090/`)
- how to invite more friends (`box invite`, or just ask an agent to do it)
- that `./uninstall.sh` reverses everything
