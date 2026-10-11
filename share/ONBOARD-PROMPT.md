Set me up on my friend's shared dev machine. The join link is: {{JOIN_LINK}}

Do these steps on THIS computer (my laptop). Run commands yourself, and only stop to ask me where it says so.
- You need network access (to a Tailscale address) and write access to `~/.ssh`. If your sandbox blocks either,
  ask me to approve running the command outside the sandbox. Don't conclude the machine is unreachable because
  of a sandbox error.
- The commands below are bash. On Windows, run them in Git Bash or WSL if available. Otherwise translate them for
  PowerShell (use `curl.exe`, not the `curl` alias, and `$env:USERPROFILE` instead of `$HOME`).

1. **Reach the machine over Tailscale.**
   Let HOST be the host part of the join link (e.g. `100.x.y.z` or `name.something.ts.net`).
   Run `curl -s -o /dev/null -w '%{http_code}' --max-time 10 {{JOIN_LINK}}`.
   - If it prints `200`, continue.
   - If it fails: check `tailscale status`. If Tailscale isn't installed or isn't logged in, stop and tell me to
     install it from https://tailscale.com/download, log in, and accept the "shared machine" invite from my
     friend. Then retry. Don't continue until you get `200`.
   - If Tailscale is running but the request still fails, the invite was probably accepted with a different
     Tailscale account than the one this computer is logged into (`tailscale status` shows the account). Tell me to
     accept the invite with this account, or to log this computer into the account that accepted it.

2. **SSH key.** Use `~/.ssh/id_ed25519.pub` if it exists. If it doesn't, create one with
   `ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519 -C "$(whoami)@$(hostname)"`. Never overwrite an existing key.
   (On Windows, use the same paths under `%USERPROFILE%\.ssh` in PowerShell.)

3. **Username.** Ask me which username I want on the machine. Suggest my local username lowercased.
   Rules: 3–16 characters, lowercase letters and digits only, starting with a letter.

4. **Sign up.** Get a form token, then submit:
   ```
   TOKEN=$(curl -s {{JOIN_LINK}} | grep -o 'name="token" value="[^"]*"' | sed 's/.*value="//; s/"$//')
   curl -s -X POST {{JOIN_LINK}} \
     --data-urlencode "token=$TOKEN" \
     --data-urlencode "username=<USERNAME>" \
     --data-urlencode "key@$HOME/.ssh/id_ed25519.pub"
   ```
   Read the HTML response:
   - "Welcome, <username>" means success. Note the port range it shows.
   - "You're already set up" means an account already exists. Use the username it shows.
   - "Request sent" means the owner must approve. Tell me, and stop here. I'll rerun this later.
   - Anything in `class="err"` (e.g. username taken) means the attempt failed. Show me the error, fix it (ask me for a
     new username if needed), and retry.
   - If TOKEN is empty, the page probably said "Can't identify you". Go back to step 1.

5. **SSH shortcut.** Add this to `~/.ssh/config` (create the file if needed, and don't duplicate an existing entry):
   ```
   Host devbox
       HostName <HOST>
       User <USERNAME>
       IdentityFile ~/.ssh/id_ed25519
       ServerAliveInterval 30
   ```
   Then test with `ssh -o StrictHostKeyChecking=accept-new devbox 'box setup --check; box ports'`.

6. **Start my T3 Code web UI on the machine.** Run `ssh devbox 'box t3'` (the first run downloads T3 and can take a
   minute or two). Its output ends with a line `T3_PAIR_URL=<link>`. That link is valid for 30 minutes and
   lets my browser into my own private T3 Code server on the machine. Keep it for step 8. Don't open it yourself.

7. **Set up git on the machine** (non-interactive parts only):
   - Read my git identity locally (`git config --global user.name` / `user.email`). If both are set, copy them over:
     `ssh devbox "git config --global user.name '<name>' && git config --global user.email '<email>'"`.
   - Don't copy any tokens, API keys or credential files to the machine.

8. **Finish: give me a short, friendly guide.** Use the real values (username, HOST, ports, link). Use these
   sections, in this order:

   **You're in.** My username, my port range, and that `ssh devbox` connects me.

   **Step 1: log in once (2 minutes, needs your browser).** Run `ssh devbox`, then `box setup`. It sets up GitHub
   and logs me into my coding agent with my own account: Claude Code (Claude subscription or API key), Codex
   (ChatGPT account), or opencode. I only do this once.

   **Step 2: run your agents. Recommended: T3 Code.** Recommend this clearly as the main way to work:
   - Open my pairing link (the `T3_PAIR_URL` from step 6) in my browser, within 30 minutes. If it expired, get a
     new one with `ssh devbox 'box t3 pair'`.
   - After pairing, bookmark `http://<HOST>:<first port of my range>/`. That's my own T3 Code; nobody else can
     use it.
   - Why it's the best option: it's a proper web UI for coding agents. I start a thread, pick Claude or Codex,
     and watch it work, with diffs, multiple threads in parallel, and a terminal. Agents keep running on the
     machine when I close the browser or my laptop sleeps, and I can check in from any device on Tailscale
     (phone included; pair each new device with `box t3 pair`).
   - Start a project: create it in T3 (it lives in `~/projects/` on the machine), or clone a repo there first
     with `ssh devbox 'cd ~/projects && git clone <repo-url>'`.

   **Alternative: the terminal.** If I prefer a plain CLI:
   `ssh devbox`, then `tmux new -A -s main` (keeps agents running when I disconnect), then
   `mkdir -p ~/projects/myapp && cd ~/projects/myapp && claude` (or `codex`, or `opencode`).
   Detach with `Ctrl-b d`; come back later with `ssh devbox` and `tmux attach`.

   **House rules (the agents on the machine already know them):**
   - Projects untouched for 7 days are auto-deleted, so push to git.
   - Builds go through `box heavy -- <cmd>`, and servers I want to keep running go through `box serve`.
   - `box` shows my usage and limits.
