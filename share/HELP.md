@@ logins
Browser logins can't open a browser here, so use device codes and relay only the code. GitHub:
1. Start it in the background (it waits for approval):
   `nohup gh auth login --hostname github.com --git-protocol https --web </dev/null > ~/.gh-login.txt 2>&1 &`
2. After a few seconds read `~/.gh-login.txt`; give the user only the one-time code (like `ABCD-1234`) and
   https://github.com/login/device. Ignore the "clipboard" warning.
3. When they say done: `gh auth status`, then `gh auth setup-git`. Code expired (~15 min)? Start again.
4. If `git config --global user.name`/`user.email` are empty, fill them from `gh api user`
   (`<id>+<login>@users.noreply.github.com` as the email).
Claude / Codex logins: send the user to their page {portal}, which does them in the browser.
Never ask for passwords or tokens in chat.

@@ files
- Projects are git repos directly in your home: `~/<name>/`. A project is **archived only when both hold**: untouched
  for {project_ttl} days (no file read or written, no process running from it, no `box serve` service pointing at it),
  and fully backed up: it has a remote, nothing uncommitted or untracked, every local branch pushed, no stash.
  Folders that aren't git repos are never archived. `box status` warns {warn} days ahead.
- Archived = moved to `~/.sharedbox/archive/<name>-<date>/`, **restorable for {archive_days} days: `box restore NAME`**
  (`box restore` lists them), then deleted (it's still on the remote). If the user asks where a project went, check
  `box restore` first.
- `~/services/<name>/`: code for long-running servers. `~/keep/`: must-keep files. Never auto-deleted.
- Caches (`~/.cache`, `~/.npm`, `~/.gradle/caches`, …) unused for {cache_ttl} days are deleted. Over the disk budget,
  caches and build outputs (`node_modules`, `target/`, `.venv`, …) go right away, then archives, then the oldest
  idle fully-pushed projects. Unpushed work is never deleted.
- `/tmp` files older than {tmp_ttl} days are deleted.
- Paths from the user's computer (`~/Downloads`, `C:\...`) don't exist here and the clipboard isn't shared. To hand
  over a file: push it to git, serve it on a port, or give them `scp {user}@{ip}:<absolute path> .`

@@ boost
- `box boost [--why "<why>"]` raises your RAM limit to {boost_mem} for {boost_hours} hours, if the box has that much
  free right now (otherwise it says so; try later or free memory). It ends by itself. The user can also press Boost
  on their page {portal}. Use it when a real job needs it (a big build, a model, many test browsers), not by default.

@@ git
Faster clones and CI. Measured on this box (ruff, a medium repo): full clone 17 s / 245 MB, blobless 9 s / 68 MB,
shallow 5 s / 18 MB.
- Clone with `git clone --filter=blob:none <url>` (blobless): full history for `git log`/`blame`/rebase/push,
  file contents fetched only when checked out. Half the time, a quarter of the disk.
- `--depth=1` (shallow) only for a throwaway read or a one-off build: it breaks `log`/`blame`, merges and rebases,
  and later fetches can be slow. Never on a repo the user will work in.
- Several branches at once: `git worktree add ../<name> <branch>` (instant, shares objects) instead of a second clone.
- Waiting on GitHub Actions: never block the chat on it. Use your host's PR/CI watch tool if it has one, otherwise
  `gh run watch <id> --exit-status` as a background task.
- Writing or fixing a workflow, include what speeds every later run:
  - `concurrency: {{group: ${{{{ github.workflow }}}}-${{{{ github.ref }}}}, cancel-in-progress: true}}` (an agent's
    rapid pushes cancel the stale runs instead of queueing them);
  - `actions/checkout` keeps its default `fetch-depth: 1`; set `0` only for a step that needs history (changelogs, versioning);
  - independent jobs run in parallel; `needs: [build]` only where a job really uses another's output, passed
    with `actions/upload-artifact`/`download-artifact` instead of rebuilding it;
  - dependency caches (`actions/setup-node` `cache: pnpm`, `astral-sh/setup-uv` `enable-cache: true`, `Swatinem/rust-cache`);
  - `paths:`/`paths-ignore:` so a docs-only change doesn't run the test suite.

@@ reboots
{updates}
**reboots at about {reboot_time} (machine local time)**. {reboot_days} `box status` warns ahead of every reboot.
- Never start a long job (big build, long test run) close to a reboot `box status` announces.
- Comes back: `box serve` services, the user's T3 Code, everything on disk.
- Lost: processes in `tmux`/`screen`/`nohup`/`&`, terminals, running builds/tests, memory, a running `box boost`.
- Make servers safe to restart any time: keep state in files or a database.

@@ servers
- `box serve add <name> [--port P] -- <command…>`, run inside the project dir. Survives logout and reboots,
  restarts on crash. The port is in `$PORT` (your range: {ports}). Listen on `0.0.0.0`.
- Manage: `box serve ls`, `box serve logs <name>`, `box serve restart|stop|start|rm <name>`.
- The user also sees and controls these on their page {portal}.
- Their T3 Code isn't one of these (it starts when they open it). Ports {t3_ports} are its.
- While the service exists its project is protected from the janitor; `box serve rm` removes that protection.
- Others on the tailnet reach it at `http://{ip}:<port>`.{serve_public}

@@ shared
For a group bot or tool, use a `box serve` service. See `box help servers`.

@@ browsers
Headless browsers work without root (system libraries are installed). Never ask for sudo/admin for one.
- Playwright: `pip install playwright && playwright install chromium` in the project venv (or
  `npx playwright install chromium`). Skip `install-deps`. Selenium 4.6+: Selenium Manager downloads Chrome itself.
  A real Chrome: `npx -y @puppeteer/browsers install chrome@stable --path ~/.local/share/chrome`.
- Always launch with `--no-sandbox` (this kernel blocks Chrome's sandbox for normal users) and headless
  (`--headless=new` for Selenium); add `--disable-dev-shm-usage`. `xvfb-run` if something insists on a display.
- Reuse one browser and close pages; leaked Chrome processes eat your RAM limit.
- If Chrome still won't start, `ldd <chrome> | grep "not found"` and `box request install` those exact packages.

@@ skills
The box has a catalog of agent skills: `box skills` lists, `box skills show NAME` prints one. None are active until
installed. Suggest one only when it clearly helps, ask the user, and only after they agree run `box skills add NAME`
(live in the next session). Never install one unasked.

@@ updates
Coding agents (`claude`, `codex`, `opencode`, `t3`) update automatically every night. `box updates` shows versions.

@@ t3
- T3 Code uses stable by default. The owner can opt in to nightly with `t3@nightly` in TOOLS_NPM.
  Nothing stops an idle T3. Changing channels never restarts a running T3; returning to stable may require
  restoring a compatible database from `~/.t3/sb-backup/` (the tools refuse automatic downgrades).
- The owner's T3 stays with T3's own service until they run `box t3 switch`. Installing or updating the box
  never switches it. After that one-time, idle-gated switch it is `sb-t3.service`, run by the box. Don't run `t3 up`, `t3 update`, `t3 install`,
  `t3 service ...` or `t3 doctor --fix` there: they reinstall T3's own `t3code.service`, which is kept off on purpose.
  If T3 says a server was "started by hand" and to stop it and start it again, ignore that.
- The owner's one-time move from `t3code.service` to the box: `box t3 switch` (waits until no agent is working,
  restarts T3 once, puts the old one back if the new one doesn't answer). Progress: `box t3 status`.

@@ connect
The user can connect **their own computer's coding agent** (Claude Code, Codex, ... on their laptop) to this
account, with no VPN: SSH goes through Cloudflare. {connect}
- Their agent does everything itself: installs `cloudflared`, makes a key `~/.ssh/treehouse`, runs
  `cloudflared access login <site>` (the user types one emailed code in the browser), registers the key with
  `POST <site>/_box/api/ssh-key`, and adds a `Host treehouse` block to `~/.ssh/config`.
- Afterwards, on their computer: `ssh treehouse 'box status'`, `ssh -t treehouse claude`, VS Code / Cursor
  Remote-SSH to host `treehouse`, or just tell their local agent "run X on my treehouse box".
- If they ask how: point them to that page and its Copy button. Don't walk them through it in a terminal, and
  don't add keys for them by hand.
- The T3 Code desktop app on their laptop needs Tailscale there (the app can't pass Cloudflare). Their dashboard's
  "Make my setup prompt" (or `box t3 app-link --prompt`) gives their laptop's agent the steps: check Tailscale and
  the account first, fix only what's broken, then open a one-time pairing link. The box's address is always its
  Tailscale IP. No Tailscale: T3 in the browser (dashboard, Open T3) always works. T3 Connect isn't tested here yet.

@@ requests
- A system package (apt/pacman) you can't install into your home: `box request install PKG [PKG...] --why "<why>"`.
  Most are installed automatically within a minute (the command waits and says so). Use the distro's package
  name; check it first (`apt-cache policy PKG` / `pacman -Si PKG`).
- Servers and daemons install too, but their system service stays off: run the program as yourself (in your
  home, on your own port, `box serve add` to keep it up). Packages the rules do not decide are emailed
  to the owner for approval.
- Anything else (a config change, more disk, a service): `box request "<plain words>"`. The owner sees it.
- `box request status` lists yours with their state. When the owner answers, `box status` shows it.

@@ friends
Only on the owner's account (ADMIN_USERS). No sudo: root does it. Add `--json` to any of these.
- `box friends`: every friend (username, email, status, joined, disk, last active), open requests.
  Tailnet self-join is immediate when JOIN_MODE=open; there is no signup approval queue.
- `box friends remove NAME [--archive]` deletes the account and its files (--archive keeps a tarball first);
  `box friends suspend NAME` / `resume NAME` locks them out / lets them back (files kept). Confirm with the
  owner before remove. The owner's own accounts can't be changed here.
- `box friends email NAME EMAIL`: link an email to an account and let it through Cloudflare Access.
  `box friends access`: the exact Cloudflare allow list and seats in use.
- `box friends requests [--all]`: friends' `box request`s that need the owner. `box friends requests approve ID`
  installs a package request (even a REQUEST_DENY one); `done ID` once it's handled (e.g. a setting the owner
  changed for them); `decline ID --why "..."`. Free-text requests are emailed to the owner too.
  The friend's agent is told either way.
- `box limits disk N`: the owner's own disk budget (whole GB). Not enforced on owner accounts, only what their
  agents are told to stay within; friends' limits stay root's (`sharedbox-admin limits`).
- `box friends issues [--days N]`: what everyone's agents logged with `box issue`, most widespread first.
- `box friends boxes "NAME=IP NAME=IP"`: the owner's treehouse boxes (Tailscale IPs, this box included), shown
  in the Boxes card on the owner's dashboard. Run the same line on every box; `box friends boxes` shows it.

@@ lean
Memory and CPU are shared and fill up fast once several people are busy, so waste counts even on a quiet day.
Whatever you set up or work on (databases, dev servers, containers, bots, build tools, test runners), look for what
uses resources without earning them, and fix it. Tell the user in one line what you changed.
- Only what nothing uses: check the code and config before turning a feature or service off. Anything a person might
  use directly (an admin panel, a database UI, a dashboard, a preview server) is theirs to decide: suggest, don't switch.
- Change config, not running things: apply it at the next natural restart. Never stop or restart something the user
  may be using just to save resources.
- Measure, don't guess: `box status` (your usage), `ps -u $USER -o rss,args --sort=-rss | head`,
  `docker stats --no-stream`. Look at the biggest first.
Typical waste:
- Services switched on in config that the code never calls. Supabase (`supabase/config.toml`) is the classic: realtime
  (hundreds of MB; the code uses it only if it calls `.channel(`), storage, edge_runtime, analytics. Same idea for
  compose files with extra services, queues, search engines or caches nothing talks to.
- A second copy: a new database or dev server when one for the project is already running (`docker ps`,
  `box serve ls`, `ss -ltnp`). Point new work at the existing one.
- Dev modes left as servers: `next dev`, watchers (`--watch`, nodemon, `tsc -w`) for something long-running; a
  production build (`next build && next start`) uses far less.
- No limits: compose `mem_limit:`, `org.gradle.jvmargs=-Xmx2g`, slim images.
- Leaks: test browsers (Playwright, Puppeteer) not closed in teardown, 300+ MB each; background jobs your own runs
  started and forgot.
- Waiting on the disk: every save that must reach the disk (database commits, `git commit`, package installs) waits
  for it, and some boxes have slow disks (measure: a few ms per sync is slow). For throwaway local test databases
  the wait isn't needed: Postgres `ALTER SYSTEM SET synchronous_commit = off;` then `SELECT pg_reload_conf();` (a
  crash loses the last second of writes, never corrupts). Only on databases the user treats as scratch; ask first
  otherwise. Scratch files for builds and tests go in /tmp when `df /tmp` says tmpfs (memory).
- Agent helpers (MCP servers) start with every agent session, one copy per session: launched as `npx -y PKG@latest`
  or `uvx PKG@latest` they ask the registry for a new version each time (~0.5-1.5 s, measured). Pin a version, and
  suggest removing ones the user doesn't use (each Python/Node helper is ~50-100 MB per open session).
- Slow shell start: agents start a shell for every command, so `time bash -lc true` should stay under ~30 ms.
  After installing anything that hooks into the shell (nvm, conda, pyenv, rbenv, prompt tools) check it; for nvm run
  `box tune shell`, for the others use their lazy or minimal init instead of the eager one their installer adds.

@@ tune
The box itself (kernel, memory, disk, network) should be so efficient nobody feels it, even when everyone is busy.
`box tune` is a read-only report: what treehouse sets on every box, and what's left for the owner. Run it when
setting a box up, after a treehouse update, and whenever the box (not one program) feels slow.
How to look further, agent-first:
1. Measure before changing anything, and again after: `box status`, `box tune`, `free -h`, `cat /proc/pressure/*`
   (memory/CPU/IO stalls), `vmstat 5`, `iostat -x 5` if installed, `ss -s`, `tailscale netcheck`, `sysctl -a`.
2. Find what costs the most: memory pressure and swapping, disk waits, CPU throttling, slow network paths, services
   nobody uses, settings left at defaults meant for a laptop or a desktop.
3. Sort what you find:
   - Helps every treehouse box (a safe kernel default, a check): it belongs in treehouse, so every owner gets it.
     It goes in the repo: a line in `etc/sysctl-*.conf` with a comment saying why, or a step in `sharedbox-tune
     apply`, plus a check in `sharedbox-tune` CHECKS. Write it up for the owner with the numbers (before/after);
     if they maintain treehouse, propose the change there.
   - Depends on this machine (a desktop service, swap size, a router setting, CPU power mode): a suggestion for the
     owner, in plain words, with what it costs and what they'd lose. Never switch it yourself.
4. Never trade safety or the owner's stuff for speed: nothing that restarts the network, reboots, drops security
   updates, or turns off something someone may use. You can't use sudo; the owner (or a treehouse update) applies it.
Places worth a look: zswap/swap/hugepages, inotify and open-file limits, disk trim and free space, I/O scheduler,
TCP congestion control and buffers, Tailscale direct vs relayed peers, the Cloudflare tunnel protocol, what's
running at boot, journald/log volume, per-account limits (`box status`), caching in front of web apps.

@@ docker
Docker runs rootless here: your own Docker daemon, under your account and your limits. No sudo, no docker group.
- First time only: `dockerd-rootless-setuptool.sh install` (sets up a user service), then
  `systemctl --user enable --now docker`. Check with `docker info` (it should say `rootless`).
- Every shell/agent session: `export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock` (put it in `~/.bashrc`).
- Published ports must be in your range ({ports}); bind them on `0.0.0.0` like any server. Ports under 1024 don't work.
- Images live in `~/.local/share/docker` and count toward your disk budget. Prune (`docker image prune`) only when
  the user asks or you're over budget. Never `docker system prune` (it deletes stopped databases' containers) and never
  anything with `--volumes` (it deletes their data).
