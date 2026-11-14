# Shared machine rules ({host})

You run on **{host}**, a remote Linux box shared by several people and their AI agents, as user `{user}`.
Details for any topic below: `box help <topic>` (`box help` lists topics). Read one before doing that kind of work.

## The user is not at this machine
- They reach you from their own device (T3 Code / SSH over Tailscale; they can also connect their own laptop's
  agent: `box help connect`). `localhost` means {host}: never give them a `localhost` URL. Give
  `http://{ip}:<port>` (the name {host} doesn't resolve for everyone), and make servers listen on `0.0.0.0`.
- If the human wants the T3 app on their laptop: `box t3 app-link --prompt` prints the steps for an agent on that laptop (it checks Tailscale first); the dashboard's "Make my setup prompt" gives the same.
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
- Run `box status` before heavy work: usage, box load, projects about to be archived, reboots (it also shows when a session starts).
- Docker works without sudo (rootless, your own daemon): `box help docker` before using it. Idle Docker databases are
  {docker_nap} (data kept); if one you need is down, start it again (`supabase start`, `docker compose up -d`).
- **Never turn things off on your own** (databases, servers, containers, T3, watchers) because a task looks finished
  or to save resources: the user may reply any minute and expect it all still there. The box stops idle things
  itself. Stop something only when the user asks, or it's broken or in the way of the current task.
- **Keep what you build lean**: the box is shared, and roomy today doesn't mean roomy tomorrow. In anything you set up
  or work on, find what uses memory or CPU without earning it and fix it in config (e.g. Supabase realtime left on but
  unused: hundreds of MB), telling the user in a line. `box help lean` first.
- Heavy builds/tests: `box heavy -- <cmd>`, one at a time, {jobs} parallel jobs max (`box help heavy`).
- Never block the chat on a test or build longer than a minute: start it in the background (your agent's
  background-task feature), tell the user in a line what's running, keep working or answering, report when it ends.
- Anything long-running: `box serve add <name> -- <cmd>` (survives logout and reboots; `box help servers`).
  Never leave servers in `nohup`/`tmux`/`&`: the box reboots at {reboot_time} (`box help reboots`).
- Something the friends run *together* (group bot, shared tool, one copy instead of three): a group server,
  `box shared create NAME` (`box help shared` first; its secrets are readable by everyone).
- Never put secrets in command-line arguments, never scan/stress/mine, never touch other users' files.
- Shared agent skills: suggest one from `box skills` only when it clearly helps, and install only after the user says yes.

## Report what slows you down (without being asked)
When something about **this machine** blocks you or costs you more than ~5 minutes (missing tool, permission denied,
throttled, bad rule), log one line, once per problem, and keep going: `box issue --kind <slow|broken|missing|limit|confusing|other> "<what> | <workaround> | <cost>"`.
Not for bugs in the user's code. No secrets.

{notices}
