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
3. Owner: works now with the `ssh minastirith` alias (radagast). Friends: federation ("Accounts across boxes") gives them
   the account and keys on the other box. Until then `box run` runs here for them.
4. Agents learn it from `box help boxes` (a short paragraph is added to share/HELP.md).

## Accounts across boxes

**Decision: one source of truth (the primary) publishes a signed roster; every other box mirrors it with the
same UID; per-user box-to-box SSH keys; T3 linked server-to-server; logins done on each box, not copied.**
Built in `lib/sbfed.py`, `sharedbox-admin fed-*`, `box _fed-user`, `box boxes setup|login`.

### Options compared

| Option | Verdict |
|---|---|
| (a) Mirror accounts (same name/UID/groups/limits) pushed by root over root SSH | Works, but needs root-to-root SSH trust (a root shell on the other box if a key leaks) and touches sshd/root's authorized_keys. |
| (b) Shared/network home (NFS or sshfs over Tailscale/Wi-Fi) | No. minastirith is on Wi-Fi: every `git status`, `node_modules` read and SQLite lock goes over the air; one box down freezes the other's agents; file locking and inotify (dev servers, T3) misbehave. The opposite of "capable". |
| (c) On-demand (create on B the first time it's needed) | Slow first use (the T3 download, an empty home) right when the user wants it; still needs an identity source, so it's (d) with a worse trigger. |
| **(d) Single identity source: the primary publishes a signed roster, secondaries pull** | **Chosen.** Pull over the existing tailnet web port, no root SSH, no new daemon (a 5-minute timer). The signature makes it safe even though anyone on a peer box can open a socket from that box's IP. Each box keeps its own home on its own disk: full local speed. |

Mirroring (a)'s *result* (same UID, groups, limits, port block) with (d)'s *transport* gets the best of both.

### How it works

- `BOX_ROLE=primary|secondary` in sharedbox.conf (default primary). Invites and `useradd` happen only on the
  primary; a secondary refuses `box invite`/signups with a pointer to the primary.
- Every box has a root-only signing key `/etc/sharedbox/fed/box_ed25519` (`sharedbox-admin fed-init`, run by
  install.sh) and an `allowed_signers` file of the boxes it trusts (`fed-key` on one box prints a line,
  `fed-trust '<line>'` on the other). This is the only manual step, once per pair of boxes, done by the owner's
  agent with sudo.
- `sharedbox-fed.timer` (every 5 min, oneshot `sharedbox-admin fed-sync`) on every box:
  1. Builds this box's roster: each friend's name, uid, status, limits, home box, their per-user box-to-box
     public key (`~/.ssh/id_treehouse`, made as the user on first use) and, on the home box only, the keys
     the user added themselves (their laptop). Plus this box's SSH host key. Signed with `ssh-keygen -Y sign`
     (namespace `treehouse-roster`), written to `/var/lib/sharedbox/fed/roster.json`, served by sharedbox-web
     at `GET /api/peer/roster` to BOXES IPs only (same check as `/api/box`).
  2. Pulls the other boxes' rosters, verifies the signature *for that box's name*, refuses older serials
     (replays) and future timestamps. The last good roster per box is kept in root's state file.
  3. Secondary only, from a fresh primary roster (< 2 days old, so a long outage never suspends anyone):
     creates missing accounts with the **same uid = gid**, groups, shell, 0700 home, limits drop-ins and port
     block (port_range is derived from the uid, so ports match too); resumes and suspends to match; copies
     per-user `limits`. **Never deletes**: removed on the primary = suspended here, home kept (like the
     primary's own suspend). Conflicts (name or uid taken by a non-treehouse account, uid outside the friends'
     range, an account here that isn't a copy) are logged once and left alone.
  4. Every box, as each user: a managed block in `~/.ssh/authorized_keys` with that user's keys from the
     other boxes (same uid only, no key options allowed), `Host <box>` blocks in `~/.ssh/config`
     (`User <same name>`, `IdentityFile ~/.ssh/id_treehouse`) and pinned host keys from the signed rosters in
     `~/.ssh/treehouse_known_hosts` (no TOFU prompt). So `ssh minastirith` works as `dave` from sietchtabr and
     back, and `box run --anywhere` works for friends unchanged (sbmulti's `ssh_dest` already prefers a Host
     whose HostName is the box's IP). Written only when the content changes.
  5. Every box, as each user, every 12 h or when keys changed (`box _fed-user link`; `box boxes setup` runs it
     on demand): links the other box's T3 into this T3 (`t3 environment link http://<ip>:<their port>
     --pairing-code <token from box _portal t3-pair there> --access full-access`), renewing a week before T3's
     30-day expiry. Both boxes do this, so the link goes both ways. From the home box only: copies the GitHub
     token (`gh auth token | ssh B gh auth login --with-token`) and the git identity if B has none.
- Friends reach B **through their usual T3**: a linked environment is server-to-server (A's T3 talks to B's T3
  over the tailnet), so it works from the public Cloudflare site with no Tailscale on the friend's laptop.
  The gate's `backend_for()` stays as it is (each friend's main T3 remains on the primary).

### Credentials: why logins are not copied

- **GitHub** (`gh`): an OAuth token that doesn't rotate and is meant to be used from many machines. Copied.
- **Claude** (`~/.claude/.credentials.json`) and **Codex** (`~/.codex/auth.json`): short-lived access tokens
  plus *rotating* refresh tokens. Two boxes refreshing the same token family race: the first refresh
  invalidates the other's copy (Codex reports "refresh token already used" and logs out; Claude drops to a
  login prompt), so a copy works for hours and then breaks one or both boxes, mid-task, unpredictably. A copy
  also doubles where a long-lived secret lives for no gain.
- Instead: `box boxes setup` lists what's missing on the other box, and `box boxes login claude --on
  minastirith` runs the same device login the dashboard uses (`box _portal login-start` over the user's SSH)
  and prints the link (and for Claude takes the code back with `--code`). The human clicks a link, as on
  day one; the agent does the rest. Each box then refreshes its own tokens independently.

### Same rules on both boxes

Limits, janitor, tick, agent rules, `box` CLI: identical code and the same sharedbox.conf (except BOX_ROLE and
public-site settings). Limits are copied per user. Projects are not shared: git is the source of truth; `box
run` mirrors are caches.

### Deploy

1. Deploy this branch to both boxes as usual (install.sh installs `sbfed.py`, `sharedbox-fed.timer`, runs
   `fed-init`).
2. On minastirith, as root: set `BOX_ROLE=secondary` in /etc/sharedbox/sharedbox.conf.
3. Key exchange, as root: on sietchtabr `sharedbox-admin fed-key` -> on minastirith
   `sharedbox-admin fed-trust '<that line>'`; and the reverse (`fed-key` on minastirith, `fed-trust` on
   sietchtabr). Both need BOXES listing both boxes (already true).
4. `sharedbox-admin fed-sync --force` on sietchtabr, then minastirith, then sietchtabr again (or wait 10
   minutes). `sharedbox-admin fed-status` shows what each side did.

### Risks and limits

- Root on any trusted box can add keys to any friend on the others (rosters contribute keys). All boxes are
  the owner's; `allowed_signers` is the trust list.
- T3's linked-environment polling may wake a friend's socket-activated T3 on the other box now and then; watch
  memory on minastirith after the first day.
- `t3 environment link` is a T3 CLI surface that may change; a failed link is logged (`fed-status`) and
  retried, nothing else depends on it.
- The owner's own accounts (pippin here, radagast there) are not mirrored: the owner keeps the `ssh
  minastirith` alias.
- A friend's files on B are separate: an agent that edits on B must push to git (said in `box help boxes`).

## Next

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
- (Built: federation links each user's T3s on the two boxes, see "Accounts across boxes".)
- Don't migrate live threads: the session state and dev servers stay where they started.

**Later**: `box run --anywhere` for Docker builds (send build context through the mirror and use the far
side's layer cache). Teach `box heavy` to suggest `box run --anywhere` when it would queue for more than
a minute and another box has room.

### Key exchange is automatic (2026-11-15)
Each box serves its public roster-signing key at `/api/peer/key` to the boxes in its BOXES (root config) only.
On its first `fed-sync`, a box pins the key of every BOXES peer it doesn't trust yet (trust on first contact
over Tailscale, which authenticates which machine owns an IP). Pinned keys are never replaced automatically; a
changed key is logged in `fed-status` and needs `sharedbox-admin fed-trust '<line>'` by hand. The manual
fed-key / fed-trust steps above are only needed for that case. Still set `BOX_ROLE=secondary` on secondaries.
