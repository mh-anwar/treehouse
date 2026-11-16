#!/usr/bin/env bash
# SCENARIO=t3switch. Runs as root INSIDE the throwaway container (tests/container/run.sh copies the repo to /src).
# The owner's T3 Code runs as T3's own t3code.service on OWNER_T3_PORT; `box t3 switch` moves it under `box t3`
# (sb-t3.service, lib/box cmd_t3_switch). With a stand-in t3 (fake-t3: launcher, preflight, server, pair) and
# shortened wait times, it checks:
#   - a new version that can't open the database: the switch fails and nothing changes
#   - the new T3 dies after migrating the database: rollback restores the database and the old service
#   - the switch waits while an agent turn is unfinished and while an agent's command runs, then switches once
#     quiet: same port, t3code.service stopped and kept off, database backed up and intact, `box t3` works
set -uo pipefail
cd /src
. /src/tests/container/lib.sh
setup_box

PORT=3773 QUIET=8 SAMPLE=1
OH=/home/owner OUID=$(id -u owner)
DB=$OH/.t3/userdata/statev2.sqlite
STATE=$OH/.sharedbox/t3-switch.json

section "install, the owner's T3 as T3's own service"
install_box /tmp/install.log --apply --no-tools --admins owner
python3 - <<EOF
import sys; sys.path.insert(0, "/usr/lib/sharedbox")
import sbcommon as sb
# no scheduled reboot: one due within 30 minutes would (rightly) hold the switch whatever day CI runs
sb.set_config({"OWNER_T3_PORT": "$PORT", "SCHEDULED_REBOOT_DAYS": ""})
EOF
install -m 755 /src/tests/container/fake-t3 /usr/local/bin/t3
mkdir -p /etc/fake-t3

# what T3's own installer leaves: runtime, ~/.local/bin/t3 -> it, service.env, the user unit, a database
runuser -u owner -- bash -e <<EOF
mkdir -p $OH/.t3/runtime/versions/0.1.0 $OH/.t3/userdata $OH/.local/bin $OH/.config/systemd/user $OH/.claude/shell-snapshots
install -m 755 /src/tests/container/fake-t3 $OH/.t3/runtime/versions/0.1.0/t3
ln -sfn $OH/.t3/runtime/versions/0.1.0/t3 $OH/.local/bin/t3
echo T3CODE_HOST=$TS_IP > $OH/.t3/service.env
cat > $OH/.config/systemd/user/t3code.service <<UNIT
[Unit]
Description=T3 Code server (stand-in)

[Service]
WorkingDirectory=%h
Environment=T3CODE_HOME=%h/.t3
Environment=T3CODE_PORT=$PORT
ExecStart=%h/.t3/runtime/versions/0.1.0/t3 __service-launcher
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
UNIT
python3 - <<'PY'
import sqlite3
con = sqlite3.connect("$DB")
con.executescript("""
CREATE TABLE orchestration_v2_projection_runs (run_id TEXT, status TEXT, requested_at TEXT, completed_at TEXT);
CREATE TABLE orchestration_v2_projection_runtime_requests (runtime_request_id TEXT, status TEXT, resolved_at TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE orchestration_v2_projection_turn_items (turn_item_id TEXT, type TEXT, status TEXT, run_id TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE orchestration_v2_projection_subagents (subagent_id TEXT, status TEXT, run_id TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE orchestration_v2_thread_launch_workflows (command_id TEXT, status TEXT);
CREATE TABLE scheduled_tasks (task_id TEXT, enabled INTEGER, next_run_at TEXT);
CREATE TABLE effect_sql_migrations (migration_id INTEGER);
CREATE TABLE threads (id TEXT, title TEXT);
INSERT INTO effect_sql_migrations VALUES (10);
INSERT INTO threads VALUES ('t1', 'the owner''s chat history');
INSERT INTO orchestration_v2_projection_runs VALUES ('old', 'completed', '2026-01-01T00:00:00.000Z', '2026-01-01T00:01:00.000Z');
""")
con.commit()
PY
EOF
check "the owner's systemd --user is up" wait_for 30 test -S /run/user/$OUID/systemd/private
ou() { as_user owner "$@"; }
octl() { ou systemctl --user "$@"; }
octl daemon-reload
check "t3code.service starts" octl enable --now t3code.service
answers() {  # VERSION: T3 answers on the owner's port, and it's that version
    curl -sf --max-time 2 "http://$TS_IP:$PORT/api/auth/session" | grep -q "\"version\": \"$1\""
}
check "the old T3 answers on $TS_IP:$PORT" wait_for 20 answers 0.1.0
status_has() { ou box t3 status >/tmp/status.out 2>&1; cat /tmp/status.out; grep -q "$1" /tmp/status.out; }
check "box t3 status: still T3's own service" status_has "running as T3's own t3code.service on port $PORT"

