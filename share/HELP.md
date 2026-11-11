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
- `~/projects/<name>/`: your work. A project **untouched for {project_ttl} days (no file read or written) is
  deleted**, with {warn} days of warning in `box status`. Never deleted while a process runs from it or a
  `box serve` service points at it (even a stopped one).
- `~/services/<name>/`: code for long-running servers. Never auto-deleted; counts toward the disk budget.
- `~/keep/`: small files that must survive. Never auto-deleted.
- Caches (`~/.cache`, `~/.npm`, `~/.gradle/caches`, `~/.cargo/registry`, …) unused {cache_ttl} days are deleted.
  Build outputs (`node_modules`, `build/`, `target/`, `.next`, `.venv`, …) in projects idle {artifact_ttl} days are
  deleted. Over the disk budget, everything regenerable is wiped right away.
- `/tmp` files older than {tmp_ttl} days are deleted.
- Paths from the user's computer (`~/Downloads`, `C:\...`) don't exist here and the clipboard isn't shared. To hand
  over a file: push it to git, serve it on a port, or give them `scp {user}@{host}:<absolute path> .`
- Nothing is backed up. Push to git often.

@@ heavy
- Prefix heavy commands with `box heavy --` (e.g. `box heavy -- ./gradlew assembleDebug`). It queues behind other
  users' heavy jobs ({slots} at a time) and runs at low priority.
- Parallelism **{jobs} jobs**: `make -j{jobs}`, `cargo build -j {jobs}`, Gradle `--max-workers={jobs}` (preset).
- One heavy build at a time. Stop dev servers, watchers, emulators and Gradle daemons (`./gradlew --stop`) after.
- If the box is `busy` in `box status`, avoid heavy work unless the user insists.
- No Docker. Install tools into your home (`~/.local`, nvm, uv, rustup, …).

@@ reboots
{updates}
**reboots at about {reboot_time} (machine local time)**. {reboot_days} `box status` warns ahead of every reboot.
- Comes back: `box serve` services, group servers, the user's T3 Code, everything on disk.
- Lost: processes in `tmux`/`screen`/`nohup`/`&`, terminals, running builds/tests, `box heavy` jobs, memory.
- Make servers safe to restart any time: keep state in files or a database.

@@ servers
- `box serve add <name> [--port P] -- <command…>`, run inside the project dir. Survives logout and reboots,
  restarts on crash. The port is in `$PORT` (your range: {ports}). Listen on `0.0.0.0`.
- Manage: `box serve ls`, `box serve logs <name>`, `box serve restart|stop|start|rm <name>`.
- The user also sees and controls these on their page {portal}.
- While the service exists its project is protected from the janitor; `box serve rm` removes that protection.
- Others on the tailnet reach it at `http://{host}:<port>`.{serve_public}

@@ shared
Group servers belong to everyone: one shared copy of a bot/tool instead of each friend running their own.
- `box shared ls` lists them (check first; don't make a duplicate). `box shared create NAME` makes
  `{shared_dir}/NAME/` and picks a port in {shared_ports} (in `$PORT`).
- Put the code there, then `box shared set NAME -- COMMAND...` runs it. `./start.sh` = a file in that folder
  (`chmod +x` it); other commands must be installed system-wide, not in a home. Python: make the venv inside the
  folder and use `./.venv/bin/python app.py`.
- `box shared start|stop|restart|logs NAME`; `box shared rm NAME` stops it and moves the code to `{shared_dir}/.trash`.
  Everyone also gets Start/Stop/Restart/Logs buttons on their page {portal}.
- Every friend can edit, start, stop and remove every group server; `box shared ls` records who changed what. Only
  create one when the user wants it shared, and don't stop or change other people's unless the user asked.
- It runs as a separate account that writes only its own folder, with `HOME` = that folder and no access to anyone's
  home. **Secrets (`.env`, tokens) there are readable by everyone on the box.** Tell the user before adding a key.
- "Permission denied" writing in `{shared_dir}/NAME` right after the group was set up: this login session started
  before the account joined the group. `box shared` commands still work; for file edits use
  `sg sbshared -c '<command>'`, or it fixes itself after the next login/reboot.
- After copying files in: `chmod -R g+w {shared_dir}/NAME`. Git "dubious ownership":
  `git config --global --add safe.directory {shared_dir}/NAME`.
- Never cleaned by the janitor; doesn't count toward your disk budget.{shared_public}

@@ browsers
Headless browsers work without root (system libraries are installed). Never ask for sudo/admin for one.
- Playwright: `pip install playwright && playwright install chromium` in the project venv (or
  `npx playwright install chromium`). Skip `install-deps`. Selenium 4.6+: Selenium Manager downloads Chrome itself.
  A real Chrome: `npx -y @puppeteer/browsers install chrome@stable --path ~/.local/share/chrome`.
- Always launch with `--no-sandbox` (this kernel blocks Chrome's sandbox for normal users) and headless
  (`--headless=new` for Selenium); add `--disable-dev-shm-usage`. `xvfb-run` if something insists on a display.
- Reuse one browser and close pages; leaked Chrome processes eat your RAM limit.
- If Chrome still won't start, `ldd <chrome> | grep "not found"` and `box request` those exact packages.

@@ skills
The box has a catalog of agent skills: `box skills` lists, `box skills show NAME` prints one. None are active until
installed. Suggest one only when it clearly helps, ask the user, and only after they agree run `box skills add NAME`
(live in the next session). Never install one unasked.

@@ updates
The coding agents (`claude`, `codex`, `opencode`, `t3`) are installed and updated by the owner. `box updates` shows
whether this user's agents update nightly (`auto`) or stay put (`manual`; `box updates now` moves up). Change it
only when the user asks. Serious security fixes are applied to everyone.
