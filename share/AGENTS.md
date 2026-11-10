# Shared machine rules ({host})

You are running on **{host}**, a machine shared by several people and their AI agents.
User `{user}` has fair-share limits. Exceeding them throttles or kills *your* processes.
Other people's processes are not affected.

## This is a remote machine, not the user's computer
The user is **not sitting at {host}**. They reach you from their own device over SSH or a web UI
(T3 Code) across Tailscale. Keep that in mind for everything you tell them:
- `localhost` / `127.0.0.1` here means {host}, not their device. **Never tell the user to open a
  `localhost` URL.** Give them `http://{host}:<port>` instead.
- Servers must listen on all interfaces (`0.0.0.0`), not just localhost, or the user can't reach them.
  Many dev servers default to localhost: use `vite --host 0.0.0.0`, `next dev -H 0.0.0.0`,
  `python -m http.server --bind 0.0.0.0`, `flask run --host 0.0.0.0`, `uvicorn --host 0.0.0.0`, etc.
- There is no display. Nothing you open appears on the user's screen, so don't run `open`,
  `xdg-open`, `code .`, or GUI apps/emulator windows. Use headless browsers for UI testing and show the
  user a URL, a screenshot file, or test output instead.
- Files you create live on {host}. Paths from the user's computer (`~/Downloads`, `C:\...`,
  `/Users/...`) don't exist here, and the clipboard isn't shared. To hand over a file, push it to git,
  serve it on one of the ports, or give them `scp {user}@{host}:<absolute path> .` to run on their device.
- **Do setup yourself. Don't send the user to a terminal.** Many users only have the chat (T3 Code)
  and aren't technical. Never tell them to SSH in, open a terminal, or run `box setup` or other commands.
  You're already on {host}, so run the commands yourself. All they should ever do is open a link and
  type in a code.
- **Talk plainly.** Assume the user isn't a programmer unless they show you otherwise. Keep messages short,
  skip jargon, and ask for one thing at a time. Do the work, then say what you did and what they can click.
  When you start something they can open, give them the clickable `http://{host}:<port>` link. Offer to put
  their work on GitHub, since projects untouched for {project_ttl} days are deleted.
- Browser-based logins can't open a browser here, so use device-code logins and relay the code.
  GitHub works like this:
  1. Start it in the background, because it waits until the user approves:
     `nohup gh auth login --hostname github.com --git-protocol https --web </dev/null > ~/.gh-login.txt 2>&1 &`
  2. After a few seconds, read `~/.gh-login.txt` and give the user only the one-time code (like `ABCD-1234`)
     and the link https://github.com/login/device. Ignore the "clipboard" warning in that file.
  3. When they say they're done, check `gh auth status`, then run `gh auth setup-git`. If the code
     expired (about 15 min), start again from step 1.
  4. If `git config --global user.name`/`user.email` aren't set, fill them in from `gh api user`
     (`<id>+<login>@users.noreply.github.com` as the email).
  Never ask for passwords or tokens in chat.
- The user's own page **{portal}** (browser, no terminal) logs them in to Claude, Codex and GitHub and
  gives a fresh T3 Code link. Send them there for anything you can't log in to yourself.

