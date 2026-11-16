#!/usr/bin/env bash
# Runs as root INSIDE the throwaway container (tests/container/run.sh copies the repo to /src first).
# Walks the whole life of a box: dry run, install, a friend, idempotent re-install, update from git,
# uninstall, reinstall. Prints PASS/FAIL per check and exits non-zero if any check failed.
set -uo pipefail
cd /src
FAILS=0
pass() { printf 'PASS %s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*"; FAILS=$((FAILS + 1)); }
check() { local what=$1; shift; if "$@" >/tmp/check.out 2>&1; then pass "$what"; else fail "$what"; sed 's/^/     /' /tmp/check.out | tail -15; fi; }
section() { printf '\n== %s\n' "$*"; }
units() { systemctl list-unit-files 'sharedbox-*' --no-legend 2>/dev/null | awk '{print $1}' | sort; }
no_failed_units() { ! systemctl --failed --no-legend --plain | grep -E 'sharedbox|sb-'; }
snapshot() {  # what an idempotent re-run must leave unchanged
    { find /etc/systemd/system -maxdepth 2 -name 'sharedbox*' -type f -exec sha256sum {} + | sort
      sha256sum /etc/sharedbox/sharedbox.conf
      find /usr/lib/sharedbox /usr/share/sharedbox -type f -not -path '*/__pycache__/*' -exec sha256sum {} + | sort
      getent group friends ssh-users; getent passwd sharedbox; } 2>/dev/null
}

section "setup: owner account and a stand-in for Tailscale"
# The dashboard binds to the box's Tailscale IP. Containers have no tailnet, so give lo a tailnet-range
# address and put a tiny fake `tailscale` (ip / status / whois) on PATH that reports it.
ip addr add 100.64.0.10/32 dev lo
cat > /usr/local/bin/tailscale <<'TS'
#!/bin/sh
case "$1" in
  ip) echo 100.64.0.10 ;;
  status) echo '{"Self": {"DNSName": "testbox.example.ts.net.", "UserID": 1, "TailscaleIPs": ["100.64.0.10"]}, "User": {}}' ;;
  *) exit 1 ;;
esac
TS
chmod 755 /usr/local/bin/tailscale
useradd -m -s /bin/bash owner
loginctl enable-linger owner

section "dry run changes nothing"
check "dry run exits 0" ./install.sh
check "dry run installed nothing" test ! -e /usr/lib/sharedbox -a ! -e /etc/sharedbox/sharedbox.conf
check "dry run made no units" test -z "$(units)"

section "install"
if ./install.sh --apply --no-tools --admins owner >/tmp/install1.log 2>&1; then pass "install --apply exits 0"
else fail "install --apply exits 0"; tail -30 /tmp/install1.log | sed 's/^/     /'; fi
grep -n -iE 'traceback|error' /tmp/install1.log | grep -viE 'error_|errors?=|no error' | head -5 | sed 's/^/     log: /'
check "box CLI on PATH" test -x /usr/local/bin/box
check "config written" grep -q '^ADMIN_USERS=owner' /etc/sharedbox/sharedbox.conf
check "sharedbox-web active" systemctl is-active -q sharedbox-web.service
for t in sharedbox-janitor.timer sharedbox-tick.timer sharedbox-tools-update.timer sharedbox-health-sample.timer \
         sharedbox-health-check.timer sharedbox-health-report.timer sharedbox-fed.timer; do
    check "$t enabled" systemctl is-enabled -q "$t"
done
check "provision path unit active" systemctl is-active -q sharedbox-provision.path
check "no failed sharedbox units" no_failed_units
PORT=$(sed -n 's/^WEB_PORT=//p' /etc/sharedbox/sharedbox.conf); PORT=${PORT:-8090}
wait_web() { for _ in $(seq 30); do curl -s -o /dev/null --max-time 2 "http://100.64.0.10:$PORT/me" && return 0; sleep 1; done; return 1; }
check "dashboard answers on the tailnet IP" wait_web
check "box help works for the owner" runuser -u owner -- box help
check "box status works for the owner" runuser -u owner -- box status
check "box tune runs" runuser -u owner -- box tune

