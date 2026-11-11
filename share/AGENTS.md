# Shared machine rules ({host})

You run on **{host}**, a remote Linux box shared by several people and their AI agents, as user `{user}`.
Details for any topic below: `box help <topic>` (`box help` lists topics). Read one before doing that kind of work.

## The user is not at this machine
- They reach you from their own device (T3 Code / SSH over Tailscale; they can also connect their own laptop's
  agent: `box help connect`). `localhost` means {host}: never give them a `localhost` URL. Give
  `http://{host}:<port>`, and make servers listen on `0.0.0.0`.
- No display: don't run `open`/`xdg-open`/GUI apps. Show a URL, a screenshot file or test output instead.
- **Do setup yourself; never send the user to a terminal** or tell them to SSH in or run commands. All they should
  ever do is click a link or type a code. Logins (GitHub, etc.) use device codes: `box help logins`.
  Their page **{portal}** logs them in to Claude/Codex/GitHub without a terminal.
- Talk plainly and briefly; assume they aren't a programmer. Never ask for passwords or tokens in chat.

## Limits (exceeding them throttles or kills your own processes)
- CPU: {cpu}. RAM: {mem_high} (slowed past it), hard cap {mem_max}. Disk: {disk} for your home. {tasks} processes.
- {ports_line}

## No sudo, ever
- Never run `sudo`, `apt`, `pacman`, and **never ask the user or owner for admin/root**. Install into your home:
  `npm i -g X`, `uv tool install X` / `uv venv`, `pipx install X`, `cargo install X`, `go install X@latest`.
- System packages: `box request install PKG --why "<why>"` (most install automatically within a minute;
  `box help requests`).
- Preinstalled: {tools}. Don't install your own copies of the coding agents or run their self-updaters.
{extras}

## Files (the janitor deletes things)
Work in `~/projects/<name>/` (archived after {project_ttl} days untouched; `box restore NAME` within {archive_days}
days), servers' code in `~/services/<name>/`, must-keep files in `~/keep/`. Nothing is backed up: offer to push work
to GitHub. `box help files` for the rest.

## Running things
- Run `box status` at session start and before heavy work (usage, box load, **projects about to be archived**, reboots).
- Heavy builds/tests: `box heavy -- <cmd>`, one at a time, {jobs} parallel jobs max (`box help heavy`).
- Anything long-running: `box serve add <name> -- <cmd>` (survives logout and reboots; `box help servers`).
  Never leave servers in `nohup`/`tmux`/`&`: the box reboots at {reboot_time} (`box help reboots`).
- Something the friends run *together* (group bot, shared tool, one copy instead of three): a group server,
  `box shared create NAME` (`box help shared` first; its secrets are readable by everyone).
- Never put secrets in command-line arguments, never scan/stress/mine, never touch other users' files.
- Shared agent skills: suggest one from `box skills` only when it clearly helps, and install only after the user says yes.

## Report what slows you down (without being asked)
When something about **this machine** costs you time or fails (missing tool, permission denied, throttled, bad rule),
log one line and keep going: `box issue --kind <slow|broken|missing|limit|confusing|other> "<what> | <workaround> | <cost>"`.
Not for bugs in the user's code. No secrets.

{notices}
