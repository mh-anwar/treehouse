# Machine rules ({host})

You run on **{host}**, a remote Linux box, as user `{user}`. Topic details: `box help <topic>` (`box help` lists them).

## The user is not at this machine
- They reach you from their own device. `localhost` means {host}: give them `http://{ip}:<port>` URLs, never
  `localhost`, and make servers listen on `0.0.0.0`. No display: show a URL, a screenshot file or test output.
- **Do setup yourself; never send the user to a terminal.** All they should do is click a link or type a code.
  Logins (GitHub, etc.) use device codes: `box help logins`. Their page **{portal}** does Claude/Codex/GitHub logins.
- Talk plainly and briefly. Never ask for passwords or tokens in chat.

## Resources
- RAM {mem_high} (hard cap {mem_max}), disk {disk}, {tasks} processes, no CPU cap. Need more RAM for a while?
  `box boost` (up to {boost_mem} for {boost_hours} h, if the box has it free); tell the user in a line.
- {ports_line}

## No sudo, ever
- Never run `sudo`/`apt`/`pacman` or ask anyone for root. Install into your home: `npm i -g X`, `uv tool install X`,
  `pipx install X`, `cargo install X`, `go install X@latest`. System packages: `box request install PKG --why "<why>"`.
- Preinstalled: {tools}. Don't install your own copies of the coding agents or run their self-updaters.
{extras}

## Files
- Each project is a git repo directly in your home (`~/<name>`). One untouched for {project_ttl} days is archived
  (`box restore NAME` within {archive_days} days), but only when everything in it is pushed to a remote. Push often:
  nothing is backed up. `~/keep/` is never touched; servers' code goes in `~/services/<name>/`. `box help files`.

## Running things
- Long-running servers: `box serve add <name> -- <cmd>` (survives logout and the {reboot_time} reboots), never
  `nohup`/`tmux`/`&`. `box help servers`. Docker works rootless: `box help docker`.
- Don't stop the user's servers, databases or containers on your own; they may come back any minute.
- Never put secrets in command-line arguments, never scan/stress/mine, never touch other users' files.

## Report what slows you down
When something about **this machine** blocks you or costs you more than ~5 minutes, log one line once and keep going:
`box issue --kind <slow|broken|missing|limit|confusing|other> "<what> | <workaround> | <cost>"`. Not for bugs in the user's code. No secrets.

{notices}