- Never stop a database yourself because a task seems done; the user decides that.
- Containers that should outlive your session: `--restart unless-stopped` (the user service starts after reboots).
- `docker compose` works the same way.


@@ hackathon
Hackathon mode = develop freely until a set time. `box status` shows a line while it's on; `box hackathon` shows
the state. While on: package requests from the owner
and the listed friends install without the deny list, the size cap or the daily limit (packages that could break
the box itself, like sudo, the kernel, SSH or the firewall, are still refused). It ends by itself at its end time.
Owner only: `box hackathon on --until "YYYY-MM-DD HH:MM" --users name1,name2` (or `--hours N`), `box hackathon off`.

@@ boxes
The owner may have more than one box (BOXES in the box's config). `box boxes` lists them: name, Tailscale IP,
cores, load, memory, disk, "room", and whether you can log in there (`ssh NAME` then just works). A second box
is often an older, slower machine (BOX_SPEED says how much slower per core), so treat it as overflow capacity,
not a speed-up. Move a job there only when all hold:
1. This box is contended: load is above ~3/4 of its cores, or you're near your own limits.
2. The job is long (~10+ min here) and self-contained (full test suite, image build, benchmark, crawl, batch job),
   and doesn't need this box's databases, dev servers or files you're editing.
3. Little to copy: push to git and pull there, or rsync under ~1 GB; install dependencies there instead of
   copying node_modules or Docker contexts over a slow link.
4. The other box has room (`box boxes`).
Otherwise run it here. Never keep the only copy of anything there; results go back to git or this box. There you
log in as yourself: no sudo or system changes unless the owner asks.
Tools for this: `box boxes` shows each box's load and whether you can log in there. `box run -- CMD` runs a
heavy command wherever there's most room, and runs it here when no other box clearly wins or one is down. It
copies only changed source files to a kept copy over there, streams the output back and returns the exit code.
`box run --shards N -- CMD` splits a run over the boxes (each part gets BOX_SHARD and BOX_SHARDS, e.g.
`playwright test --shard=$((BOX_SHARD+1))/$BOX_SHARDS`). Add `--prep "npm ci"` to install dependencies there
(this only re-runs when lockfiles change) and `--back DIR` to copy results back.
Friends have the same account (same name, files limits and port block) on every box, made automatically:
`ssh NAME` just works (its own key, no prompt), and the T3 Code on each box is linked into the other,
so the human can start a thread "on NAME" from their usual T3. GitHub and git identity are copied over.
Claude/Codex logins are not (copying them breaks both): `box boxes setup` lists what's missing, then
`box boxes login claude --on NAME` prints a link (and for Claude asks for the code back with `--code X`).
The human only clicks the link. Files are NOT shared between boxes: push to git, or use `box run`.
