"""SSH from a friend's own computer, set up by their own local coding agent (Claude Code, Codex, ...).

No VPN: `ssh treehouse` runs `cloudflared access ssh` as its ProxyCommand, which logs in to Cloudflare Access
(the same email code as the website) and tunnels to sshd on the box (SSH_HOST -> tunnel -> localhost:22).
sshd still wants the friend's key, which their agent registers through POST /_box/api/ssh-key with the
Access token (sharedbox-web -> spool -> root -> `box _portal add-key`).

This module only renders text: the prompt the friend pastes into their agent (also served as
/_box/connect.md), and the ~/.ssh/config block. Pure functions, no I/O; stdlib only.

It also renders the T3 Code app's one-click pairing link (t3code://pair?pairingUrl=...) and the "setup prompt" a
friend pastes into the agent on their laptop to install the app and connect it to their T3 on the box.
"""

import urllib.parse

ALIAS = "treehouse"


def t3_app_link(pair_url):
    """The T3 Code app's pairing link. The app asks "Add the backend at <origin>?" and pairs with the token.
    Built from the pairing URL people connect to (the box's Tailscale IP and the friend's first port), never from
    the "App link:" line `t3 pair` prints (that one carries T3's internal port)."""
    return "t3code://pair?pairingUrl=" + urllib.parse.quote(pair_url, safe="") if pair_url else ""


def app_origin(pair_url):
    """http://<ip>:<port> of a pairing URL, or ""."""
    u = urllib.parse.urlsplit(pair_url or "")
    return f"{u.scheme}://{u.netloc}" if u.scheme in ("http", "https") and u.netloc else ""


def app_prompt(pair_url, dashboard, box="the box"):
    """The prompt for the friend's own agent: install T3 Code, then open the pairing link on that computer.
    `dashboard` is where they make a new one ({site}/_box/me); `box` is the box's name as Tailscale shows it."""
    origin = app_origin(pair_url)
    if not origin:
        return ""
    return f"""Set up the T3 Code app on this computer and connect it to my account on treehouse, a shared Linux box. Do each step yourself. Only ask me when a step says to.

1. Check the box is reachable (on Windows, run curl.exe):
   curl -sI --max-time 15 {origin}/
   Any HTTP reply means it works. If it times out, this computer can't reach the box over Tailscale yet. If Tailscale isn't installed, install it from https://tailscale.com/download. Ask me to sign in to Tailscale with the account the owner shared the box with, then try again. If it still times out, stop and tell me: "Ask the owner to share {box} with your Tailscale account, then make a new prompt at {dashboard}."
2. Check the T3 Code app is installed. If it isn't, download the right installer for this computer from https://t3.codes and install it. If you can't, ask me to install it and wait for me. If the app was just installed, open it once and wait for its first screen.
3. Open this link with the system link opener: macOS: open, Linux: xdg-open, Windows: PowerShell's Start-Process (not cmd's start). Keep the single quotes:
   '{t3_app_link(pair_url)}'
4. Tell me: "The T3 app should ask: Add the backend at {origin}? Click OK."
5. If the link doesn't open the app, tell me: "In the T3 app, go to Settings, Connections, Add environment, pick Remote link, and paste this into Host: {pair_url}"

The link works once, for 30 minutes. If it fails or has expired, ask me to make a new prompt at {dashboard}.
Don't change anything else on this computer.
"""


def ssh_config_block(user, ssh_host, cloudflared="cloudflared", alias=ALIAS):
    """The Host block for ~/.ssh/config. `cloudflared` is the command (Windows: its full path, quoted)."""
    return (f"Host {alias}\n"
            f"  HostName {ssh_host}\n"
            f"  User {user}\n"
            f"  IdentityFile ~/.ssh/{alias}\n"
            f"  IdentitiesOnly yes\n"
            f"  ProxyCommand {cloudflared} access ssh --hostname %h\n"
            f"  ServerAliveInterval 30\n")