| Resource | Your limit |
|---|---|
| CPU | {cpu} (lower priority than the box owner; shared fairly when several people are busy) |
| RAM | {mem_high} soft (past it you're slowed down) / {mem_max} hard (past it your biggest process gets killed) |
| Processes | {tasks} |
| Disk | {disk} budget for your whole home dir |
| Ports | **{ports}**. Bind your servers to these ports only. The first one is reserved for the user's T3 Code server |

## Tools on this machine
Preinstalled system-wide: {tools}.
- There is **no sudo**, so never run `sudo` or a system package manager (`apt`, `pacman`). Install what you need into the home directory instead:
  `npm i -g X` (goes to ~/.local), `uv tool install X` / `uv venv`, `pipx install X`, `cargo install X`
  (run `rustup default stable` first), `go install X@latest`. ~/.local/bin is on PATH.
- If something truly needs a system package, run `box request "install <pkg> (system package): <why>"` and tell the
  user that you did. Don't wait for it; find a workaround in the meantime.
{extras}

## Shared skills
The box has a catalog of agent skills (council reviews, PR helpers, and skills other people here published).
`box skills` lists them and `box skills show NAME` prints one. None are active until installed. When one would
clearly help with what the user asked for, **suggest it and ask the user first**. Only after they agree, run
`box skills add NAME` (and the skills it depends on). It's live in the next agent session. Never install one unasked.

## Before you start
- Run `box status` at the start of a session and before any heavy job. It shows your usage,
  how busy the box is, and any **pending deletions**. If the box is `busy`, avoid heavy
  work unless the user insists.

## Report what slows you down (do this without being asked)
Whenever something about **this machine** costs you time or doesn't work, log it right away with one line and
keep going. Don't ask the user first, and don't stop to wait for a fix:
`box issue --kind <slow|broken|missing|limit|confusing|other> "<what happened> | <what you tried / workaround> | <cost>"`
- Log things like: a missing command or system library, permission denied, an install that failed or crawled,
  a build that was killed or throttled (RAM/CPU limit, `box heavy` queue), a port you couldn't bind, network or
  registry errors, auth that didn't work, the janitor deleting something you needed, and rules here that were
  wrong, unclear or got in the way.
- Don't log bugs in the user's own code, or failing tests you caused.
- One entry per distinct problem. Exact repeats within 10 minutes are dropped. Never include secrets.
- Entries go to `~/.sharedbox/issues.jsonl`, which the box owner reviews to fix the box. `box issues` lists them.

## Where files go (the janitor enforces this)
- `~/projects/<name>/`: put your work here. A project **not touched at all for {project_ttl} days
  (no file read or written) is deleted.** You get {warn} days of warning in `box status`. A project is
  never deleted (and its `node_modules`/build outputs are kept) while a process is running from it, or
  while a `box serve` service points at it, even if that service is stopped.
- `~/services/<name>/`: code for long-running servers. Never deleted automatically, but it counts toward the disk budget.
- `~/keep/`: small files that must survive. Never deleted automatically.
- Caches (`~/.cache`, `~/.npm`, `~/.gradle/caches`, `~/.cargo/registry`, …) unused for {cache_ttl} days are deleted.
  Build outputs (`node_modules`, `build/`, `target/`, `.next`, `.venv`, …) in projects idle for {artifact_ttl} days are deleted.
  Everything regenerable is wiped right away if you go over the disk budget.
- `/tmp` files older than {tmp_ttl} days are deleted. Never keep anything important in `/tmp`.
- Push work to git often. Nothing on this box is backed up.

## Heavy work (builds, compiles, tests, APKs, Docker-like workloads)
- Prefix heavy commands with `box heavy --`, e.g. `box heavy -- ./gradlew assembleDebug`.
  It queues behind other users' heavy jobs ({slots} at a time) and runs at low priority.
- Keep parallelism at **{jobs} jobs**: `make -j{jobs}`, `cargo build -j {jobs}`,
  Gradle `--max-workers={jobs}` (already set in `~/.gradle/gradle.properties`).
- Run one heavy build at a time. Never start parallel builds in multiple terminals.
- Stop dev servers, watchers, emulators, and Gradle daemons (`./gradlew --stop`) when you're done.
- Never put secrets (API keys, tokens) in command-line arguments. Other users can see process lists. Use env vars or files.
- Never mine crypto, port-scan, stress-test, fork-bomb, or touch other users' files or processes.
- There is no sudo and no Docker. Install tools into your home (`~/.local`, nvm, uv, rustup, …).
- The coding agents (`claude`, `codex`, `opencode`, `t3`) are installed and updated by the owner. Don't install
  your own copies or run their self-updaters. `box updates` shows whether this user's agents update nightly
  (`auto`) or stay on their versions (`manual`, `box updates now` to move up). Change it only when the user asks.

## Reboots
{updates}
**reboots at about {reboot_time} (machine local time)**. {reboot_days} `box status` warns ahead of every reboot.
- Things that come back by themselves: `box serve` services, the user's `box t3` server, and everything on disk.
- Things that are lost: processes in `tmux`/`screen`/`nohup`/`&`, terminal sessions, running builds and tests,
  `box heavy` jobs, and anything held only in memory. Anything long-lived must be a `box serve` service.
- Servers should be safe to restart at any time: keep state in files or a database, not in memory.

## Long-running servers
- Use `box serve add <name> --port <port> -- <command…>` (run it inside the project dir). It
  survives logout and reboots and restarts on crash. Manage with `box serve ls|logs|restart|stop|rm <name>`.
  The port is also in `$PORT`. Make the server listen on `0.0.0.0`.
- While the service exists, its project is protected from the janitor. `box serve rm` removes that
  protection, so only remove services the user no longer wants.
- Don't leave servers running inside `nohup`/`tmux` forever. Use `box serve` so the owner can see them.
- Other people on the tailnet reach your servers at `http://{host}:<port>`.

{notices}
