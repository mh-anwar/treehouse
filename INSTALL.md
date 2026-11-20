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
3. **How friends connect.** Don't make the owner choose from jargon. Ask one question, "Do you have a
   domain on Cloudflare?" (check yourself first if you can), and recommend the route that fits:
   - **Cloudflare login (most secure, recommended if they have a domain on Cloudflare):** friends open
     `https://box.<their-domain>` on any device and log in with an email code. Only emails the owner
     invited ever reach the machine. The owner clicks one pre-filled link and copies one token; you do
     everything else, including turning on Cloudflare Zero Trust.
   - **Private (Tailscale only):** nothing is exposed at all. Each friend installs Tailscale and the owner
     shares the machine with them in the Tailscale admin console. Good for a few technical friends; free
     Tailscale limits how many people you can add.
4. **Who can join:** "only people you invite" (recommended; you or your agent run `box invite`) or "anyone
   you've shared the machine with on Tailscale". This is `JOIN_MODE` = `closed` / `open`; it only affects
   Tailscale self-signup at `/join`, where the account is created right away (there is no approval step).
   Invites always work. With the Cloudflare login it defaults to `closed` when unset. Without Cloudflare,
   `box invite` sends friends to `/join`, so that setup needs `open`.

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
limits, cleanup policy; it also adds an `sbshared` system account and `/srv/shared` for servers the
whole group owns, `box shared`). Mention the parts that matter on a machine already in use: the janitor
archives git projects untouched for 20 days in *friends'* homes, and only once they're fully pushed (restorable
for 14 days); every account, the owner's included, gets the same limits (16 GB RAM, no CPU cap); and the scheduled reboot is twice a week (Wed and Sun at 04:00; it waits up to 2
hours for agents that are mid-turn). Then ask for the OK, and run:
```
sudo ./install.sh --apply --admins <admins> [--mail-to <email>]
```
Then write the owner's answer to "who can join" (question 4) into the config, so it doesn't depend on a default:
`sudo sed -i 's/^#\?JOIN_MODE=.*/JOIN_MODE=<closed|open>/' /etc/sharedbox/sharedbox.conf && sudo systemctl restart sharedbox-web`
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

## 5. Connect friends (the route from question 3)

### Cloudflare login
The gate also checks Cloudflare's signed login token on every request, and the tunnel only makes outbound
connections, so nothing on the box listens on the internet.
1. Send the owner this pre-filled link. Tell them to check the summary, click **Continue to summary** and then
   **Create Token**, and copy the token:
   https://dash.cloudflare.com/profile/api-tokens?permissionGroupKeys=%5B%7B%22key%22%3A%22argotunnel%22%2C%22type%22%3A%22edit%22%7D%2C%7B%22key%22%3A%22access%22%2C%22type%22%3A%22edit%22%7D%2C%7B%22key%22%3A%22access_acct%22%2C%22type%22%3A%22edit%22%7D%2C%7B%22key%22%3A%22dns%22%2C%22type%22%3A%22edit%22%7D%2C%7B%22key%22%3A%22zone%22%2C%22type%22%3A%22read%22%7D%5D&accountId=*&zoneId=all&name=treehouse
   If the list doesn't include **Cloudflare Tunnel · Edit**, they add it with **+ Add more** (Cloudflare
   doesn't support pre-filling every permission). The full list is: Account · Cloudflare Tunnel · Edit;
   Account · Access: Apps and Policies · Edit; Account · Access: Organizations, Identity Providers, and
   Groups · Edit; Zone · DNS · Edit; Zone · Zone · Read.
2. Get the token into `/etc/sharedbox/cloudflare-token` (root, mode 600) without it ever entering the chat:
   - If your harness has a secure secret prompt that saves to a file, use it, then
     `sudo install -m 600 -o root -g root <that file> /etc/sharedbox/cloudflare-token`.
   - Otherwise have the owner type this one line; it asks for the token without showing it, then for the
     hostname and their email: `sudo bash /usr/share/sharedbox/cloudflare-setup.sh`
3. Pick the hostname yourself (e.g. `treehouse.<domain>`, a subdomain that has no DNS record yet, so no existing
   site is touched), then run:
   `sudo sharedbox-admin cloudflare setup --hostname <host> --owner-email <owner's email>`
   It turns on Zero Trust if it isn't on yet (free plan, a team name picked for them). It also creates the
   tunnel, DNS record, email-code login and Access app, installs `cloudflared`, and starts it with the gate.
   It is safe to re-run. If it names a missing permission, the owner edits the token and you run it again.
4. Check: `sudo sharedbox-admin cloudflare status`. Also `curl -sI https://<host>/` should redirect to
   `<team>.cloudflareaccess.com`.
5. Setup also turns on **SSH through Cloudflare** for friends' laptops: a second hostname (`SSH_HOST`; default
   `<first label>-ssh.<rest>`, e.g. `treehouse-ssh.example.com`, which Cloudflare's free certificate covers) is
   routed by the tunnel to sshd on `localhost:22`, behind an Access app on the same allow list. Nothing about
   sshd changes. On a box set up before this existed: `sudo sharedbox-admin cloudflare ssh`. Friends then paste
   the prompt from their page (`/_box/me`, "Use the box from your own computer's agent") into their own
   Claude Code / Codex, which sets up `ssh treehouse` by itself. `SSH_HOST=off` in the config skips it.

Tailscale Funnel is no longer a public mode: with `PUBLIC_URL` set but `PUBLIC_MODE` not `cloudflare`, the
public front door stays off. Use the Cloudflare login above instead (the installer never changes tailscaled).

### Private (Tailscale only)
There's nothing to set up. For each friend, the owner opens the Tailscale admin console → **Machines** →
this machine → **Share…**. `box invite` prints the matching message.

## 6. Check it works
- `systemctl is-active sharedbox-web sharedbox-tick.timer sharedbox-janitor.timer`
- `curl -s http://$(tailscale ip -4 | head -1):8090/healthz` prints `ok`
- `sudo sharedbox-health report | head -40` reads sensibly
- `box shared ls` runs (as the owner's account) and `sudo sharedbox-admin shared-sync` lists the friends and
  admins in `sbshared` (the group that may edit shared servers)

Fix anything that's down (`journalctl -u <unit> -n 40`) before you report success.

Then make the machine itself efficient: run `box tune`. Everything under "should already be set by treehouse"
must be fixed now (re-run the installer; if a setting won't stick, find what overrides it). Read the
"for the owner" items and `box help tune`, look for more on this particular machine (it may be a laptop or a
desktop with leftovers a server doesn't need), and tell the owner in plain words what you'd change and what it
would cost them. Never turn off a service yourself. Anything that would help every treehouse box goes back
into treehouse (see "Making boxes efficient" in CLAUDE.md).

## 7. Invite the first friend
Run `box invite --note "<who>"` as the owner's account (no sudo; it works for every account in
`--admins`) and give the owner the message it prints, ready to paste to a friend. With the Cloudflare
public link, add the friend's email: `box invite --email sam@example.com --note "Sam"`. That email is
allowed in through Cloudflare Access, and the friend logs in with it from then on. With a public link,
`box invite` creates the friend's account right away (name from `--username`, else picked per
`AUTO_USERNAMES`), so the friend signs in with their email without a signup form. `--no-create` gives the old signup link; `box invite --json` returns `username`, `link`,
`email` and `expires` if you're sending invites yourself.
Finish with a short summary:
- what's running
- the dashboard link (`http://<tailscale-name>:8090/`)
- how to invite more friends (`box invite`, or just ask an agent to do it)
- that `./uninstall.sh` reverses everything
