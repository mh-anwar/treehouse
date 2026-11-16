# Multi-box treehouse

Two boxes on one tailnet today: sietchtabr (i7 Haswell, 8 threads, 30G) and minastirith (i3-4130, 4 threads,
16G, Wi-Fi; later an i7-4790 + 32G). `BOXES=` in sharedbox.conf lists both, each box's sharedbox-web answers
`GET /api/box` to the other, and the owner's dashboard shows a Boxes card. The gate has a `backend_for(user)`
hook for putting a user's T3 on another box. This note covers what to build on top of that.

## Efficiency or leave it to T3?

Both, split by what each one can see.

**Where an agent's time goes.** A coding agent's own process is light: about 100 MB each on this box right now,
and mostly idle while it waits on the model API. What uses the box is the commands it runs: test suites, builds,
Playwright, type checks, benchmarks. Those are also what the agent sits and waits on. So:

- Moving a whole agent (a T3 thread) to another box saves almost nothing: it was barely using this one.
- Moving its heavy commands saves real time when this box is busy, and **splitting one command into
  shards across boxes** cuts the wait for every agent, even when the box is calm. Nobody does this for agents
  yet. CI systems shard tests, but not from inside an agent's edit-test loop, on boxes friends already share.

**What T3 can do.** Linked environments (`t3 environment link`, `linkedEnvironmentsEnabled: true` here) let one
T3 drive threads on another T3 server, and it shows them in one UI. That covers placement at the level of
threads, done by hand: a person or agent chooses "run this thread over there". T3 doesn't know box load, the
box's limits, the heavy-job queue or which friend owns which account. It can't split a command, and it doesn't
sync a working tree. It just runs a whole agent on the other side, where the code has to already be.

**Answer.** Leave threads, UI and "which machine is this agent on" to T3. Its linked environments are
good, and copying them would be wasted work. treehouse adds what only it knows: **load-aware, command-level
offload with an incremental sync**, plus a load signal that T3 or an agent can read (`box boxes --json`).
That's the efficiency win, and it's what's built.

A caveat about today's hardware: minastirith is about 0.45x sietchtabr per core, and on Wi-Fi. A single job sent
there finishes *slower* than here unless sietchtabr is contended, so `pick()` only leaves this box when another
one has clearly more room (1.5x, with at least 1 core-equivalent more). Sharding is where the second box pays
off now: it adds about 45% capacity. After the i7-4790 upgrade it's about 1:1, so sharded suites finish in
roughly half the time. Set `BOX_SPEED="minastirith=0.45"` in sharedbox.conf now, and drop it after the upgrade.

## What's built (lib/sbmulti.py, hooked into lib/box)

`box boxes [--json]`: one line per box: cpus, load, level, mem, disk, "room" (free cores x speed, cut when
the box is busy, mem >= 90% or disk < 5%), and whether *you* can SSH there. Load comes from the existing
`/api/box`, so it's one small HTTP call per box, in parallel, with a 3 s timeout. The JSON includes `best`.

`box run [--anywhere | --on NAME] [--shards N] [--prep CMD] [--back PATH]... [--git] -- CMD...`
- Picks the box with the most room and stays here unless another box clearly wins.
- Sync: rsync into a persistent mirror at `~/.sharedbox/mirror/<name>-<hash>` on the other box. In a git repo
  the project's `.gitignore` files are the filter (`:- .gitignore`, nested ones count). That means only source
  changes travel, deletions propagate, and the other box's own `node_modules`/`.venv`/build outputs are
  excluded, so they're never deleted or re-sent. `.git` is skipped unless you pass `--git`. Non-git folders
  skip the usual dependency and build directories. It uses `-z` for Wi-Fi. Only the first sync is a full copy.
- `--prep "npm ci"` runs on the other box only when the lockfiles (package-lock, pnpm, yarn, uv, poetry,
  requirements, Cargo, go.sum...) or the prep command change. The stamp is kept next to the mirror.
- The command runs as the same user, through that box's `box heavy` (so it queues behind its own heavy work),
  with output streamed back and the exit code returned. It runs under `setsid`, and a watcher kills its process
  group when our SSH stdin closes, so a Ctrl-C or dropped link doesn't leave tests running over there.
