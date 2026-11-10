Set me up on my friend's shared dev machine, end to end. The join link is: {{JOIN_LINK}}

Your job: do every step yourself. I should only have to (a) answer the questions in step 0, (b) click through
browser logins, and (c) paste a code back to you if a login page shows one. Don't hand me commands to run.
Don't stop to report progress. Keep going until step 9 is done or you're truly blocked.

Ground rules:
- You need network access (to a Tailscale address), write access to `~/.ssh`, and long-running commands. If your
  sandbox blocks any of these, ask me to approve running the command outside the sandbox. A sandbox error does not
  mean the machine is unreachable.
- The commands are bash. On Windows, use Git Bash or WSL if available. Otherwise translate them for PowerShell
  (`curl.exe` instead of the `curl` alias, `$env:USERPROFILE` instead of `$HOME`, `start` instead of `open`).
- **Opening links for me:** whenever I need a web page, open it in my default browser yourself (`open <url>` on
  macOS, `xdg-open <url>` on Linux, `start "" "<url>"` on Windows). Also print the URL in case that fails.
  Never fetch login or pairing links with curl or your own browser tool: that can use up a one-time link.
- **Interactive commands on the machine** (logins that print a URL and then wait for a pasted code): run them
  inside tmux on the machine, so you can read and type into them:
  ```
  ssh devbox 'tmux kill-session -t login 2>/dev/null; tmux new-session -d -s login -x 200 -y 50'
  ssh devbox 'tmux send-keys -t login "<command>" Enter'
  ssh devbox 'tmux capture-pane -p -J -t login -S -100'      # read the screen; repeat every few seconds
  ssh devbox 'tmux send-keys -t login "<text>" Enter'          # answer a prompt / paste a code
  ```
  Read the screen after every action. Answer menus with the arrow keys/Enter (`send-keys Down`, `send-keys Enter`).
  When a URL shows up, open it for me. If the page gives me a code to paste back, ask me for it and type it in.
  If the tool shows a code for me to enter on the page, tell me the code. A long URL may be wrapped across lines:
  join the lines and drop the spaces before opening it.
- Don't copy tokens, API keys, or credential files from this computer to the machine. Logins happen on the machine.

0. **Ask me everything up front, in one message:**
   - Which username I want on the machine. Suggest my local username lowercased. Rules: 3–16 characters,
     lowercase letters and digits only, starting with a letter.
   - Which coding agents to log into: Claude Code (Claude subscription, or Anthropic Console for API billing),
     Codex (ChatGPT account), opencode. Default: the ones I use on this computer (check whether `claude`,
     `codex` or `opencode` are installed here).
   - Whether to log into GitHub on the machine (default yes), and any repos to clone there (optional).
   - My git name and email, but only if `git config --global user.name` / `user.email` aren't set here.
   - Whether the coding agents on the machine should update to the newest version every night (default yes).
     If not, they stay on the versions I get today until I ask for newer ones. Tell me that serious security
     fixes are applied to everyone either way.
   Then carry out steps 1–9 without stopping, except for the browser steps.