def connect_text(user, site, ssh_host, alias=ALIAS):
    """Instructions written FOR a coding agent on the friend's computer. `site` is https://<public host>.
    With ssh_host empty (the owner hasn't turned SSH on yet) it says so instead of half-working steps."""
    site = site.rstrip("/")
    if not ssh_host:
        return (f"# Use {site} from your own computer's agent\n\n"
                f"SSH through Cloudflare isn't turned on for this box yet. Tell the human: \"ask the owner to run "
                f"`sudo sharedbox-admin cloudflare ssh` on the box, then copy these instructions again from "
                f"{site}/_box/me\". Don't try other ways in (no VPN, no port forwarding).\n")
    key = f"~/.ssh/{alias}"
    block = ssh_config_block(user, ssh_host, alias=alias)
    win_block = ssh_config_block(user, ssh_host, '"C:\\Program Files (x86)\\cloudflared\\cloudflared.exe"', alias)
    return f"""# Connect this computer to the treehouse box ({site})

You are a coding agent on the human's own computer. Set up SSH so that `ssh {alias}` reaches their account
**{user}** on the shared Linux box at {site}. It goes through Cloudflare (no VPN, no Tailscale). Do every step
yourself; the human only has to type one emailed code in a browser. Work out the OS first (macOS, Linux or
Windows) and use the matching commands. Never print or send a private key anywhere; only `{key}.pub` leaves
this computer.

Already set up? If `ssh -o BatchMode=yes {alias} true` succeeds, skip to step 7.

## 1. Install cloudflared (Cloudflare's client)
- macOS: `brew install cloudflared` (no Homebrew: download `cloudflared-darwin-arm64.tgz` or
  `cloudflared-darwin-amd64.tgz` from https://github.com/cloudflare/cloudflared/releases/latest, unpack it into
  `~/.local/bin` and `chmod +x` it).
- Linux, no root needed: `mkdir -p ~/.local/bin && curl -fsSL -o ~/.local/bin/cloudflared
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 && chmod +x
  ~/.local/bin/cloudflared` (use `cloudflared-linux-arm64` on ARM; `uname -m` tells you). Make sure `~/.local/bin`
  is on PATH. With root you may instead use the distro package (Debian/Ubuntu: the `.deb` from the same releases
  page or Cloudflare's apt repo; Arch: `pacman -S cloudflared`).
- Windows (PowerShell): `winget install --id Cloudflare.cloudflared`, then open a new shell. Find its full path
  with `(Get-Command cloudflared).Source` (usually `C:\\Program Files (x86)\\cloudflared\\cloudflared.exe`).
Check: `cloudflared --version` prints a version.

## 2. Make a dedicated SSH key (only if `{key}` doesn't exist yet)
- macOS / Linux: `mkdir -p ~/.ssh && chmod 700 ~/.ssh && ssh-keygen -t ed25519 -f {key} -N "" -C "{user}@{alias}"`
- Windows: `cmd /c 'ssh-keygen -t ed25519 -f "%USERPROFILE%\\.ssh\\{alias}" -N "" -C "{user}@{alias}"'`
  (through cmd so the empty passphrase really is empty; OpenSSH ships with Windows 10+).
Don't overwrite an existing `{key}`; reuse it.

## 3. Log in to Cloudflare (the one step that needs the human)
Tell the human exactly this first: "A browser tab will open. Sign in with the email you use for {site} and
type the code Cloudflare emails you. Then come back here." Then run:
    cloudflared access login {site}
It finishes by itself once they've signed in (it saves a token under `~/.cloudflared/`).
Then pre-authorize the SSH hostname the same way (usually no new code is needed now; if one is, the human
types it again): `cloudflared access login https://{ssh_host}`. If that command errors, skip it: the first
`ssh` in step 6 opens the browser instead.

## 4. Register the public key with the box
macOS / Linux:
    curl -sS -X POST {site}/_box/api/ssh-key -H "X-Treehouse-Agent: 1" -H "cf-access-token: $(cloudflared access token -app={site})" --data-binary @"$HOME/.ssh/{alias}.pub"
Windows (PowerShell):
    $tok = cloudflared access token -app={site}
    Invoke-RestMethod -Method Post -Uri {site}/_box/api/ssh-key -Headers @{{"X-Treehouse-Agent"="1"; "cf-access-token"=$tok}} -ContentType "text/plain" -Body (Get-Content -Raw "$env:USERPROFILE\\.ssh\\{alias}.pub")
(No `-f`: on an error the JSON body says why.) The answer is JSON: `{{"ok": true, "user": "{user}", "ssh_host": "{ssh_host}", "ssh_config": "..."}}`. If `ok` is
false, show the human the `error` text. A 403 "Not allowed" page means the Cloudflare login is missing or
expired: redo step 3. `GET {site}/_box/api/whoami` (same header) shows who you're signed in as.

## 5. Add the host to the SSH config (only if there's no `Host {alias}` in it yet)
Append to `~/.ssh/config` (create it, mode 600 on macOS/Linux):

{block}
Windows: the file is `%USERPROFILE%\\.ssh\\config`, and ProxyCommand needs cloudflared's full path, quoted:

{win_block}
(If ssh can't run a path with spaces, copy cloudflared.exe to `%USERPROFILE%\\bin\\` and use that path.)

## 6. Test
    ssh -o StrictHostKeyChecking=accept-new {alias} 'box status'
It should print {user}'s usage and limits on the box. "Permission denied (publickey)": step 4 didn't work.
A browser opening: Cloudflare wants the login again; tell the human to finish it.

## 7. Tell the human what they can do now
- "Run X on my treehouse box": you run `ssh {alias} '<command>'` for them.
- `ssh -t {alias} claude` (or `codex`) starts a coding agent that lives on the box.
- VS Code / Cursor: Remote-SSH, "Connect to Host...", pick `{alias}`.
- Every few weeks Cloudflare asks for the email code again; a browser tab opens by itself when it does.
- The website is still {site} (their dashboard: {site}/_box/me).

## Rules when you work on the box
- It's a shared machine with no sudo for anyone. Never run `sudo` or ask for root/admin. Read
  `ssh {alias} 'box rules'` once before doing real work there and follow it.
- Files live on the box, not on this computer: edit them there (`ssh`, Remote-SSH) or move them with
  `scp`/`git`. Keep projects in `~/projects/<name>/`; idle projects are cleaned up, so push work to git.
- Anything long-running goes in `box serve add <name> -- <command>` (or `box shared` for something the whole
  group runs). Never leave servers in `nohup`, `tmux` or `&`: the box reboots for updates.
- Heavy builds: `box heavy -- <command>`. `box help` lists more topics.

To add the T3 app too: `ssh {alias} box t3 app-link` and open the t3code:// link it prints on this
computer (the app needs Tailscale here).
"""