- `--shards N` splits the run in proportion to each box's room (this box included). Each shard gets
  `BOX_SHARD`/`BOX_SHARDS` (for example `pytest -p xdist`-style splitting, or
  `playwright test --shard=$((BOX_SHARD+1))/$BOX_SHARDS`). Lines come back prefixed `[box#i]`. The exit code is
  the first failing shard's.
- `--back coverage/ --back test-results/` copies results back afterwards.
- **Failure handling**: no BOXES, the other box down, no SSH login, or a failed sync all mean it runs here
  through `box heavy`, with one stderr line saying why. If a sync fails while sharding, that box's shards
  run here. `SHAREDBOX_REMOTE=1` is set on the far side, so a nested `box run` never bounces back.
  Exit 97 or 98 from the far side means the folder or `--prep` failed there.

SSH destination order: `~/.sharedbox/boxes.json` `{"name": "user@host"}`, then a `~/.ssh/config` Host whose
HostName is the box's IP (the owner today: `minastirith` reaches radagast), then `<same user>@<tailscale ip>`.

Tests: `tests/test_multi.py` covers scoring, the pick margin, the shard plan, identity, rsync argv, the prep
key, and the remote script, run with local `sh`, including its exit code and kill-on-stdin-close.

## What's needed at deploy

1. `install.sh` copies `lib/sbmulti.py` (one line, added). Deploy to both boxes as usual.
2. Optional: `BOX_SPEED=minastirith=0.45` in /etc/sharedbox/sharedbox.conf on both boxes.
3. Owner: works now with the `ssh minastirith` alias (radagast). Friends: they need the account, the same
   username and a key on the other box (next section). Until then `box run` runs here for them.
4. Agents learn it from `box help boxes` (a short paragraph is added to share/HELP.md).

## Next

**Identity across boxes (root provisioner, no human steps).** Pull model, with no new write endpoint:
- `box _init-home` makes `~/.ssh/id_treehouse` (ed25519, no passphrase) for each user and adds a
  `Host <box>` block for every BOXES entry (`HostName ip`, `User <same name>`, `IdentityFile`).
- Each box's sharedbox-web serves `GET /api/peer/accounts` with the same peer check as `/api/box`. It returns
  `[{user, uid, pubkey, role}]` for accounts that are on.
- Each box's sharedbox-admin (root) polls its peers every few minutes. It creates missing accounts with the
  **same UID** (so files keep their owners across syncs), applies the same limits and port block, and writes
  the pubkey to a root-owned `/etc/ssh/treehouse_keys/%u` (an sshd drop-in adds it to `AuthorizedKeysFile`,
  so users can't lock themselves out). A friend removed or suspended on their home box disappears from the
  list, and the peer locks the account. Trust is the tailnet plus BOXES, the same as today's `/api/box`.
- `box invite` still creates the account on one box (its *home*). The others follow within minutes.

**Janitor**: prune `~/.sharedbox/mirror/*` untouched for 7 days (core agent's area). Mirrors are caches:
the source of truth is always the home box or git.

**Team board across boxes** (interface only, for `box team`). The board (`board.jsonl` plus the inotify wait)
lives on one box, the team's home, recorded as `home=<box>` in the team's metadata. On any other box, `box team`
does `os.execvp("ssh", [dest, "box", "team", *argv])` with the same `ssh_dest()`. A post is one round trip, and
`box team wait` is one long-lived SSH session blocked on inotify there, which costs nothing while idle. SSH
already carries the user's identity, so the board needs no new auth or HTTP API. sbteam needs about 10 lines:
`if team_home != this_box: forward`.

**Placement of new agents.** Keep it in T3:
- Give every user's T3 a home box chosen at invite time by memory headroom (`box boxes --json`). Set it via the
  gate's existing `backend_for()` hook (`rec["box"]`), so the public URL stays the same.
- At provisioning, auto-link each user's T3s on the two boxes (`t3 environment link`), so they can start a
  thread "over there" from one UI when they want.
- Don't migrate live threads: the session state and dev servers stay where they started.

**Later**: `box run --anywhere` for Docker builds (send build context through the mirror and use the far
side's layer cache). Teach `box heavy` to suggest `box run --anywhere` when it would queue for more than
a minute and another box has room.
