#!/usr/bin/env bash
# sharedbox installer. Safe to re-run (idempotent).
#
#   sudo ./install.sh                    dry run: print what would happen, change nothing
#   sudo ./install.sh --apply            install
#   sudo ./install.sh --apply --admins alice,bob
#   sudo ./install.sh --apply --no-tools   skip installing agents/toolchains (sharedbox-tools install later)
#   sudo ./install.sh --apply --mail-to you@gmail.com   where weekly health reports + alerts go
#
# This script never restarts or reconfigures sshd, tailscaled, networking or the firewall,
# and never changes limits of existing users. It installs files, creates the `friends`
# group, a `sharedbox` system user, runs `systemctl daemon-reload`
# and starts its own units.
set -euo pipefail

APPLY=0
ADMINS=""
TOOLS=1
MAIL_TO=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --apply) APPLY=1 ;;
        --admins) ADMINS=$2; shift ;;
        --no-tools) TOOLS=0 ;;
        --mail-to) MAIL_TO=$2; shift ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

SRC=$(cd "$(dirname "$0")" && pwd)
LIB=/usr/lib/sharedbox SHARE=/usr/share/sharedbox ETC=/etc/sharedbox STATE=/var/lib/sharedbox

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
do_() {
    if [[ $APPLY == 1 ]]; then "$@"; else printf '    would run: %s\n' "$*"; fi
}

