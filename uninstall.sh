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

# every sharedbox unit, whatever this or an older version installed: timers and path units first, so nothing
# starts a service again while the services are being stopped
mapfile -t UNITS < <({ systemctl list-unit-files 'sharedbox-*' --no-legend --plain; systemctl list-units 'sharedbox-*' --all --no-legend --plain; } 2>/dev/null \
    | awk '{print $1}' | grep -E '^sharedbox-' | sort -u)
if [[ ${#UNITS[@]} -gt 0 ]]; then
    for u in "${UNITS[@]}"; do [[ $u == *.timer || $u == *.path ]] && systemctl disable --now "$u" 2>/dev/null; done
    for u in "${UNITS[@]}"; do [[ $u == *.timer || $u == *.path ]] || systemctl disable --now "$u" 2>/dev/null; done
fi
true
# files of retired features (decoy-port trap, canary tripwire): only the canary's own unchanged decoys
python3 -I "$(dirname "$0")/lib/sbretired.py" /etc/sharedbox/sharedbox.conf /var/lib/sharedbox || true
rm -rf /var/lib/sharedbox/trap /var/lib/sharedbox/alerts
rm -f /var/lib/sharedbox/canary.jsonl /var/lib/sharedbox/canary-planted.json
# shared servers (box shared): stopped and their units removed; their code in /srv/shared is kept
for u in /etc/systemd/system/sb-shared-*.service; do
    [[ -e $u ]] && systemctl disable --now "$(basename "$u")" 2>/dev/null || true
done
rm -f /etc/systemd/system/sb-shared-*.service /etc/systemd/system/sharedbox-shared.slice
rm -f /etc/systemd/system/sharedbox-*.{service,timer,path,slice}
rm -rf /etc/systemd/system/sharedbox-gate.service.d
# apt timer drop-ins exist only on Debian/Ubuntu (install.sh adds nothing like it on Arch); no-ops elsewhere
rm -f /etc/systemd/system/apt-daily.timer.d/sharedbox.conf /etc/systemd/system/apt-daily-upgrade.timer.d/sharedbox.conf
rmdir /etc/systemd/system/apt-daily.timer.d /etc/systemd/system/apt-daily-upgrade.timer.d 2>/dev/null || true
systemctl daemon-reload
if systemctl cat apt-daily-upgrade.timer >/dev/null 2>&1; then
    systemctl restart apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true   # back to Ubuntu's schedule
fi
# box tuning: back to the kernel's defaults at the next reboot (nothing is changed on the running system)
rm -f /etc/sysctl.d/60-sharedbox-*.conf /etc/modules-load.d/sharedbox-bbr.conf
if [[ -e /swap.treehouse ]]; then swapoff /swap.treehouse 2>/dev/null && rm -f /swap.treehouse; fi
# every command link into the program folder (box, sharedbox-admin, ...), then the folder itself
for d in /usr/local/bin /usr/local/sbin; do
    for l in "$d"/*; do
        [[ -L $l && $(readlink "$l") == /usr/lib/sharedbox/* ]] && rm -f "$l"
    done
done
rm -rf /usr/lib/sharedbox /usr/share/sharedbox
rm -f /etc/profile.d/sharedbox.sh
systemctl reset-failed 'sharedbox-*' 2>/dev/null || true
echo "if you turned on the public front door: sudo tailscale funnel --https=443 off   (not done here; tailscaled is never touched)"
echo "Cloudflare mode: the tunnel, DNS record and Access app stay in your Cloudflare account (delete them in the dashboard); cloudflared stays installed"
echo "removed. Kept: system packages and agents installed by sharedbox-tools, /etc/sharedbox, /var/lib/sharedbox (archives, registry), friend accounts and their limit drop-ins, shared servers' code in /srv/shared and the sbshared user."