switch() { ou env SHAREDBOX_T3_SWITCH_QUIET=$QUIET SHAREDBOX_T3_SWITCH_SAMPLE=$SAMPLE box t3 switch; }
state_is() {  # STATE [REASON-SUBSTRING]
    python3 -c "
import json, sys
st = json.load(open('$STATE'))
sys.exit(0 if st.get('state') == sys.argv[1] and sys.argv[2] in ' '.join(st.get('waiting_on') or []) + st.get('reason', '') else 1)
" "$1" "${2:-}"
}
show_state() { cat "$STATE" 2>/dev/null; echo; ou journalctl --user -u sb-t3-switch -n 30 --no-pager 2>/dev/null | tail -30; }
mig_is() { local m; m=$(migration "$1"); echo "migration $m"; [[ $m == "$2" ]]; }
migration() { python3 -c "import sqlite3; print(sqlite3.connect('file:$1?mode=ro', uri=True).execute('SELECT max(migration_id) FROM effect_sql_migrations').fetchone()[0])"; }
has_history() { python3 -c "import sqlite3; assert sqlite3.connect('file:$1?mode=ro', uri=True).execute(\"SELECT count(*) FROM threads WHERE title = 'the owner''s chat history'\").fetchone()[0] == 1"; }
old_back() {  # the old T3 Code is the one running, and box's marks are gone
    octl is-active -q t3code.service && octl is-enabled -q t3code.service && ! octl is-active -q sb-t3.service \
        && ! octl is-enabled -q sb-t3.service && [[ ! -e $OH/.sharedbox/t3-managed ]] \
        && [[ ! -e $OH/.config/systemd/user/t3code.service.d/sb-managed.conf ]] && answers 0.1.0
}

section "a new version that can't open the database: nothing changes"
touch /etc/fake-t3/preflight-refuses
check "box t3 switch starts" switch
check "the switch fails and says why" wait_for 30 state_is failed "can't run on this database"
state_is failed || show_state | sed 's/^/     /'
check "the old T3 still runs and answers" old_back
check "database untouched" mig_is "$DB" 10
rm /etc/fake-t3/preflight-refuses

section "the new T3 dies after migrating the database: rollback"
touch /etc/fake-t3/serve-fails
check "box t3 switch starts" switch
check "it rolls back" wait_for 90 state_is rolled-back "didn't start"
state_is rolled-back || show_state | sed 's/^/     /'
check "the old T3 runs again, sb-t3 is off" wait_for 30 old_back
check "the database is restored to before the new version's migration" mig_is "$DB" 10
check "the owner's chat history is intact" has_history "$DB"
check "~/.local/bin/t3 points at the old runtime again" \
    test "$(readlink $OH/.local/bin/t3)" = "$OH/.t3/runtime/versions/0.1.0/t3"
check "box t3 status shows the rollback" status_has "Switch rolled-back"
rm /etc/fake-t3/serve-fails

section "the switch waits while an agent works"
python3 - <<EOF
import sqlite3, time
con = sqlite3.connect("$DB")
con.execute("INSERT INTO orchestration_v2_projection_runs VALUES ('live', 'running', ?, NULL)",
            (time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),))
con.commit()
EOF
check "box t3 switch starts" switch
check "it waits on the unfinished agent turn" wait_for 30 state_is waiting "agent turns not finished"
sleep $((QUIET + 4))
check "still waiting after the quiet period (the turn is still running)" state_is waiting "agent turns not finished"
check "the old T3 still serves" answers 0.1.0
# the turn ends, but a command the agent started is still running (Claude Code's shell, in T3's agent slice)
: > $OH/.claude/shell-snapshots/snapshot-bash-1.sh
check "an agent command starts in T3's agent slice" ou systemd-run --user --quiet --unit=fake-agent-cmd --slice=app-t3code.slice \
    bash -c "source $OH/.claude/shell-snapshots/snapshot-bash-1.sh; sleep 600; true"
python3 -c "import sqlite3; c = sqlite3.connect('$DB'); c.execute(\"UPDATE orchestration_v2_projection_runs SET status = 'completed' WHERE run_id = 'live'\"); c.commit()"
check "it waits on the agent's command" wait_for 30 state_is waiting "agent commands running"
sleep $((QUIET + 4))
check "still waiting after the quiet period (the command still runs)" state_is waiting "agent commands running"
check "the old T3 still serves" answers 0.1.0
octl stop fake-agent-cmd.service

section "quiet: it switches"
check "the switch completes" wait_for $((QUIET * 3 + 60)) state_is "done"
state_is "done" || show_state | sed 's/^/     /'
check "the box's T3 answers on the same port" wait_for 20 answers 0.2.0
new_on() { octl is-active sb-t3.service && octl is-enabled sb-t3.service; }
check "sb-t3.service is active and enabled" new_on
old_off() { ! octl is-active -q t3code.service && ! octl is-enabled -q t3code.service; }
check "t3code.service is stopped and disabled" old_off
old_kept_off() { octl start t3code.service; sleep 3; ! octl is-active -q t3code.service && answers 0.2.0; }
check "t3code.service can't take the port back" old_kept_off
check "the new T3 migrated the database" mig_is "$DB" 11
check "the owner's chat history is intact" has_history "$DB"
BACKUP=$OH/.t3/sb-backup/statev2-before-switch.sqlite
check "a backup was taken before the new version started" mig_is "$BACKUP" 10
check "the backup has the chat history" has_history "$BACKUP"
check "~/.local/bin/t3 is the box's t3" test "$(readlink $OH/.local/bin/t3)" = /usr/local/bin/t3
check "box t3 status: run by the box" status_has "run by treehouse): running at http://$TS_IP:$PORT/"
check "box t3 status: switch done" status_has "Switch: done"
pair() { as_user owner box t3 >/tmp/pair.out 2>&1; cat /tmp/pair.out; grep -q "^T3_PAIR_URL=http://$TS_IP:$PORT/pair" /tmp/pair.out; }
check "box t3 gives the owner a pairing link" pair
again() { switch >/tmp/again.out 2>&1; cat /tmp/again.out; grep -q 'Nothing to switch' /tmp/again.out; }
check "box t3 switch again: nothing to switch" again
check "sb-t3 restarts cleanly" octl restart sb-t3.service
check "and answers again" wait_for 20 answers 0.2.0
check "no failed sharedbox units" no_failed_units

finish