1. **Tailscale.** Let HOST be the host part of the join link.
   Run `curl -s -o /dev/null -w '%{http_code}' --max-time 10 {{JOIN_LINK}}`. If it prints `200`, go to step 2.
   Otherwise fix it yourself:
   - Tailscale not installed: install it (macOS: `brew install --cask tailscale` if brew exists; Windows:
     `winget install tailscale.tailscale`; Linux: `curl -fsSL https://tailscale.com/install.sh | sh`, which needs
     sudo). If you can't install it, open https://tailscale.com/download for me and wait.
   - Not logged in: run `tailscale up` (or `tailscale login`), open the login URL it prints, and wait for me.
     On macOS/Windows, starting the Tailscale app may be needed first.
   - Logged in but the request still fails: the machine hasn't been shared with this Tailscale account yet.
     Show me which account this computer uses (`tailscale status` / `tailscale whois`). Tell me to accept my
     friend's "shared machine" invite with that account, or to ask them to share it with that account.
   Retry every 15 seconds until you get `200` (give up after 10 minutes and tell me what's wrong).

2. **SSH key.** Use `~/.ssh/id_ed25519.pub` if it exists. Otherwise create one with
   `ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519 -C "$(whoami)@$(hostname)"`. Never overwrite an existing key.

3. **Sign up.**
   ```
   TOKEN=$(curl -s {{JOIN_LINK}} | grep -o 'name="token" value="[^"]*"' | sed 's/.*value="//; s/"$//')
   curl -s -X POST {{JOIN_LINK}} \
     --data-urlencode "token=$TOKEN" \
     --data-urlencode "username=<USERNAME>" \
     --data-urlencode "key@$HOME/.ssh/id_ed25519.pub"
   ```
   - "Welcome, <username>": success. Note the port range.
   - "You're already set up": an account exists. Use the username it shows.
   - "Request sent": the owner must approve. Tell me and stop. Rerunning this prompt later picks up from here.
   - Something in `class="err"` (e.g. username taken): fix it and retry. Ask me only if you need a new username.
   - TOKEN empty: the page probably said "Can't identify you". Go back to step 1.

4. **SSH shortcut.** Add this to `~/.ssh/config` (create the file if needed; update an existing `devbox` entry
   instead of adding a second one):
   ```
   Host devbox
       HostName <HOST>
       User <USERNAME>
       IdentityFile ~/.ssh/id_ed25519
       ServerAliveInterval 30
   ```
   Test with `ssh -o StrictHostKeyChecking=accept-new devbox 'box ports'`. The account may take ~30 seconds to
   become ready after signup, so retry for up to 2 minutes if SSH is refused.

5. **Start T3 Code (web UI) in the background now**, since the first run downloads it:
   `ssh devbox 'box t3' > /tmp/devbox-t3.log 2>&1 &` (or your tool's background mode). Carry on with steps 6–7
   while it runs.

6. **Git and updates on the machine.**
   `ssh devbox "git config --global user.name '<name>' && git config --global user.email '<email>' && git config --global init.defaultBranch main"`
   using my local git identity (or what I told you in step 0). Then record my update choice:
   `ssh devbox 'box updates auto'` (or `box updates manual` if I said no).

7. **Logins on the machine.** For each one, use the tmux method above, open the URL for me, wait until the check
   passes, then move to the next. Kill the `login` tmux session when you're done.
   - **GitHub:** `gh auth login --hostname github.com --git-protocol https --web`. When it shows a one-time
     code, tell me the code and open https://github.com/login/device. Then run `ssh devbox 'gh auth setup-git'`.
     Check: `ssh devbox 'gh auth status'`.
   - **Claude Code:** `claude auth login` (add `--console` if I chose API billing). Open the URL. When I paste
     the code from the browser to you, type it in. Check: `ssh devbox 'claude auth status --text'`.
   - **Codex:** `codex login --device-auth`. Tell me the code and open the URL it shows.
     Check: `ssh devbox 'codex login status'`.
   - **opencode:** `opencode auth login`. Pick the provider I want from the menu, then follow its prompts.
     Check: `ssh devbox 'opencode auth list'`.
   If a login fails twice, skip it, carry on, and mention it at the end (my agent in T3 Code can redo it for me later).
   Then run `ssh devbox 'mkdir -p ~/.sharedbox && touch ~/.sharedbox/setup-done && box setup --check'`.

8. **Repos and T3 pairing.**
   - Clone any repos I asked for: `ssh devbox 'cd ~/projects && gh repo clone <owner/repo>'`
     (or `git clone <url>` if GitHub login was skipped).
   - Wait for step 5 to finish and get the `T3_PAIR_URL=<link>` line from its output. If it failed or the link is
     over 30 minutes old, run `ssh devbox 'box t3 pair'` for a new one. Open the pairing link in my browser.
     After pairing, my T3 Code is at `http://<HOST>:<first port of my range>/`.

9. **Finish: a short, friendly summary** with the real values:
   - **You're in.** Username, port range, `ssh devbox` connects. Which logins worked (for any that
     didn't, just ask the agent in T3 Code to log me in).
   - **Your T3 Code: `http://<HOST>:<first port>/`. Bookmark it.** This is the main way to work. Start a thread,
     pick Claude or Codex, and watch it work, with diffs, parallel threads and a terminal. Agents keep running when
     I close the browser or my laptop sleeps. Any device on my Tailscale can use it after pairing (new pairing
     link: `ssh devbox 'box t3 pair'`). Projects live in `~/projects/` on the machine.
   - **Terminal alternative:** `ssh devbox`, `tmux new -A -s main`, then
     `mkdir -p ~/projects/myapp && cd ~/projects/myapp && claude` (or `codex` / `opencode`). Detach with `Ctrl-b d`.
   - **House rules** (the agents on the machine already know them): projects untouched for 7 days are
     auto-deleted, so push to git. Builds go through `box heavy -- <cmd>`, and servers I want to keep running go
     through `box serve`. `box` shows my usage and limits.