section "a friend"
ssh-keygen -q -t ed25519 -N '' -f /tmp/alice_key -C alice@example.com
check "admin add alice" sharedbox-admin add alice --key "$(cat /tmp/alice_key.pub)"
check "alice exists and is a friend" sh -c 'id alice | grep -q friends'
check "alice's home is private" sh -c '[ "$(stat -c %a /home/alice)" = 700 ]'
check "alice's key installed" grep -q alice@example.com /home/alice/.ssh/authorized_keys
check "alice has a slice limit" sh -c 'systemctl cat user-$(id -u alice).slice 2>/dev/null | grep -q MemoryMax'
check "box status works for alice" runuser -u alice -- box status
check "alice can't read the owner's home" sh -c '! runuser -u alice -- ls /home/owner >/dev/null 2>&1 || [ "$(stat -c %a /home/owner)" = 755 ]'
check "admin list shows alice" sh -c 'sharedbox-admin list | grep -q alice'

section "re-install is idempotent"
before=$(snapshot)
if ./install.sh --apply --no-tools >/tmp/install2.log 2>&1; then pass "second install exits 0"
else fail "second install exits 0"; tail -20 /tmp/install2.log | sed 's/^/     /'; fi
after=$(snapshot)
if [[ $before == "$after" ]]; then pass "second install changed nothing"
else fail "second install changed nothing"; diff <(echo "$before") <(echo "$after") | head -20 | sed 's/^/     /'; fi
check "alice still there" id alice
check "no failed sharedbox units" no_failed_units

section "update from a git checkout (what the Update button does)"
git config --global user.email ci@example.com; git config --global user.name ci
rm -rf /srv/treehouse-src && cp -r /src /srv/treehouse-src && cd /srv/treehouse-src
git init -q -b main 2>/dev/null || true; git add -A; git commit -qm "test: baseline" || true
echo "# update marker $(date +%s)" >> share/HELP.md; git commit -qam "test: an update"
want=$(git rev-parse --short main); cd /src
python3 - <<'EOF'
import sys; sys.path.insert(0, "/usr/lib/sharedbox")
import sbcommon as sb
sb.set_config({"UPDATE_SOURCE": "/srv/treehouse-src", "UPDATE_BRANCH": "main"})
EOF
if /usr/lib/sharedbox/sharedbox-update test >/tmp/update.log 2>&1; then pass "sharedbox-update exits 0"
else fail "sharedbox-update exits 0"; cat /tmp/update.log /var/lib/sharedbox/update.log 2>/dev/null | tail -20 | sed 's/^/     /'; fi
check "update.json says done at the new commit" python3 -c "
import json; d = json.load(open('/var/lib/sharedbox/update.json')); assert d['state'] == 'done' and d['commit'] == '$want', d"
check "the update reached the installed files" grep -q 'update marker' /usr/share/sharedbox/HELP.md
check "dashboard answers after the update" wait_web
check "no failed sharedbox units" no_failed_units

section "uninstall"
if ./uninstall.sh >/tmp/uninstall.log 2>&1; then pass "uninstall exits 0"
else fail "uninstall exits 0"; tail -20 /tmp/uninstall.log | sed 's/^/     /'; fi
check "no sharedbox unit files left" test -z "$(units)"
check "no sharedbox processes left" sh -c '! pgrep -af /usr/lib/sharedbox | grep -v pgrep'
check "program files removed" test ! -e /usr/lib/sharedbox -a ! -e /usr/share/sharedbox
check "no dangling links in /usr/local" sh -c '! find /usr/local/bin /usr/local/sbin -xtype l | grep .'
check "no sharedbox sysctl files left" sh -c '! ls /etc/sysctl.d/60-sharedbox-* 2>/dev/null | grep .'
check "friend account kept" id alice
check "friend home kept" test -d /home/alice
check "config kept" test -e /etc/sharedbox/sharedbox.conf
check "no failed units after uninstall" no_failed_units

section "reinstall after uninstall"
if ./install.sh --apply --no-tools >/tmp/install3.log 2>&1; then pass "reinstall exits 0"
else fail "reinstall exits 0"; tail -20 /tmp/install3.log | sed 's/^/     /'; fi
check "dashboard answers again" wait_web
check "box status works for alice again" runuser -u alice -- box status
check "no failed sharedbox units" no_failed_units

printf '\n%s\n' "$([[ $FAILS == 0 ]] && echo "ALL PASSED" || echo "$FAILS FAILED")"
exit $((FAILS > 0))
