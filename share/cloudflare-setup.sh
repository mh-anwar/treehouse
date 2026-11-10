#!/usr/bin/env bash
# treehouse: one-time Cloudflare Tunnel + Access setup, so friends reach the box from any browser with an email
# code, no Tailscale for them, and nobody who isn't invited ever reaches this machine. Safe to re-run.
#
#   sudo bash /usr/share/sharedbox/cloudflare-setup.sh
#
# It asks for (1) a Cloudflare API token (typed blind, saved to /etc/sharedbox/cloudflare-token, root, 600;
# never echoed, never on a command line), (2) the hostname friends will open, (3) your own email. Then it runs
# `sharedbox-admin cloudflare setup`, which creates the tunnel, DNS record and Access app, installs
# cloudflared and starts it with the gate, and prints a status check.
set -euo pipefail
umask 077

ETC=/etc/sharedbox
CONF=$ETC/sharedbox.conf
TOKEN_FILE=$ETC/cloudflare-token

[[ $EUID == 0 ]] || { echo "Run it with sudo:  sudo bash $0"; exit 1; }
command -v sharedbox-admin >/dev/null || { echo "treehouse isn't installed yet: run install.sh --apply first."; exit 1; }
[[ -e /dev/tty ]] || { echo "Run this in a terminal (it asks questions)."; exit 1; }

conf_get() { sed -n "s/^$1=//p" "$CONF" 2>/dev/null | tail -1 || true; }

cat <<'EOF'

treehouse: Cloudflare login for your friends
--------------------------------------------
You need a Cloudflare API token with these permissions (custom token):
  Account  Cloudflare Tunnel                                      Edit
  Account  Access: Apps and Policies                              Edit
  Account  Access: Organizations, Identity Providers, and Groups  Read
  Zone     DNS                                                    Edit
  Zone     Zone                                                   Read
Create it at https://dash.cloudflare.com/profile/api-tokens (Create Token > Custom token).

EOF

# ---- 1. API token (never echoed; written straight from this shell to a 600 file) ----
USE_SAVED=n
if [[ -s $TOKEN_FILE ]]; then
    read -rp "A token is already saved. Use it? [Y/n] " ans </dev/tty
    [[ ${ans,,} == n* ]] || USE_SAVED=y
fi
if [[ $USE_SAVED != y ]]; then
    TOK=""
    for _ in 1 2 3; do
        read -rsp "Paste the API token, then press Enter (it won't show): " TOK </dev/tty; echo
        TOK=${TOK//[[:space:]]/}
        [[ $TOK =~ ^[A-Za-z0-9_-]{20,200}$ ]] && break
        echo "That doesn't look like a Cloudflare API token (${#TOK} characters). Try again."
        TOK=""
    done
    [[ -n $TOK ]] || { echo "No token saved. Run this again when you have it."; exit 1; }
    install -d -m 755 "$ETC"
    rm -f "$TOKEN_FILE"
    install -m 600 -o root -g root /dev/null "$TOKEN_FILE"
    printf '%s\n' "$TOK" > "$TOKEN_FILE"   # printf is a shell builtin: the token is in no process's argv
    unset TOK
    echo "Saved to $TOKEN_FILE (only root can read it)."
fi
chmod 600 "$TOKEN_FILE"; chown root:root "$TOKEN_FILE"

# ---- 2. hostname ----
CUR=$(conf_get PUBLIC_URL); CUR=${CUR#https://}
[[ $(conf_get PUBLIC_MODE) == cloudflare ]] || CUR=""
HOST=""
while [[ -z $HOST ]]; do
    read -rp "Address friends will open, on a domain you have in Cloudflare (e.g. box.example.com)${CUR:+ [$CUR]}: " HOST </dev/tty
    HOST=${HOST:-$CUR}
    HOST=${HOST,,}; HOST=${HOST#https://}; HOST=${HOST#http://}; HOST=${HOST%%/*}; HOST=${HOST%.}
    if [[ ! $HOST =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]]; then
        echo "That isn't a hostname like box.example.com. Try again."; HOST=""
    fi
done

# ---- 3. owner email ----
CUR=$(conf_get CF_OWNER_EMAILS); CUR=${CUR%%,*}
EMAIL=""
while [[ -z $EMAIL ]]; do
    read -rp "Your email (you can always log in with it)${CUR:+ [$CUR]}: " EMAIL </dev/tty
    EMAIL=${EMAIL:-$CUR}; EMAIL=${EMAIL,,}; EMAIL=${EMAIL//[[:space:]]/}
    [[ $EMAIL =~ ^[^@]+@[a-z0-9-]+(\.[a-z0-9-]+)+$ ]] || { echo "That doesn't look like an email. Try again."; EMAIL=""; }
done

# ---- 4. Cloudflare side + config + cloudflared + gate (all idempotent) ----
echo
sharedbox-admin cloudflare setup --hostname "$HOST" --owner-email "$EMAIL"

# ---- 5. check ----
echo
sleep 3
if sharedbox-admin cloudflare status; then
    echo
    echo "All set. Open https://$HOST/ yourself to try the login, then invite a friend:"
    echo "  box invite --email friend@example.com --note \"Sam\""
else
    echo
    echo "Something above isn't green yet. The tunnel can take a minute to connect: run"
    echo "  sudo sharedbox-admin cloudflare status"
    echo "again in a minute. This script is safe to re-run."
fi
