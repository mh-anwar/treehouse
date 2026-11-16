#!/usr/bin/env bash
# SCENARIO=agents. Runs as root INSIDE the throwaway container (tests/container/run.sh copies the repo to /src).
# A full install with the coding agents and toolchains (no --no-tools), then checks, as a friend in a login shell,
# what the agent rules (share/AGENTS.md) promise: every preinstalled tool runs, `npm i -g`, `uv tool install`,
# `pipx install`, `go install` and `cargo install` put programs on the friend's PATH without sudo, the agent
# launchers are the box's shims, the managed rules block is in every agent's rule file, and the friend's T3 Code
# starts and pairs. Downloads a few GB; about 10-15 minutes.
set -uo pipefail
cd /src
. /src/tests/container/lib.sh
setup_box

section "install with tools"
start=$(date +%s)
install_box /tmp/install.log --apply --admins owner
echo "     (install took $(( $(date +%s) - start ))s)"
check "every tool installed (no 'some tools failed')" sh -c '! grep -E "some tools failed|^ *error:" /tmp/install.log'
grep -n -iE 'traceback|warning' /tmp/install.log | head -10 | sed 's/^/     log: /'
check "no failed sharedbox units" no_failed_units

# What sharedbox-tools found (tools.json drives the "Preinstalled:" line in every agent's rules)
tools_json_has() {
    python3 - "$@" <<'EOF'
import json, sys
d = json.load(open("/var/lib/sharedbox/tools.json"))
missing = [b for b in sys.argv[1:] if b not in d["tools"]]
print("missing from tools.json:", missing or "nothing")
sys.exit(1 if missing else 0)
EOF
}
check "tools.json lists the advertised tools" tools_json_has claude codex opencode node npm python3 uv pipx git gh \
    tmux rg fdfind jq sqlite3 gcc make go rustup xvfb-run
agents_current() {
    python3 - <<'EOF'
import json, sys
d = json.load(open("/var/lib/sharedbox/tools.json"))["agents"]
print(d)
bad = [n for n in ("@anthropic-ai/claude-code", "@openai/codex", "@opencode/cli", "t3") if not (d.get(n) or {}).get("current")]
sys.exit(1 if bad else 0)
EOF
}
check "every TOOLS_NPM agent has a current version" agents_current
for b in claude codex opencode t3; do
    check "/usr/local/bin/$b is the box's shim" grep -q 'sharedbox agent shim' "/usr/local/bin/$b"
done

section "a friend"
ssh-keygen -q -t ed25519 -N '' -f /tmp/alice_key -C alice@example.com
check "admin add alice" sharedbox-admin add alice --key "$(cat /tmp/alice_key.pub)"
A=/home/alice

section "the preinstalled tools, as alice in a login shell"
for cmd in "claude --version" "codex --version" "opencode --version" "node --version" "npm --version" \
           "python3 --version" "uv --version" "uvx --version" "pipx --version" "git --version" "gh --version" \
           "go version" "rustup --version" "tmux -V" "rg --version" "fdfind --version" "jq --version" \
           "sqlite3 --version" "gcc --version" "make --version" "curl --version" "zip -v" "unzip -v" \
           "command -v xvfb-run"; do
    check "$cmd" as_login alice "$cmd"
done
check "node is 22 or newer" as_login alice "node -e 'process.exit(+process.versions.node.split(\".\")[0] >= 22 ? 0 : 1)'"
check "agents' self-updaters are off (DISABLE_AUTOUPDATER=1)" as_login alice 'test "$DISABLE_AUTOUPDATER" = 1'
check "box agents works" as_login alice "box agents"
check "box status --agent works" as_login alice "box status --agent"

section "installing into alice's home without sudo"
check "npm's global prefix is ~/.local" as_login alice 'test "$(npm config get prefix)" = "$HOME/.local"'
check "npm i -g semver" as_login alice "npm i -g semver@7"
check "semver runs from ~/.local/bin" as_login alice 'test "$(command -v semver)" = "$HOME/.local/bin/semver" && semver 1.2.3'
check "uv tool install cowsay" as_login alice "uv tool install cowsay"
check "cowsay runs from ~/.local/bin" as_login alice 'test "$(command -v cowsay)" = "$HOME/.local/bin/cowsay" && cowsay -t hi'
check "pipx install pyfiglet" as_login alice "pipx install pyfiglet"
check "pyfiglet runs from ~/.local/bin" as_login alice 'test "$(command -v pyfiglet)" = "$HOME/.local/bin/pyfiglet" && pyfiglet hi'
check "go install golang.org/x/example/hello" as_login alice "go install golang.org/x/example/hello@latest"
check "the Go program is on alice's PATH" as_login alice 'command -v hello && hello'
check "cargo works (rustup sets up the default toolchain)" as_login alice "cargo --version"
check "cargo install a local crate" as_login alice \
    'cd /tmp && rm -rf hello-rs && cargo new -q --name hello-rs hello-rs && cargo install -q --path hello-rs'
check "the cargo program is on alice's PATH" as_login alice 'command -v hello-rs && hello-rs'
check "alice can't write the shared tool directories" as_login alice \
    '! touch /usr/local/bin/x 2>/dev/null && ! touch /opt/sharedbox/agents/x 2>/dev/null'

section "alice's agent setup"
for f in .claude/CLAUDE.md .codex/AGENTS.md .config/opencode/AGENTS.md .gemini/GEMINI.md AGENTS.md; do
    check "~/$f has the managed rules block" grep -q 'sharedbox:begin' "$A/$f"
done
check "the rules list the preinstalled tools" grep -q 'Preinstalled: Claude Code' "$A/AGENTS.md"
check "Claude Code's settings run box status at session start" grep -q 'box status --agent' "$A/.claude/settings.json"
check "alice's files belong to alice" sh -c "! find $A -not -user alice | grep ."

section "alice's T3 Code"
t3_pair() { as_user alice box t3 >/tmp/t3.out 2>&1; local rc=$?; cat /tmp/t3.out; [[ $rc == 0 ]] && grep -q "^T3_PAIR_URL=http://$TS_IP:" /tmp/t3.out; }
check "box t3 starts it and prints a pairing link" t3_pair
t3_running() { as_user alice box t3 status | tee /dev/stderr | grep -q 'T3 Code: running'; }
check "box t3 status says running" t3_running

section "nightly agent update is a no-op right after install"
check "sharedbox-tools update exits 0" sharedbox-tools update
check "no failed sharedbox units" no_failed_units

finish
