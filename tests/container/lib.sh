# shellcheck shell=bash
# Shared helpers for the scenarios run.sh starts inside the container (sourced, not run).
FAILS=0
pass() { printf 'PASS %s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*"; FAILS=$((FAILS + 1)); }
check() { local what=$1; shift; if "$@" >/tmp/check.out 2>&1; then pass "$what"; else fail "$what"; sed 's/^/     /' /tmp/check.out | tail -15; fi; }
section() { printf '\n== %s\n' "$*"; }
units() { systemctl list-unit-files 'sharedbox-*' --no-legend 2>/dev/null | awk '{print $1}' | sort; }
no_failed_units() { ! systemctl --failed --no-legend --plain | grep -E 'sharedbox|sb-'; }
finish() {
    printf '\n%s\n' "$([[ $FAILS == 0 ]] && echo "ALL PASSED" || echo "$FAILS FAILED")"
    exit $((FAILS > 0))
}

# The dashboard binds to the box's Tailscale IP. Containers have no tailnet, so give lo a tailnet-range
# address and put a tiny fake `tailscale` (ip / status) on PATH that reports it. Then an owner account.
TS_IP=100.64.0.10
setup_box() {
    section "setup: owner account and a stand-in for Tailscale"
    ip addr add $TS_IP/32 dev lo
    cat > /usr/local/bin/tailscale <<TS
#!/bin/sh
case "\$1" in
  ip) echo $TS_IP ;;
  status) echo '{"Self": {"DNSName": "testbox.example.ts.net.", "UserID": 1, "TailscaleIPs": ["$TS_IP"]}, "User": {}}' ;;
  *) exit 1 ;;
esac
TS
    chmod 755 /usr/local/bin/tailscale
    useradd -m -s /bin/bash owner
    loginctl enable-linger owner
}

# install.sh with its output in a log; the tail of the log on failure
install_box() {  # LOG ARGS...
    local log=$1; shift
    if ./install.sh "$@" >"$log" 2>&1; then pass "install.sh $* exits 0"
    else fail "install.sh $* exits 0"; tail -40 "$log" | sed 's/^/     /'; fi
}

# a command in a user's login shell (/etc/profile.d applies, as for an SSH login or an agent's shell)
as_login() { local u=$1; shift; runuser -l "$u" -c "$*"; }
# a command as a user with their systemd --user manager reachable (systemctl --user, systemd-run --user)
as_user() {
    local u=$1 uid; shift; uid=$(id -u "$u")
    runuser -u "$u" -- env HOME="$(getent passwd "$u" | cut -d: -f6)" XDG_RUNTIME_DIR=/run/user/$uid \
        DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus "$@"
}
wait_for() {  # SECONDS CMD...: until CMD succeeds
    local n=$1; shift
    for _ in $(seq "$n"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
    "$@"
}
