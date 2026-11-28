"""SSH from a friend's own computer, set up by their own local coding agent (Claude Code, Codex, ...).

No VPN: `ssh treehouse` runs `cloudflared access ssh` as its ProxyCommand, which logs in to Cloudflare Access
(the same email code as the website) and tunnels to sshd on the machine (SSH_HOST -> tunnel -> localhost:22).
sshd still wants the friend's key, which their agent registers through POST /_box/api/ssh-key with the
Access token (sharedbox-web -> spool -> root -> `box _portal add-key`).

This module only renders text: the prompt the friend pastes into their agent (also served as
/_box/connect.md), and the ~/.ssh/config block. Pure functions, no I/O; stdlib only.

It also renders the T3 Code app's one-click pairing link (t3code://pair?pairingUrl=...) and the "setup prompt" a
friend pastes into the agent on their laptop to install the app and connect it to their T3 on the machine.
"""

import re
import time
import urllib.parse

ALIAS = "treehouse"


def t3_app_link(pair_url):
    """The T3 Code app's pairing link. The app asks "Add the backend at <origin>?" and pairs with the token.
    Built from the pairing URL people connect to (the machine's Tailscale IP and the friend's first port), never from
    the "App link:" line `t3 pair` prints (that one carries T3's internal port)."""
    return "t3code://pair?pairingUrl=" + urllib.parse.quote(pair_url, safe="") if pair_url else ""


def app_origin(pair_url):
    """http://<ip>:<port> of a pairing URL, or ""."""
    u = urllib.parse.urlsplit(pair_url or "")
    return f"{u.scheme}://{u.netloc}" if u.scheme in ("http", "https") and u.netloc else ""


def pair_expires(now, minutes=29):
    """When a pairing link minted at `now` (epoch seconds, after `t3 pair --ttl 30m`) runs out, as "YYYY-MM-DD HH:MM
    UTC" for the prompt (a minute early: the link was minted just before)."""
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now + minutes * 60))


def ts_facts(status, rec):
    """What the machine's own `tailscale status --json` says about a friend's Tailscale account, for the setup prompt.
    Pure: `status` is that JSON (or None), `rec` their users.json record. Returns {"login", "mode"}:
    mode "sharee" (the machine is shared with them and has seen their devices), "member" (they are in the owner's
    tailnet), "reverse" (only their own computer is shared INTO the owner's tailnet: the machine can reach it, but the
    box isn't shared back), "none" (no device of theirs seen) or "" (unknown)."""
    rec = rec or {}
    login, ts_id = rec.get("login") or "", str(rec.get("ts_id") or "")
    if "@" not in login:  # "cloudflare" (invited by email): not a Tailscale account name
        login = ""
    if not isinstance(status, dict):
        return {"login": login, "mode": ""}
    users = status.get("User") or {}
    if not ts_id:  # an email-invited friend: match their email (or a Tailscale login on record) against the machine's
        names = {x.lower() for x in (rec.get("email") or "", login) if x}
        ts_id = next((str(k) for k, u in users.items() if (u.get("LoginName") or "").lower() in names), "")
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
    """The "What the machine knows" lines of the setup prompt (only facts the machine really has)."""
    out = []
    if ts_login:
        out.append({
            "sharee": f"- The machine is shared with the Tailscale account {ts_login}, and it has seen that account's devices.",
            "member": f"- {ts_login} is a member of the machine owner's tailnet.",
            "reverse": f"- The machine is NOT shared with the Tailscale account {ts_login} yet. (A computer of that account "
                       f"is shared with the owner, which only lets the machine reach it, not the other way round.)",
            "none": f"- The machine hasn't seen any device of the Tailscale account {ts_login} yet. That's normal if "
                    f"Tailscale isn't set up on any computer yet; otherwise the share isn't accepted, or this computer "
                    f"uses another account.",
        }.get(ts_mode, f"- The owner may have shared the machine with the Tailscale account {ts_login} (not confirmed)."))
    for a in apps or ():
        bits = ", ".join(b for b in (a.get("os"), a.get("ip") and "Tailscale IP " + a["ip"],
                                     a.get("version") and "app " + a["version"]) if b)
        when = f", last connected {a['last']}" if a.get("last") else ""
        out.append(f"- A T3 app paired with my T3 earlier ({bits or 'details unknown'}{when}).")
    return "\n".join(out)


