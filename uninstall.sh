#!/usr/bin/env bash
# Remove sharedbox's services and files. Friend accounts and their homes are KEPT
# unless --remove-users is given. Never touches sshd, tailscaled or networking.
#
#   sudo ./uninstall.sh [--remove-users]
set -euo pipefail
[[ $EUID == 0 ]] || { echo "run with sudo" >&2; exit 1; }

if [[ ${1:-} == --remove-users ]]; then
    for u in $(python3 -c 'import json;print(" ".join(json.load(open("/var/lib/sharedbox/users.json"))["users"]))' 2>/dev/null); do
        sharedbox-admin remove "$u" --archive || true
    done
fi

systemctl disable --now sharedbox-web.service sharedbox-provision.path \
    sharedbox-janitor.timer sharedbox-guard.timer sharedbox-tools-update.timer sharedbox-reboot.timer 2>/dev/null || true
rm -f /etc/systemd/system/sharedbox-*.{service,timer,path}
rm -f /etc/systemd/system/apt-daily.timer.d/sharedbox.conf /etc/systemd/system/apt-daily-upgrade.timer.d/sharedbox.conf
rmdir /etc/systemd/system/apt-daily.timer.d /etc/systemd/system/apt-daily-upgrade.timer.d 2>/dev/null || true
systemctl daemon-reload
systemctl restart apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true   # back to Ubuntu's schedule
rm -rf /usr/lib/sharedbox /usr/share/sharedbox
rm -f /usr/local/bin/box /usr/local/sbin/sharedbox-admin /usr/local/sbin/sharedbox-janitor /usr/local/sbin/sharedbox-tools \
    /etc/profile.d/sharedbox.sh
echo "removed. Kept: system packages and agents installed by sharedbox-tools, /etc/sharedbox, /var/lib/sharedbox (archives, registry), friend accounts and their limit drop-ins."
