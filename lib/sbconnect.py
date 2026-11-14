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


def ts_facts(status, rec):
    """What the box's own `tailscale status --json` says about a friend's Tailscale account, for the setup prompt.
    Pure: `status` is that JSON (or None), `rec` their users.json record. Returns {"login", "mode"}:
    mode "sharee" (the box is shared with them and has seen their devices), "member" (they are in the owner's
    tailnet), "reverse" (only their own computer is shared INTO the owner's tailnet: the box can reach it, but the
    box isn't shared back), "none" (no device of theirs seen) or "" (unknown)."""
    rec = rec or {}
    login, ts_id = rec.get("login") or "", str(rec.get("ts_id") or "")
    if not isinstance(status, dict):
        return {"login": login, "mode": ""}
    users = status.get("User") or {}
    if not ts_id:  # an email-invited friend: match their email against the logins the box knows
        email = (rec.get("email") or "").lower()
        ts_id = next((str(k) for k, u in users.items() if email and (u.get("LoginName") or "").lower() == email), "")
    if not ts_id:
        return {"login": login, "mode": ""}
    login = (users.get(ts_id) or {}).get("LoginName") or login
    suffix = "." + ((status.get("CurrentTailnet") or {}).get("MagicDNSSuffix") or status.get("MagicDNSSuffix") or "")
    mine = [p for p in (status.get("Peer") or {}).values() if str(p.get("UserID")) == ts_id]
    if any(p.get("ShareeNode") for p in mine):
        mode = "sharee"
    elif suffix != "." and any((p.get("DNSName") or "").rstrip(".").endswith(suffix.rstrip(".")) for p in mine):
        mode = "member"
    else:
        mode = "reverse" if mine else "none"
    return {"login": login, "mode": mode}


def _facts(ts_login, ts_mode, apps):
    """The "What the box knows" lines of the setup prompt (only facts the box really has)."""
    out = []
    if ts_login:
        out.append({
            "sharee": f"- The box is shared with the Tailscale account {ts_login}, and it has seen that account's devices.",
            "member": f"- {ts_login} is a member of the box owner's tailnet.",
            "reverse": f"- The box is NOT shared with the Tailscale account {ts_login} yet. (A computer of that account "
                       f"is shared with the owner, which only lets the box reach it, not the other way round.)",
            "none": f"- The box hasn't seen any device of the Tailscale account {ts_login} yet: the share may not be "
                    f"accepted yet, or this computer uses another account.",
        }.get(ts_mode, f"- My Tailscale account on the box's side is {ts_login}."))
    for a in apps or ():
        bits = ", ".join(b for b in (a.get("os"), a.get("ip") and "Tailscale IP " + a["ip"],
                                     a.get("version") and "app " + a["version"]) if b)
        when = f", last connected {a['last']}" if a.get("last") else ""
        out.append(f"- A T3 app already connected to my T3 ({bits or 'details unknown'}{when}).")
    return "\n".join(out)