def app_prompt(pair_url, dashboard, box="the machine", *, ts_login="", ts_mode="", apps=(), expires=""):
    """The prompt for the friend's own agent on their laptop: check whether the app already works, diagnose
    read-only, fix only what's broken (Tailscale, then the T3 Code app), then open the one-time pairing link and ask
    the human what the app showed (the agent can't see it). `dashboard` is where they make a new one
    ({site}/_box/me); `box` is the machine's name (prose only: shared-in users can't resolve it, addresses use the IP).
    Optional facts from the machine: `ts_login`/`ts_mode` (ts_facts), `apps` (T3 app sessions paired earlier: dicts
    with os, ip, version, last) and `expires` (when the pairing link runs out, e.g. "2026-11-14 18:42 UTC")."""
    origin = app_origin(pair_url)
    if not origin:
        return ""
    ip = urllib.parse.urlsplit(origin).hostname
    facts = _facts(ts_login, ts_mode, apps)
    facts = f"\nWhat the machine knows:\n{facts}\n" if facts else ""
    has_box = ts_login and ts_mode in ("sharee", "member")  # only these prove that account can see the machine
    who = (f"{ts_login} (the account that has the machine)" if has_box else
           f"the account that accepted the share of {box}, usually the one I use for Tailscale")
    hint = {"sharee": f" The machine is shared with {ts_login}: if this computer's account is another one, that is the "
                      f"cause; tell me both and ask which to use.",
            "member": f" The machine's tailnet has {ts_login}: if this computer's account is another one, that is the "
                      f"cause; tell me both and ask which to use.",
            "reverse": f" The machine isn't shared with {ts_login}: if that is this computer's account, ask me whether I "
                       f"have an email from Tailscale about {box}; if not, go straight to the message for the owner "
                       f"below."}.get(ts_mode, "") if ts_login else ""
    until = f"expires at {expires} (check the clock with date -u)" if expires else "lasts 30 minutes"
    seen = " An app paired earlier (see above) makes this likely." if apps else ""
    return f"""Connect the T3 Code desktop app on this computer to my T3 on treehouse ({box}), a shared Linux machine, over Tailscale. Do the work yourself; I only sign in, approve permission prompts, click Confirm, or type this computer's password. You can't see the apps' windows: when something should appear, ask me what I see. Look before you change anything and stop as soon as it works. Never repeat a step that failed unless you changed something since; after two rounds that change nothing, stop. Never sign out of or remove a Tailscale account, never use --reset or --force-reauth, don't set up T3 Connect, and don't change anything else on this computer. If your own sandbox blocks the network or a command, ask me to allow it: that isn't a Tailscale problem.

My T3: {origin} (always use this IP address; the name {box} doesn't resolve for shared devices). The pairing link below works once and {until}.
{facts}
0. Already working? If the T3 Code app is installed, ask me: "In the T3 app, open Settings, Environments. Is there a {box} entry with the address {origin} that shows as connected, and do your threads open?" If yes, stop: it's done, change nothing.{seen}
1. Look, read-only. Work out the OS, then:
   a. curl -s --noproxy '*' --max-time 15 {origin}/.well-known/t3/environment   (Windows: curl.exe). A JSON reply with environmentId means the machine is reachable: go to step 3. Anything else (nothing, or a proxy's page) means it isn't.
   b. Find the Tailscale command: tailscale on PATH, else /Applications/Tailscale.app/Contents/MacOS/Tailscale (macOS), else & 'C:\\Program Files\\Tailscale\\tailscale.exe' (PowerShell, keep the &). Run tailscale status --json and note BackendState, this computer's account (LoginName of the User entry whose ID is Self.UserID), and whether a Peer has {ip} in TailscaleIPs (no Peer list means no).
2. Fix Tailscale (only when 1a failed). Fix what 1b found, then run 1b again and handle the new state: installing, starting, signing in and the peer check are one chain. Run the curl again only once BackendState is Running and the peer is there.
   - Not installed: macOS: curl -fLo ~/Downloads/Tailscale.pkg https://pkgs.tailscale.com/stable/Tailscale-latest-macos.pkg and open it; I click through the installer and type my password. Windows: winget install --id Tailscale.Tailscale -e (I click Yes). Linux: unless you can get root without a password, give me ONE line to paste in a terminal that runs https://tailscale.com/install.sh, then tailscale set --operator=$USER as root, then tailscale up. Never install a second Tailscale next to an existing one.
   - The command can't talk to Tailscale: start it (macOS: open -a Tailscale; Windows: Tailscale from the Start menu; Linux: the tailscaled service). If it's already open on macOS, its setup isn't finished: ask me to finish the Tailscale window (Allow the VPN, or System Settings, Privacy and Security, Allow) and wait for me.
   - Linux, a tailscale command says Access denied: give me ONE line to paste that runs tailscale set --operator=$USER as root, then go on.
   - Stopped: tailscale up. If it asks for more flags, don't add any: ask me to turn Tailscale on from its menu-bar or tray icon.
   - NeedsLogin: start tailscale up in the background (tailscale login if tailscale switch --list shows other accounts, so they stay), show me the login link it prints (or AuthURL in tailscale status --json), and wait until BackendState is Running. Tell me to sign in as {who}.
   - NeedsMachineAuth: an admin of that tailnet must approve this computer. Tell me and stop.
   - Running, no peer {ip}: wait a minute (a new sign-in takes a moment) and check again. Still none: this account can't see the machine.{hint} If tailscale switch --list shows another account that has the machine, ask me before switching; say it turns off my other Tailscale (for example work) until I switch back, and how to switch back (Tailscale menu, or tailscale switch with the old account). Never switch on a computer my employer manages. Add an account only with tailscale login, and only if I agree. Otherwise tell me: "Find the email from Tailscale about {box} (check spam and other inboxes) and click Accept. Sign in as <this computer's account>, with the same Google, GitHub or Microsoft button this computer's Tailscale uses, even if the email went to another address; if the page shows someone else signed in, sign out there first. Tell me when you're done." Whichever account accepts gets the machine; on a work tailnet only its admins can accept. Wait for me, then check again.
   - Running, peer {ip} there, curl fails: tailscale ping --c 3 --timeout 3s {ip}. A pong means Tailscale works and a proxy, firewall or work tailnet's rules block the port. No reply: the machine or this computer is offline, or another VPN or a work tailnet's rules block it. Ask me before touching another VPN.
   If it still times out, stop and tell me what blocks it. Only when this account can't see the machine and no share is waiting for me, add this for the owner: "Please share {box} with my Tailscale account <login>." (<login> is the account this computer will use: the one from 1b, or the one I chose.) Then I make a new prompt at {dashboard}. Meanwhile T3 works in the browser: {dashboard}, Open T3.
3. The T3 Code app. Find it: macOS: /Applications or ~/Applications, T3 Code*.app (version: defaults read with its Contents/Info and CFBundleShortVersionString; older than 0.1.9 needs an update). Windows: winget list --id T3Tools.T3Code -e, or the Start menu. Linux: dpkg -s t3-code, the Exec line of ~/.local/share/applications/com.t3tools.T3Code.desktop, or a T3*Code*.AppImage in my home folder. Not sure: ask me before installing. Missing: macOS: brew install --cask t3-code if brew exists, else the .dmg for this CPU (uname -m) from https://github.com/pingdotgg/t3code/releases/latest, copied into /Applications. Windows: winget install --id T3Tools.T3Code -e (updates: winget upgrade). Linux: the AppImage for this CPU from https://t3.codes, kept in ~/Applications (the .deb needs root). Not t3.codes' install.sh: that is only the server. Then start it detached (macOS: open -a with the app; Linux: setsid with its path, output discarded, in the background; Windows: Start menu) and ask me whether its window is up. I can skip its welcome screens.
4. Pair. First check that the link can open the app. Windows: if reg query HKCU\\Software\\Classes\\t3code and reg query HKCR\\t3code both find nothing, go to step 5. Linux: xdg-mime query default x-scheme-handler/t3code must print a .desktop name (the app registers it when it starts); nothing: go to step 5.
   Tell me: "The T3 app should ask: Add the backend at {origin}? Click Confirm." Then open this link with the system link opener (macOS: open, Linux: xdg-open, Windows: PowerShell's Start-Process), keeping the single quotes:
   '{t3_app_link(pair_url)}'
   Then ask me what the app showed:
   - "Backend added": done.
   - Nothing within 30 seconds of its window being up: open the link once more (only Confirm uses it up). Still nothing: step 5.
   - "already saved": the app has {box} under another address. If that entry connects, stop: nothing to fix. Otherwise copy the pairing URL to my clipboard and tell me: "In the T3 app, go to Settings, Environments, open {box} (the one whose address isn't {origin}), click Add route, paste this into Host and click Add route: {pair_url} Then click Remove route on every address that isn't {origin}."
   - Anything about updating or versions: update the app (step 3), start it again and reopen the same link (a refused link isn't used up).
   - Any other message: the link probably expired or was used. Ask me for a new prompt; don't reuse it in step 5.
5. If the link can't open the app, copy the pairing URL to my clipboard (pbcopy, clip, wl-copy or xclip) and tell me: "In the T3 app, go to Settings, Environments, Add environment, pick Remote link, and paste this into Host: {pair_url} Then click Add environment."

Never open the link in a web browser: that uses it up on the browser. If it has expired or was used, ask me to make a new prompt at {dashboard}. Each computer needs its own prompt.
At the end, tell me in one line: it works, or what blocks it and the Tailscale account this computer uses (and how to switch back, if you switched accounts).
"""



