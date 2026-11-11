"""Outgoing mail for sharedbox (weekly report, alerts). stdlib smtplib, STARTTLS or SMTPS.

Config (sharedbox.conf): MAIL_TO, MAIL_FROM, SMTP_HOST, SMTP_PORT, SMTP_USER.
The password lives alone in SMTP_PASSWORD_FILE (root-only, 0600), never in the config.
"""

import os
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid


class MailNotConfigured(Exception):
    pass


def configured(cfg):
    return bool(cfg.get("MAIL_TO") and cfg.get("SMTP_HOST") and os.path.exists(cfg["SMTP_PASSWORD_FILE"]))


def send(cfg, subject, text, html=None, to=None):
    if not cfg.get("MAIL_TO") or not cfg.get("SMTP_HOST"):
        raise MailNotConfigured("set MAIL_TO and SMTP_HOST in sharedbox.conf")
    pw_file = cfg["SMTP_PASSWORD_FILE"]
    try:
        st = os.stat(pw_file)
        with open(pw_file) as f:
            password = f.read().strip()
    except FileNotFoundError:
        raise MailNotConfigured(f"missing {pw_file} (put the SMTP/app password in it, chmod 600)")
    if st.st_mode & 0o077:
        raise MailNotConfigured(f"{pw_file} is readable by others; chmod 600 it")
    user = cfg.get("SMTP_USER") or cfg["MAIL_TO"].split(",")[0].strip()
    sender = cfg.get("MAIL_FROM") or user
    host = socket.gethostname()

    msg = EmailMessage()
    msg["Subject"] = f"[{host}] {subject}"
    msg["From"] = f"{(cfg.get('BRAND') or 'treehouse').strip()} on {host} <{sender}>"
    msg["To"] = to or cfg["MAIL_TO"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=host)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    port = int(cfg.get("SMTP_PORT") or 587)
    ctx = ssl.create_default_context()
    if port == 465:
        s = smtplib.SMTP_SSL(cfg["SMTP_HOST"], port, context=ctx, timeout=30)
    else:
        s = smtplib.SMTP(cfg["SMTP_HOST"], port, timeout=30)
        s.starttls(context=ctx)
    with s:
        s.login(user, password)
        s.send_message(msg)