def app_prompt(pair_url, dashboard, box="the box", *, ts_login="", ts_mode="", apps=()):
    """The prompt for the friend's own agent on their laptop: diagnose first, fix only what's broken (Tailscale,
    then the T3 Code app), then open the one-time pairing link. `dashboard` is where they make a new one
    ({site}/_box/me); `box` is the box's name (prose only: shared-in users can't resolve it, addresses use the IP).
    Optional facts from the box: `ts_login`/`ts_mode` (ts_facts) and `apps`, the T3 app sessions already
    connected (dicts with os, ip, version, last)."""
    origin = app_origin(pair_url)
    if not origin:
        return ""
    ip = urllib.parse.urlsplit(origin).hostname
    facts = _facts(ts_login, ts_mode, apps)
    facts = f"\nWhat the box knows:\n{facts}\n" if facts else ""
    who = ts_login or "the account the owner shared the box with"
    right = f" The box expects {ts_login}." if ts_login else ""
    known = (" If the box says an app already connected from this computer's Tailscale IP (tailscale ip -4), ask me "
             f"whether T3 on {box} works in the app; if yes, stop: it's done." if apps else "")
    return f"""Connect the T3 Code desktop app on this computer to my T3 on treehouse ({box}), a shared Linux box, over Tailscale. Do the work yourself; I only sign in, approve permission prompts, click Confirm, or type this computer's password. Look before you change anything and stop as soon as it works. Never repeat a failing step blindly: retry once, only after fixing something. Don't set up T3 Connect, never sign out of or remove a Tailscale account, and don't change anything else on this computer.

My T3: {origin} (always use this IP address; the name {box} doesn't resolve for shared devices).
{facts}
1. Look first, read-only. Work out the OS, then:
   a. curl -sI --max-time 15 {origin}/   (Windows: curl.exe). Any HTTP reply means the box is reachable: go to step 3.
   b. Find the Tailscale command: tailscale on PATH, else /Applications/Tailscale.app/Contents/MacOS/Tailscale (macOS), else "C:\\Program Files\\Tailscale\\tailscale.exe" (Windows). Run tailscale status --json and note BackendState, the signed-in account (LoginName of the User entry whose ID is Self.UserID), CurrentTailnet.Name, and whether a Peer has {ip} in TailscaleIPs.
2. Fix Tailscale (only when 1a failed), by what 1b found:
   - Not installed: macOS: brew install --cask tailscale-app (or the .pkg from https://tailscale.com/download); I type my password, open Tailscale and click Allow on its VPN/system extension prompt. Windows: winget install --id Tailscale.Tailscale -e (I click Yes). Linux: it needs root; if you can't get root without a password, give me ONE line to paste in a terminal that runs https://tailscale.com/install.sh, then tailscale set --operator=$USER as root, then tailscale up. Never install a second Tailscale next to an existing one.
   - The command can't talk to Tailscale: start the app (macOS: open -a Tailscale; Windows: Tailscale from the Start menu; Linux: the tailscaled service, as root).
   - Stopped: tailscale up.
   - NeedsLogin: run tailscale up in the background, show me the login link it prints, and wait until BackendState is Running. Tell me to sign in as {who}, with the same Google/GitHub/Microsoft button as before.
   - NeedsMachineAuth: an admin of that tailnet must approve this computer. Tell me and stop.
   - Running, no peer {ip}: this account can't see the box.{right} If tailscale switch --list shows another account that should have it, ask me before switching, and say switching turns off my other Tailscale (for example work) until I switch back. Add an account only with tailscale login (keeps the old one), and only if I agree. Otherwise the share isn't accepted yet: ask me to open the email from Tailscale about {box} and accept it signed in as the account this computer uses (whichever account accepts gets the box; on a work tailnet only its admins can accept).
   - Running, peer {ip} there, curl times out: tailscale ping --c 3 {ip}. No reply: the box or this computer is offline, or another VPN or a work tailnet's rules block it. Ask me before touching another VPN.
   Then run the curl once more. If it still times out, stop and tell me: "Ask the owner to share {box} with my Tailscale account <login>, then make a new prompt at {dashboard}." (Put in the login from 1b: the owner shares with exactly that.) Meanwhile T3 works in the browser: {dashboard}, Open T3.
3. The T3 Code app. If it isn't installed (macOS: /Applications/T3 Code*.app), install it: macOS brew install --cask t3-code, Windows winget install T3Tools.T3Code, Linux the .deb or AppImage for this CPU from https://t3.codes (not its install.sh: that is only the server). Older than 0.1.9: update it the same way. Open it once and wait for its first screen.{known}
4. Open this link with the system link opener: macOS: open, Linux: xdg-open, Windows: PowerShell's Start-Process. Keep the single quotes:
   '{t3_app_link(pair_url)}'
   Windows: if reg query HKCU\\Software\\Classes\\t3code finds nothing, skip to step 5. Linux: if xdg-mime query default x-scheme-handler/t3code prints nothing, start the app once and check again; still nothing: step 5.
   Tell me: "The T3 app should ask: Add the backend at {origin}? Click Confirm." It can take 10 seconds after the app starts. Success is "Backend added". Don't open the link a second time.
   - "already saved": the app has {box} under an old address (like http://{ip}:<old port> or the name {box}). Tell me: "In the T3 app, go to Settings, Environments, open {box}, click Add route, paste this into Host and click Add route: {pair_url} Then click Remove route on the old address."
   - "Update your app": update it (step 3), then ask me for a new prompt.
5. If the link doesn't open the app, tell me: "In the T3 app, go to Settings, Environments, Add environment, pick Remote link, and paste this into Host: {pair_url}" (Older apps call Environments "Connections".)

The link works once, for 30 minutes. Don't open it in a browser to test it: that uses it up. If it fails or has expired, ask me to make a new prompt at {dashboard}. Each computer needs its own prompt.
At the end, tell me in one line: it works, or what blocks it and the Tailscale account this computer uses.
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

To add the T3 Code app on this computer too (it needs Tailscale here): run `ssh {alias} box t3 app-link --prompt`
and follow the steps it prints (it checks Tailscale first, then pairs the app).
"""
