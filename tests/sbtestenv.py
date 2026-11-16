"""One throwaway config + state dir for the whole test run. Every test module imports this FIRST.

sbcommon reads SHAREDBOX_CONF / SHAREDBOX_STATE once, when it's first imported (and load_config's default path is
fixed then), so whichever test module imports it first decides for all of them. Without this, that depended on
the order modules were discovered in, and could fall back to this machine's real /etc/sharedbox and
/var/lib/sharedbox. Not a test module itself (no test_ prefix)."""

import atexit
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
TMP = tempfile.mkdtemp(prefix="sbtest-")
CONF = os.path.join(TMP, "sharedbox.conf")
STATE = os.path.join(TMP, "state")
SITE = "https://treehouse.example.com"

with open(CONF, "w") as f:
    f.write(f"PUBLIC_URL={SITE}\nPUBLIC_MODE=cloudflare\nCF_ACCOUNT_ID=acct12345\nCF_TUNNEL_ID=tunnel12345\n"
            "CF_ACCESS_POLICY_ID=policy12345\nCF_TEAM_DOMAIN=team.cloudflareaccess.com\n"
            "CF_ACCESS_AUD=aud0123456789abcdef\nCF_OWNER_EMAILS=owner@example.com\n"
            "SSH_HOST=treehouse-ssh.example.com\nCF_SSH_APP_ID=sshapp12345\n"
            "WEB_PORT=8090\nREQUEST_AUTO_INSTALL=yes\n")
os.makedirs(os.path.join(STATE, "gate"), exist_ok=True)
os.environ.update(SHAREDBOX_CONF=CONF, SHAREDBOX_STATE=STATE, SHAREDBOX_LIB=LIB,
                  SHAREDBOX_SHARE=os.path.join(ROOT, "share"), SHAREDBOX_WEB_SOCK=os.path.join(TMP, "web.sock"))
os.environ.pop("T3CODE_HOME", None)  # the T3 test sets its own
if LIB not in sys.path:
    sys.path.insert(0, LIB)
atexit.register(shutil.rmtree, TMP, True)