# ---------- preflight (read-only) ----------
say "preflight"
[[ $EUID == 0 ]] || { echo "run it with sudo: sudo ./install.sh" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "treehouse needs systemd, which this system doesn't use. Ubuntu 24.04 or Debian 13 have it." >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' \
    || { echo "treehouse needs Python 3.12 or newer and this system has $(python3 -V 2>&1 || echo none). Ubuntu 22.04 and Debian 12 are too old: upgrade to Ubuntu 24.04 or Debian 13 first." >&2; exit 1; }
[[ $(stat -fc %T /sys/fs/cgroup) == cgroup2fs ]] || { echo "treehouse needs cgroup v2 (how Linux limits each account's memory). Ubuntu 22.04+ and Debian 11+ use it by default; something on this machine turned it off (look for systemd.unified_cgroup_hierarchy=0 on the kernel command line)." >&2; exit 1; }
# hardware: a 32-bit PC stops here (the agents can't run); small RAM, a full or spinning disk, a laptop: notes
PF=0; python3 -I "$SRC/lib/sharedbox-tune" preflight || PF=$?
[[ $PF != 3 ]] || exit 1
# distro: debian (apt) | arch (pacman) | unknown. Prints what it found + missing tools with the install command.
DISTRO_FAMILY=unknown
eval "$(python3 -I "$SRC/lib/sbdistro.py" shell)"
python3 -I "$SRC/lib/sbdistro.py" preflight
[[ $DISTRO_FAMILY != unknown ]] || warn "unsupported distro: package installs (sharedbox-tools, smartmontools) will fail; install them yourself"
for t in setpriv ssh-keygen useradd loginctl du; do
    command -v $t >/dev/null || { echo "missing tool: $t" >&2; exit 1; }
done
command -v tailscale >/dev/null || warn "tailscale not found. Set WEB_BIND in $ETC/sharedbox.conf to an IP"
# sshd: never modified here, only checked. Debian names the unit ssh, Arch sshd; Ubuntu may socket-activate it.
SSHD_UNIT=ssh; systemctl cat ssh.service >/dev/null 2>&1 || SSHD_UNIT=sshd
systemctl is-active -q "$SSHD_UNIT.service" "$SSHD_UNIT.socket" 2>/dev/null \
    || warn "sshd is not running, so friends can't log in. Start it yourself: systemctl enable --now $SSHD_UNIT"
if [[ -d /etc/ssh/sshd_config.d && -r /etc/ssh/sshd_config ]] \
    && ! grep -qiE '^[[:space:]]*Include[[:space:]]+[^[:space:]]*sshd_config\.d/' /etc/ssh/sshd_config; then
    warn "/etc/ssh/sshd_config doesn't Include sshd_config.d/, so drop-ins there are ignored: put AllowGroups/PasswordAuthentication in sshd_config itself"
fi

# read existing config if present, else defaults from the shipped file
conf_get() { sed -n "s/^$1=//p" "${ETC}/sharedbox.conf" 2>/dev/null | tail -1 || true; }
SSH_GROUP=$(conf_get SSH_GROUP); SSH_GROUP=${SSH_GROUP:-ssh-users}
FRIENDS_GROUP=$(conf_get FRIENDS_GROUP); FRIENDS_GROUP=${FRIENDS_GROUP:-friends}

ALLOW=$(sshd -T 2>/dev/null | sed -n 's/^allowgroups //p' || true)
if [[ -n $ALLOW ]] && ! tr ' ' '\n' <<<"$ALLOW" | grep -qx "$SSH_GROUP"; then
    warn "sshd AllowGroups is '$ALLOW' and doesn't include SSH_GROUP=$SSH_GROUP. Friends couldn't log in."
    warn "set SSH_GROUP in $ETC/sharedbox.conf to one of those groups. (sshd is not modified by this installer.)"
fi
getent group "$SSH_GROUP" >/dev/null || warn "group $SSH_GROUP does not exist; it will be created"

# Retired security daemons: clean up on every update, including units left by older installs.
for unit in sharedbox-trap sharedbox-canary; do
    do_ systemctl disable --now "$unit.service" 2>/dev/null || true
    do_ rm -f "/etc/systemd/system/$unit.service" "/usr/lib/systemd/system/$unit.service" \
        "$LIB/$unit" "/usr/local/sbin/$unit"
done
do_ python3 -I "$SRC/lib/sbretired.py" "$ETC/sharedbox.conf" "$STATE"
do_ rm -rf "$STATE/trap" "$STATE/alerts"
do_ rm -f "$STATE/canary.jsonl" "$STATE/canary-planted.json"
do_ systemctl daemon-reload

# ---------- accounts ----------
say "groups and service user"
getent group "$FRIENDS_GROUP" >/dev/null || do_ groupadd "$FRIENDS_GROUP"
getent group "$SSH_GROUP" >/dev/null || do_ groupadd "$SSH_GROUP"
getent passwd sharedbox >/dev/null || do_ useradd --system --no-create-home --home-dir /nonexistent \
    --shell /usr/sbin/nologin --user-group sharedbox

# ---------- files ----------
say "files"
do_ install -d -m 755 "$LIB" "$SHARE" "$ETC"
for f in sbcommon.py sbclean.py sbmail.py sbaudit.py sbdistro.py sbaccess.py sbcloudflare.py sbconnect.py sbteam.py sbmulti.py sbfed.py; do do_ install -m 644 "$SRC/lib/$f" "$LIB/$f"; done
for f in box sharedbox-admin sharedbox-janitor sharedbox-web sharedbox-tools sharedbox-health sharedbox-update \
         sharedbox-gate sharedbox-tune; do
    do_ install -m 755 "$SRC/lib/$f" "$LIB/$f"
done
do_ install -m 644 "$SRC/share/AGENTS.md" "$SHARE/AGENTS.md"
do_ install -m 644 "$SRC/share/HELP.md" "$SHARE/HELP.md"
do_ install -m 755 "$SRC/share/cloudflare-setup.sh" "$SHARE/cloudflare-setup.sh"
do_ rm -rf "$SHARE/skel"
do_ cp -r "$SRC/share/skel" "$SHARE/skel"
do_ chmod -R u=rwX,go=rX "$SHARE/skel"
do_ rm -rf "$SHARE/skills"
do_ cp -r "$SRC/share/skills" "$SHARE/skills"   # the shared skill catalog (`box skills`)
do_ chmod -R u=rwX,go=rX "$SHARE/skills"
do_ install -m 644 "$SRC/share/profile.sh" /etc/profile.d/sharedbox.sh
do_ rm -rf "$SHARE/team"
do_ cp -r "$SRC/share/team" "$SHARE/team"   # team mode: crew prompts (`box team crew`)
do_ chmod -R u=rwX,go=rX "$SHARE/team"
do_ install -d -m 755 /srv/teams              # team mode: root creates /srv/teams/NAME (member ACLs)
# box tuning (etc/sysctl-*.conf, `sharedbox-tune`): additive, applied live, nothing restarted. `box tune` checks it.
for f in "$SRC"/etc/sysctl-*.conf; do
    n=$(basename "$f" .conf); do_ install -m 644 "$f" "/etc/sysctl.d/60-sharedbox-${n#sysctl-}.conf"
done
do_ sh -c "printf 'tcp_bbr\\n' > /etc/modules-load.d/sharedbox-bbr.conf"
do_ sh -c "modprobe tcp_bbr 2>/dev/null; for f in /etc/sysctl.d/60-sharedbox-*.conf; do sysctl -q -p \"\$f\"; done" \
    || warn "some box tuning settings didn't apply (see /etc/sysctl.d/60-sharedbox-*.conf); the rest did"
if [[ ! -e $ETC/sharedbox.conf ]]; then
    do_ install -m 644 "$SRC/etc/sharedbox.conf" "$ETC/sharedbox.conf"
    if [[ -n $ADMINS ]]; then do_ sed -i "s/^ADMIN_USERS=.*/ADMIN_USERS=$ADMINS/" "$ETC/sharedbox.conf"; fi
    # less than 20 GB of RAM: per-account limits that fit this machine (still the same for everyone)
    do_ env SHAREDBOX_CONF="$ETC/sharedbox.conf" SHAREDBOX_LIB="$SRC/lib" PYTHONDONTWRITEBYTECODE=1 python3 -I "$SRC/lib/sharedbox-tune" fit-limits
    do_ touch "$ETC/.v2-limits"   # a new conf already has the v2 limits; the one-time migration below must not reset them
else
    say "keeping existing $ETC/sharedbox.conf (new settings use built-in defaults; see $SRC/etc/sharedbox.conf)"
    # v2 (2026-11): equal limits, no naps/throttles, 20-day archive. Old boxes wrote the v1 numbers into the conf
    # explicitly, which would override the new defaults, so rewrite them once (marker: $ETC/.v2-limits).
    if [[ ! -e $ETC/.v2-limits ]]; then
        do_ env SHAREDBOX_CONF="$ETC/sharedbox.conf" PYTHONPATH="$SRC/lib" PYTHONDONTWRITEBYTECODE=1 python3 -c '
import re, sbcommon as sb
p = sb.CONF_PATH
gone = ("CPU_QUOTA", "BURST_CPU_QUOTA", "BURST_MEMORY_HIGH", "BALANCE_CPU_BUSY", "BALANCE_MEM_OK_PCT", "BALANCE_MEM_TIGHT_PCT",
        "OOM_SCORE_ADJ", "BUILD_JOBS", "ARTIFACT_TTL_DAYS", "T3_NAP_MINUTES", "T3_IDLE_DAYS", "DOCKER_NAP_MINUTES",
        "HEAVY_SLOTS", "HEAVY_WAIT_MAX_MIN")
lines = open(p).read().splitlines()
out = [("# v1, removed: " + l) if re.match(r"\s*(%s)=" % "|".join(gone), l) else l for l in lines]
open(p + ".v2tmp", "w").write("\n".join(out) + "\n")
import os, shutil
shutil.copymode(p, p + ".v2tmp")
os.replace(p + ".v2tmp", p)
sb.set_config({"MEMORY_HIGH": "16G", "MEMORY_MAX": "17G", "CPU_WEIGHT": "100", "IO_WEIGHT": "100",
               "PROJECT_TTL_DAYS": "20", "CACHE_TTL_DAYS": "30"}, p)
'
        do_ touch "$ETC/.v2-limits"
    fi
fi
if [[ -n $MAIL_TO ]]; then
    if grep -q '^MAIL_TO=' "$ETC/sharedbox.conf" 2>/dev/null; then do_ sed -i "s/^MAIL_TO=.*/MAIL_TO=$MAIL_TO/" "$ETC/sharedbox.conf"
    else do_ sh -c "printf '\\n# health email (see sharedbox-health)\\nMAIL_TO=%s\\n' '$MAIL_TO' >> $ETC/sharedbox.conf"; fi
fi
do_ ln -sfn "$LIB/box" /usr/local/bin/box
do_ ln -sfn "$LIB/sharedbox-admin" /usr/local/sbin/sharedbox-admin
do_ ln -sfn "$LIB/sharedbox-janitor" /usr/local/sbin/sharedbox-janitor
do_ ln -sfn "$LIB/sharedbox-tools" /usr/local/sbin/sharedbox-tools
do_ ln -sfn "$LIB/sharedbox-health" /usr/local/sbin/sharedbox-health

say "state directory"
do_ install -d -m 755 "$STATE"
do_ install -d -m 700 -o sharedbox -g sharedbox "$STATE/spool"
do_ install -d -m 1753 -g sharedbox "$STATE/requests"   # anyone may drop a request; only root/dashboard can list
# removed features (box heavy queue, T3 naps): their leftovers
[[ -e $STATE/slots ]] && do_ rm -rf "$STATE/slots" "$STATE/t3-seen" "$STATE/t3-naps.json" "$STATE/balance.json"
[[ -e $STATE/users.json ]] || do_ sh -c "echo '{\"users\": {}}' > $STATE/users.json"
[[ -e $STATE/events.jsonl ]] || do_ install -m 644 /dev/null "$STATE/events.jsonl"
do_ install -d -m 755 "$STATE/health"
do_ install -d -m 755 "$STATE/skills"   # skills admins publish with `box skills share`
do_ install -d -m 700 "$STATE/audit"
# public front door: signup invite records, written only by root, readable by sharedbox-gate/-web
do_ install -d -m 750 -g sharedbox "$STATE/gate" "$STATE/gate/invites"
# `box invite` without sudo: anyone can drop a file, root only acts on files owned by ADMIN_USERS
do_ install -d -m 1733 "$STATE/admin"
do_ install -d -m 711 "$STATE/admin-replies"
# Team/boost requests retain their own identity-checked channel.
do_ install -d -m 1733 "$STATE/member-drop"
do_ install -d -m 711 "$STATE/member-replies"


# ---------- cron/at ----------
# cron/at sessions skip pam_systemd, so friends' jobs would run outside their slice limits.
say "cron/at: only root and admins may schedule jobs"
CRON_USERS=$(echo root "$(conf_get ADMIN_USERS)" "$ADMINS" | tr ', ' '\n\n' | sed '/^$/d' | sort -u)
for f in /etc/cron.allow /etc/at.allow; do
    for u in $CRON_USERS; do
        grep -qxF -- "$u" "$f" 2>/dev/null || do_ sh -c 'echo "$1" >> "$2"' _ "$u" "$f"
    done
done
for u in $(getent group "$FRIENDS_GROUP" | cut -d: -f4 | tr ',' ' '); do
    if crontab -l -u "$u" >/dev/null 2>&1; then do_ crontab -r -u "$u"; fi
done

# ---------- tools ----------
if [[ $TOOLS == 1 ]]; then
    if [[ $DISTRO_FAMILY == arch ]]; then
        say "tools: coding agents, toolchains, browser libs (pacman -S --needed only: no -Sy, no system upgrade, no AUR)"
    else
        say "tools: coding agents, toolchains, browser libs (apt install only, no system upgrade)"
    fi
    if [[ $APPLY == 1 ]]; then "$LIB/sharedbox-tools" install || warn "some tools failed; rerun: sudo sharedbox-tools install"
    else PYTHONDONTWRITEBYTECODE=1 SHAREDBOX_LIB="$SRC/lib" SHAREDBOX_CONF="$SRC/etc/sharedbox.conf" python3 -I "$SRC/lib/sharedbox-tools" install --dry-run \
        || echo "    would run: sharedbox-tools install"; fi
fi

# ---------- systemd ----------
# Remove obsolete code and empty feature directories; preserve any historical records (user data).
do_ rm -f "$LIB/sbqr.py"
for old in "$STATE/pending" "$STATE/gate/sessions" "$STATE/gate/logins"; do
    do_ rmdir "$old" 2>/dev/null || true
done
say "systemd units"
# Retired shared-server feature: keep all code and data in /srv/shared.
for u in /etc/systemd/system/sb-shared-*.service; do
    [[ -e $u ]] || continue
    do_ systemctl disable --now "$(basename "$u")" 2>/dev/null || true
    do_ rm -f "$u"
done
do_ systemctl disable --now sharedbox-shared.slice 2>/dev/null || true
do_ rm -f /etc/systemd/system/sharedbox-shared.slice "$LIB/sbshared.py" "$STATE/agent-floors.json" "$STATE/shared.json"
for d in shared-drop shared-replies; do
    do_ rmdir "$STATE/$d" 2>/dev/null || true
done
[[ ! -d /srv/shared ]] || say "Kept /srv/shared and its ownership unchanged (user data)."
# Request status and account notices used to live beside version pins.
if [[ -d $STATE/agent-pins ]]; then
    do_ env PYTHONPATH="$SRC/lib" python3 -c 'import sbcommon; sbcommon.migrate_user_state()'
fi
# Remove retired settings from existing boxes as well as the example config.
do_ sed -i -E '/^(SHARED_(USER|GROUP|DIR|PORT_BASE|PORT_COUNT|MAX|MEMORY_MAX|CPU_QUOTA|PUBLIC_LINKS)|AGENT_(UPDATES_DEFAULT|FORCE_SEVERITY)|REQUEST_REVIEWER|REQUEST_REVIEW_MODEL|REQUEST_REVIEW_CLAUDE_MODEL|OPENROUTER_KEY_FILE)=/d' "$ETC/sharedbox.conf"
# Old user/group IDs still own preserved data; leave them reserved.

do_ env SHAREDBOX_LIB="$LIB" python3 -c '
import importlib.machinery
m = importlib.machinery.SourceFileLoader("tools", "/usr/lib/sharedbox/sharedbox-tools").load_module()
cfg = m.sb.load_config()
for name, _ in m.agent_specs(cfg):
    cur = m.current_version(name)
    for binary in m.agent_bins(name, m.version_dir(name, cur)) if cur else []:
        m.write_shim(cfg, name, binary)
'

# removed units: the busy-box guard (it lowered limits at runtime) and the T3/Docker nap timer (now sharedbox-tick)
for u in sharedbox-guard.timer sharedbox-t3-naps.timer; do
    if [[ -e /etc/systemd/system/$u ]]; then
        do_ systemctl disable --now "$u" 2>/dev/null || true
        do_ rm -f "/etc/systemd/system/$u" "/etc/systemd/system/${u%.timer}.service"
    fi
done
for u in "$SRC"/systemd/*; do do_ install -m 644 "$u" "/etc/systemd/system/$(basename "$u")"; done
do_ systemctl daemon-reload
# equal limits for every account, the owner's included (16G RAM, no CPU cap, equal weight): rewrite every slice
# drop-in, and drop what older versions left (OOM preference for friends, the guard's runtime CPU/RAM values)
do_ "$LIB/sharedbox-admin" reapply >/dev/null
do_ systemctl enable --now sharedbox-provision.path sharedbox-requests.path sharedbox-janitor.timer \
    sharedbox-tools-update.timer sharedbox-tick.timer
# federation (same accounts on every box in BOXES): a signing key once, then a 5-minute timer that does nothing
# until another box is trusted (sharedbox-admin fed-trust; docs/multibox.md)
do_ "$LIB/sharedbox-admin" fed-init
do_ systemctl enable --now sharedbox-fed.timer
do_ systemctl enable sharedbox-tune.service   # every boot; applied right now too:
do_ "$LIB/sharedbox-tune" apply || warn "box tuning (sharedbox-tune apply) didn't fully apply; see \`box tune\`"
do_ systemctl restart sharedbox-janitor.timer sharedbox-tools-update.timer   # pick up schedule changes
do_ systemctl restart sharedbox-provision.path   # pick up new watch paths (it only watches; nothing is interrupted)
do_ systemctl enable sharedbox-web.service
do_ systemctl restart sharedbox-web.service
# public front door: its own unit, only on when PUBLIC_URL is set. Friends' T3 services are not touched.
PUBLIC_URL=$(conf_get PUBLIC_URL); GATE_PORT=$(conf_get GATE_PORT); GATE_PORT=${GATE_PORT:-8091}
PUBLIC_MODE=$(conf_get PUBLIC_MODE)
GATE_DROPIN=/etc/systemd/system/sharedbox-gate.service.d/cloudflare.conf
CF_ENV=/etc/sharedbox/cloudflared.env
if [[ -n $PUBLIC_URL && $PUBLIC_MODE == cloudflare ]]; then
    # Cloudflare Tunnel + Access: the gate verifies Access tokens, so it may fetch the signing keys (outbound HTTPS)
    say "public front door: $PUBLIC_URL via Cloudflare Tunnel + Access -> sharedbox-gate on 127.0.0.1:$GATE_PORT"
    do_ install -d -m 755 "$(dirname "$GATE_DROPIN")"
    do_ sh -c "printf '# managed by sharedbox (PUBLIC_MODE=cloudflare): the gate fetches Cloudflare Access signing keys\n[Service]\nIPAddressDeny=\n' > $GATE_DROPIN"
    do_ systemctl daemon-reload
fi
if [[ -n $PUBLIC_URL && $PUBLIC_MODE == cloudflare ]]; then
    do_ systemctl enable sharedbox-gate.service
    do_ systemctl restart sharedbox-gate.service
else
    [[ -z $PUBLIC_URL ]] || warn "public access disabled: set up Cloudflare (PUBLIC_MODE=cloudflare)"
    do_ systemctl disable --now sharedbox-gate.service
fi
if [[ -n $PUBLIC_URL && $PUBLIC_MODE == cloudflare && -e $CF_ENV ]] && command -v cloudflared >/dev/null; then
    do_ systemctl enable sharedbox-cloudflared.service
    do_ systemctl restart sharedbox-cloudflared.service
elif [[ $PUBLIC_MODE != cloudflare || -z $PUBLIC_URL ]]; then
    if systemctl is-enabled -q sharedbox-cloudflared.service 2>/dev/null; then
        do_ systemctl disable --now sharedbox-cloudflared.service
    fi
    if [[ -e $GATE_DROPIN ]]; then do_ rm -f "$GATE_DROPIN"; do_ systemctl daemon-reload; fi
fi

# ---------- health email ----------
REPORT_AT=$(conf_get HEALTH_REPORT_TIME); REPORT_AT=${REPORT_AT:-Mon 09:00}
say "health: metrics every 5 min, urgent check hourly, weekly report $REPORT_AT"
do_ sh -c "printf '[Unit]\\nDescription=sharedbox health: weekly report ($REPORT_AT)\\n\\n[Timer]\\nOnCalendar=${REPORT_AT% *} *-*-* ${REPORT_AT#* }\\nPersistent=true\\nRandomizedDelaySec=5min\\n\\n[Install]\\nWantedBy=timers.target\\n' > /etc/systemd/system/sharedbox-health-report.timer"
do_ systemctl daemon-reload
do_ systemctl enable --now sharedbox-health-sample.timer sharedbox-health-check.timer sharedbox-health-report.timer
do_ systemctl restart sharedbox-health-report.timer
if ! command -v smartctl >/dev/null; then
    case $DISTRO_FAMILY in
        arch) do_ pacman -S --needed --noconfirm smartmontools \
                  || warn "smartmontools not installed (stale sync db? run a full pacman -Syu); reports won't include disk health" ;;
        unknown) warn "smartmontools not installed; reports won't include disk health. Install it with your package manager" ;;
        *) do_ apt-get install -y --no-install-recommends smartmontools \
               || warn "smartmontools not installed; reports won't include disk health" ;;
    esac
fi
if [[ ! -s $(conf_get SMTP_PASSWORD_FILE || true) && ! -s $ETC/smtp-password ]]; then
    warn "email not set up yet: set MAIL_TO in $ETC/sharedbox.conf (or --mail-to), then put a Gmail app"
    warn "password in $ETC/smtp-password:  sudo install -m 600 /dev/stdin $ETC/smtp-password  (paste, Enter, Ctrl-D)"
    warn "then test: sudo sharedbox-health test-email"
fi
# ---------- nightly security updates ----------
# Ubuntu installs security updates daily around 06:00-07:00 by default. Move that to the night so
# updates that need a reboot are applied by the Automatic-Reboot-Time the same night, not a day later.
# Only the two apt timers get a drop-in; apt's own config is not touched and nothing is run now.
# Arch has no unattended-upgrades, and unattended full upgrades are risky there: nothing is installed or
# scheduled. The weekly health email nags about pending updates (checkupdates) and reboot-required instead.
UPGRADE_TIME=$(conf_get APT_UPGRADE_TIME); UPGRADE_TIME=${UPGRADE_TIME:-03:00}
if [[ $DISTRO_FAMILY == arch ]]; then
    say "updates: Arch has NO automatic security updates, and sharedbox doesn't add any (unattended"
    say "  full upgrades can break Arch). Update by hand regularly: read archlinux.org/news, then"
    say "  $(command -v omarchy-update >/dev/null && echo 'omarchy-update (as yourself)' || echo 'sudo pacman -Syu'), and reboot after kernel updates."
    say "  The weekly health email lists pending updates and says when a reboot is needed."
    command -v checkupdates >/dev/null \
        || warn "checkupdates not found, so the report can't count pending updates: pacman -S --needed pacman-contrib"
elif [[ $UPGRADE_TIME != off ]] && systemctl cat apt-daily-upgrade.timer >/dev/null 2>&1; then
    LISTS_TIME=$(date -d "$UPGRADE_TIME 30 min ago" +%H:%M)
    say "apt: refresh package lists at $LISTS_TIME, install security updates at $UPGRADE_TIME"
    do_ install -d -m 755 /etc/systemd/system/apt-daily.timer.d /etc/systemd/system/apt-daily-upgrade.timer.d
    do_ sh -c "printf '# sharedbox: nightly, ahead of the auto-reboot time\n[Timer]\nOnCalendar=\nOnCalendar=*-*-* $LISTS_TIME\nRandomizedDelaySec=15min\n' > /etc/systemd/system/apt-daily.timer.d/sharedbox.conf"
    do_ sh -c "printf '# sharedbox: nightly, ahead of the auto-reboot time\n[Timer]\nOnCalendar=\nOnCalendar=*-*-* $UPGRADE_TIME\nRandomizedDelaySec=10min\n' > /etc/systemd/system/apt-daily-upgrade.timer.d/sharedbox.conf"
    do_ systemctl daemon-reload
    do_ systemctl restart apt-daily.timer apt-daily-upgrade.timer
    REBOOT_AT=$(apt-config dump 2>/dev/null | sed -n 's/^Unattended-Upgrade::Automatic-Reboot-Time "\(.*\)";/\1/p' | tail -1)
    if apt-config dump 2>/dev/null | grep -qx 'Unattended-Upgrade::Automatic-Reboot "true";'; then
        say "apt: auto-reboot is ON at ${REBOOT_AT:-now} when an update needs it (not managed by sharedbox)"
    else
        say "apt: auto-reboot is off. Updates that need a reboot wait until you reboot by hand"
    fi
fi

# ---------- scheduled reboots ----------
REBOOT_DAYS=$(conf_get SCHEDULED_REBOOT_DAYS)
if [[ -z $(conf_get SCHEDULED_REBOOT_DAYS) ]] && ! grep -q '^SCHEDULED_REBOOT_DAYS=' "$ETC/sharedbox.conf" 2>/dev/null; then
    REBOOT_DAYS=Wed,Sun   # older config without the key: use the shipped default
fi
RB_TIME=$(conf_get REBOOT_TIME); RB_TIME=${RB_TIME:-04:00}
if [[ -n $REBOOT_DAYS ]]; then
    # fire 5 min early: the service warns logged-in users and reboots at RB_TIME
    RB_FIRE=$(date -d "$RB_TIME 5 min ago" +%H:%M)
    say "scheduled reboots: $REBOOT_DAYS at $RB_TIME"
    do_ sh -c "printf '[Unit]\nDescription=sharedbox: scheduled reboot ($REBOOT_DAYS $RB_TIME)\n\n[Timer]\nOnCalendar=$REBOOT_DAYS *-*-* $RB_FIRE\nPersistent=false\n\n[Install]\nWantedBy=timers.target\n' > /etc/systemd/system/sharedbox-reboot.timer"
    do_ systemctl daemon-reload
    do_ systemctl enable sharedbox-reboot.timer
    do_ systemctl restart sharedbox-reboot.timer
else
    say "scheduled reboots: off"
    do_ systemctl disable --now sharedbox-reboot.timer 2>/dev/null || true
    do_ rm -f /etc/systemd/system/sharedbox-reboot.timer
fi

if [[ $APPLY == 1 ]]; then
    sleep 2
    PORT=$(conf_get WEB_PORT); IP=$(tailscale ip -4 2>/dev/null | head -1 || true)
    if systemctl is-active -q sharedbox-web; then
        say "done. Dashboard: http://${IP:-<tailscale-ip>}:${PORT:-8090}/  Join: http://$(hostname):${PORT:-8090}/join"
    else
        warn "sharedbox-web is not running yet. Check: journalctl -u sharedbox-web"
    fi
    # push the new agent rules into every account now, not at the next 6-hourly janitor run
    systemctl start --no-block sharedbox-janitor.service || true
    say "refreshing every user's agent rules (janitor run started)"
    say "next: share this machine with friends in the Tailscale admin console, then send them the text from:"
    say "  sudo sharedbox-admin invite"
fi
if [[ $PUBLIC_MODE != cloudflare || -z $PUBLIC_URL ]]; then
    say "optional public access: set up Cloudflare Tunnel + Access:"
    say "  sudo bash $SHARE/cloudflare-setup.sh"
elif [[ $APPLY == 1 ]]; then
    say "public invites: box invite --email friend@example.com (check: sudo sharedbox-admin cloudflare status)"
fi
[[ $APPLY == 1 ]] || say "dry run only. Re-run with --apply to install."