def _fleet_line(m, ts_login):
    """One machine's lines in the all-machines prompt: its address and pairing link, or why there is none."""
    box = m.get("box") or "?"
    origin = app_origin(m.get("pair_url"))
    if not origin:
        why = m.get("error") or "it didn't answer"
        return (f"- {box}: no pairing link ({why.rstrip('.')}). Don't try to reach it. In the final report, the fix is: "
                f"\"Ask the owner to check {box}\" if it says offline or not connected, otherwise make a new prompt "
                f"later.")
    out = [f"- {box}: {origin}", f"  link: '{t3_app_link(m['pair_url'])}'", f"  pairing URL: {m['pair_url']}"]
    mode = m.get("ts_mode") or ""
    if ts_login and mode in ("sharee", "member"):
        out.append(f"  Tailscale: {box} has seen devices of {ts_login}, so that account can reach it.")
    elif ts_login and mode in ("reverse", "none"):
        out.append(f"  Tailscale: {box} hasn't seen any device of {ts_login} yet (not shared with it yet, the share "
                   f"isn't accepted, or this computer uses another account).")
    if m.get("apps"):
        out.append(f"  A T3 app paired with {box} before ({', '.join(a.get('os') or '?' for a in m['apps'])}).")
    return "\n".join(out)


def fleet_prompt(machines, dashboard, *, ts_login="", expires=""):
    """The setup prompt for the agent on the friend's laptop that connects the T3 Code app to their T3 on EVERY
    treehouse machine their account is on, from one paste. `machines`: [{box, pair_url, ts_mode, apps, error}]
    (pair_url empty + error: no link from that machine). Same rules as app_prompt (look first, fix only what's
    broken, never open a link in a browser), but per machine, and it ends with one line per machine: works, or why
    not and the fix. "" when no machine gave a link."""
    ok = [m for m in machines if app_origin(m.get("pair_url"))]
    if not ok:
        return ""
    names = ", ".join(m.get("box") or "?" for m in machines)
    ips = " ".join(urllib.parse.urlsplit(app_origin(m["pair_url"])).hostname for m in ok)
    until = f"expire at {expires} (check the clock with date -u)" if expires else "last 30 minutes"
    acct = (f"{ts_login}, the Tailscale account the machines know for me" if ts_login else
            "the account that accepted the machines' shares, usually the one I use for Tailscale")
    lines = "\n".join(_fleet_line(m, ts_login) for m in machines)
    return f"""Connect the T3 Code desktop app on this computer to my T3 on each of my treehouse machines ({names}): shared Linux machines, reached over Tailscale. Do the work yourself; I only sign in, approve permission prompts, click Confirm, or type this computer's password. You can't see the apps' windows: when something should appear, ask me what I see. Look before you change anything. Never repeat a step that failed unless you changed something since; after two rounds that change nothing, stop. Never sign out of or remove a Tailscale account, never use --reset or --force-reauth, don't set up T3 Connect, and don't change anything else on this computer. If your own sandbox blocks the network or a command, ask me to allow it: that isn't a Tailscale problem. One machine failing never stops the others: finish every machine you can, then report.

My machines. Always use these IP addresses (the names don't resolve for shared devices). Each link works once and the links {until}:
{lines}

0. Already working? If the T3 Code app is installed, ask me: "In the T3 app, open Settings, Environments. Which of these show as connected: {names}?" Machines that are connected at the address above are done: skip them everywhere below and don't open their links.
1. Look, read-only. Work out the OS, then:
   a. For each machine: curl -s --noproxy '*' --max-time 15 <its address>/.well-known/t3/environment   (Windows: curl.exe). A JSON reply with environmentId means it's reachable.
   b. Find the Tailscale command: tailscale on PATH, else /Applications/Tailscale.app/Contents/MacOS/Tailscale (macOS), else & 'C:\\Program Files\\Tailscale\\tailscale.exe' (PowerShell, keep the &). Run tailscale status --json and note BackendState, this computer's account (LoginName of the User entry whose ID is Self.UserID), and which of these IPs are in some Peer's TailscaleIPs: {ips}
2. Fix Tailscale, only if a curl failed. Fix what 1b found, run 1b again and handle the new state; then run the failed curls again.
   - Not installed: macOS: curl -fLo ~/Downloads/Tailscale.pkg https://pkgs.tailscale.com/stable/Tailscale-latest-macos.pkg and open it; I click through the installer and type my password. Windows: winget install --id Tailscale.Tailscale -e (I click Yes). Linux: unless you can get root without a password, give me ONE line to paste in a terminal that runs https://tailscale.com/install.sh, then tailscale set --operator=$USER as root, then tailscale up. Never install a second Tailscale next to an existing one.
   - The command can't talk to Tailscale: start it (macOS: open -a Tailscale; Windows: Tailscale from the Start menu; Linux: the tailscaled service). If it's already open on macOS, ask me to finish the Tailscale window (Allow the VPN, or System Settings, Privacy and Security, Allow) and wait for me.
   - Linux, Access denied: give me ONE line to paste that runs tailscale set --operator=$USER as root.
   - Stopped: tailscale up. If it asks for more flags, don't add any: ask me to turn Tailscale on from its menu-bar or tray icon.
   - NeedsLogin: start tailscale up in the background (tailscale login if tailscale switch --list shows other accounts, so they stay), show me the login link it prints (or AuthURL in tailscale status --json), wait until BackendState is Running. Tell me to sign in as {acct}.
   - NeedsMachineAuth: an admin of that tailnet must approve this computer. Tell me and stop.
   - Running, but a machine's IP is in no Peer: wait a minute and check again (a new sign-in or share takes a moment). Still missing: this account can't see that machine. If tailscale switch --list shows another account that has it, ask me before switching (it turns off my other Tailscale until I switch back; never on a computer my employer manages). Otherwise ask me: "Do you have an email from Tailscale about <machine>? Click Accept, signed in as <this computer's account> with the same Google, GitHub or Microsoft button this computer's Tailscale uses. Tell me when you're done." No email: that machine isn't shared with this account; the fix is the line for the owner in the report.
   - Running, peer there, curl fails: tailscale ping --c 3 --timeout 3s <its IP>. A pong: a proxy, firewall or work tailnet's rules block the port. No reply: that machine or this computer is offline, or another VPN blocks it. Ask me before touching another VPN.
3. The T3 Code app, if any machine is reachable. Find it: macOS: /Applications or ~/Applications, T3 Code*.app (version: defaults read with its Contents/Info and CFBundleShortVersionString; older than 0.1.9 needs an update). Windows: winget list --id T3Tools.T3Code -e, or the Start menu. Linux: dpkg -s t3-code, the Exec line of ~/.local/share/applications/com.t3tools.T3Code.desktop, or a T3*Code*.AppImage in my home folder. Not sure: ask me before installing. Missing: macOS: brew install --cask t3-code if brew exists, else the .dmg for this CPU (uname -m) from https://github.com/pingdotgg/t3code/releases/latest, copied into /Applications. Windows: winget install --id T3Tools.T3Code -e. Linux: the AppImage for this CPU from https://t3.codes, kept in ~/Applications. Not t3.codes' install.sh: that is only the server. Start it detached and ask me whether its window is up. I can skip its welcome screens.
4. Pair each reachable machine, one at a time. First check that links can open the app (Windows: reg query HKCU\\Software\\Classes\\t3code or HKCR\\t3code finds something; Linux: xdg-mime query default x-scheme-handler/t3code prints a .desktop name); if not, use step 5 for every machine. For each machine: tell me "The T3 app should ask: Add the backend at <its address>? Click Confirm." Then open its link with the system link opener (macOS: open, Linux: xdg-open, Windows: PowerShell's Start-Process), keeping the single quotes, and ask me what the app showed:
   - "Backend added": that machine is done.
   - Nothing within 30 seconds: open the same link once more (only Confirm uses it up). Still nothing: step 5 for that machine.
   - "already saved": the app has that machine under another address. If that entry connects, it's done. Otherwise copy its pairing URL to my clipboard and tell me: "In the T3 app, go to Settings, Environments, open <machine>, click Add route, paste this into Host and click Add route. Then click Remove route on every address that isn't <its address>."
   - Anything about updating or versions: update the app (step 3), start it again and reopen the same link.
   - Any other message: that link probably expired or was used; don't reuse it.
5. If a link can't open the app: copy that machine's pairing URL to my clipboard (pbcopy, clip, wl-copy or xclip) and tell me: "In the T3 app, go to Settings, Environments, Add environment, pick Remote link, paste this into Host and click Add environment."

Never open a link in a web browser: that uses it up there. Each computer needs its own prompt.
At the end, report one line per machine: connected (or already connected), or not connected with the reason and the fix. Fixes: not shared on Tailscale: "Ask the owner: please share <machine> with my Tailscale account <this computer's account>." Link expired or used: "Make a new prompt at {dashboard} (Advanced, Get setup instructions)." Machine offline or blocked: what you saw, and "ask the owner to check <machine>". Also say which Tailscale account this computer uses, and how to switch back if you switched.
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
                f"SSH through the public site isn't turned on for this machine yet. Tell the human: \"ask the owner to "
                f"turn on SSH for the public site, then copy these instructions again from {site}/_box/me\". Don't "
                "try other ways in (no VPN, no port forwarding).\n")
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
It should print {user}'s usage and limits on the machine. "Permission denied (publickey)": step 4 didn't work.
A browser opening: Cloudflare wants the login again; tell the human to finish it.

## 7. Tell the human what they can do now
- "Run X on my treehouse box": you run `ssh {alias} '<command>'` for them.
- `ssh -t {alias} claude` (or `codex`) starts a coding agent that lives on the machine.
- VS Code / Cursor: Remote-SSH, "Connect to Host...", pick `{alias}`.
- Every few weeks Cloudflare asks for the email code again; a browser tab opens by itself when it does.
- The website is still {site} (their dashboard: {site}/_box/me).

## Rules when you work on the box
- It's a shared machine with no sudo for anyone. Never run `sudo` or ask for root/admin. Read
  `ssh {alias} 'box rules'` once before doing real work there and follow it.
- Files live on the machine, not on this computer: edit them there (`ssh`, Remote-SSH) or move them with
  `scp`/`git`. Keep each project as a git repo in `~/<name>/` and push it: idle projects are archived only once fully pushed.
- Anything long-running goes in `box serve add <name> -- <command>` (or `box shared` for something the whole
  group runs). Never leave servers in `nohup`, `tmux` or `&`: the machine reboots for updates.
- Memory: each account has a guaranteed share and may use most of the machine while it's quiet; `ssh {alias} box running`
  shows what's using it, `box help memory` explains. `box help` lists more topics.

## 8. The T3 Code app on this computer, for every treehouse machine
If the human wants the T3 Code desktop app here (or asked to "connect me to all my treehouse machines"): run
`ssh {alias} box t3 app-link --all --prompt` and follow the steps it prints. It covers every treehouse machine their
account is on: it checks Tailscale here first, pairs the app with each machine, and ends with a line per machine.
(`box t3 app-link --prompt` instead: only the machine `ssh {alias}` reaches.)
"""


# ---------- teammates' agents in your T3 (`t3 environment link`, set up from the dashboard) ----------
# Alice lets Bob's agents work in her T3: her T3 mints a one-time pairing code labelled "<bob>'s agents (treehouse)",
# and Bob's T3 links her T3 with it (`t3 environment link URL --pairing-code CODE --access LEVEL`). Bob's agents then
# see her T3 as a linked environment. She stops it by revoking that session; Bob's side forgets the link.

TEAM_ACCESS = {"read-only": "Look only", "approval-required": "Ask before changes", "full-access": "Full access"}
TEAM_SUFFIX = "'s agents (treehouse)"


def team_label(to):
    return f"{to}{TEAM_SUFFIX}"


def split_pair_url(url):
    """http://ip:port/pair#token=abc -> ("http://ip:port", "abc"); ("", "") when it isn't one."""
    u = urllib.parse.urlsplit(url or "")
    tok = urllib.parse.parse_qs(u.fragment).get("token", [""])[0]
    if u.scheme not in ("http", "https") or not u.netloc or not tok:
        return "", ""
    return f"{u.scheme}://{u.netloc}", tok


def team_grants(sessions):
    """`t3 auth session list --json` -> [{"to", "session", "since", "last"}]: the teammates whose agents can work in
    this T3 now (sessions labelled by team_label, not revoked)."""
    out = []
    for s in sessions if isinstance(sessions, list) else []:
        if not isinstance(s, dict):
            continue
        label = str((s.get("client") or {}).get("label") or "")
        if not label.endswith(TEAM_SUFFIX) or s.get("revokedAt"):
            continue
        out.append({"to": label[:-len(TEAM_SUFFIX)], "session": str(s.get("sessionId") or ""),
                    "since": str(s.get("issuedAt") or "")[:10], "last": str(s.get("lastConnectedAt") or "")[:10]})
    return [g for g in out if g["session"]]


def team_links_to(envs, origin):
    """`t3 environment list --json` -> ids of the linked environments reached at `origin`."""
    if not isinstance(envs, list):
        return []
    return [str(e.get("environmentId")) for e in envs if isinstance(e, dict) and e.get("environmentId") and any(str(u).rstrip("/") == origin.rstrip("/") for u in e.get("urls") or [])]


# ---------- reading a login screen ----------

_URL_PART = re.compile(r"[A-Za-z0-9%&=._~:/?#+@!$'()*,;\[\]-]+")


def screen_urls(screen):
    """https:// links on a terminal screen, put back together when the program hard-wrapped them at the
    terminal's width (Claude's sign-in link is ~330 characters: `tmux capture-pane -J` only joins tmux's own
    wraps, so the rest of the link sits alone on the next line). A program that indents the link (Antigravity's
    full-screen sign-in puts one space before every line) indents the rest of it the same way."""
    lines = (screen or "").splitlines()
    out = []
    for i, line in enumerate(lines):
        indent = line[:len(line) - len(line.lstrip())]
        for m in re.finditer(r"https://\S+", line):
            url = m.group(0)
            # it ran to the end of a long line: the next line(s) of bare link characters are the rest of it
            wrapped = m.end() == len(line.rstrip()) and len(line.rstrip()) >= 80
            j = i + 1
            while wrapped and j < len(lines):
                nxt = lines[j]
                if not nxt.strip() or nxt[:len(nxt) - len(nxt.lstrip())] != indent \
                        or not _URL_PART.fullmatch(nxt.strip()):
                    break
                url += nxt.strip()
                j += 1
            out.append(url.rstrip(".,)"))
    return out
