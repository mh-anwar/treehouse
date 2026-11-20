"""Telegram for treehouse: message cards on people's phones, plus /where and /away, without a terminal.

One bot per box, run by root (`sharedbox-admin telegram-bot`, sharedbox-telegram.service), long-polling Telegram
(no public webhook, nothing listens). Linking is the website's job: the dashboard's "Connect Telegram" button asks
root for a one-time code and shows a t.me link (and a QR code for a laptop). Opening it sends `/start CODE` to the
bot, which ties that chat to the account. Nobody types a username or a token.

  /etc/sharedbox/telegram-token          the bot token from @BotFather (0600 root; `sharedbox-admin telegram setup`,
                                         or the owner's dashboard)
  STATE_DIR/telegram/                    0700 root
      links.json   {user: {chat, handle, since, notify: all|important|off}}
      codes.json   {code: {user, exp}}   one-time link codes, 15 minutes
      seen.json    {user: {last, ids}}   what has been pushed already (a link starts from "now": no history blast)
      pending.json {chat: {reply_to, exp}}  "Reply" pressed, waiting for the text
  STATE_DIR/telegram-bot.json            0644: {username, ok, error, checked} for the dashboard

Everything a person does in Telegram goes through the same code as `box msg` / `box where` (sbmsg.deliver,
sbwhere.handle), as the linked account, so the rules (rate limits, masking, who may message whom) are the same.
Messages are information: the bot never runs anything from one.

Stdlib only. The network part (api) is injected so tests run without Telegram.
"""

import html
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request

import sbmsg
import sbwhere

CODE_TTL = 15 * 60
REPLY_TTL = 10 * 60
NOTIFY = ("all", "important", "off")
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
API = "https://api.telegram.org/bot{token}/{method}"
LEVEL_LABEL = {"critical": "URGENT", "high": "High", "normal": "", "low": "Low"}
HELP = ("Here's what I can do:\n"
        "/inbox  your open messages\n"
        "/where NAME  where someone is (just /where for everyone)\n"
        "/away TEXT  set your status, e.g. /away in class till 3pm\n"
        "/back  clear your status\n"
        "/quiet  only urgent ones and ones due within a day (the default)\n"
        "/loud  every message\n"
        "/stop  disconnect this chat from your account\n\n"
        "New messages show up here by themselves. Tap Reply under one to answer it.")


def state_dir(sb):
    return os.path.join(sb.STATE_DIR, "telegram")


def bot_info_path(sb):
    return os.path.join(sb.STATE_DIR, "telegram-bot.json")


def _load(path, default):
    try:
        with open(path) as f:
            v = json.load(f)
        return v if isinstance(v, type(default)) else default
    except (OSError, ValueError):
        return default


def _save(path, data, mode=0o600):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w") as f:
        json.dump(data, f)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


class Store:
    """The bot's state files under one directory (root's in production, a temp dir in tests)."""

    def __init__(self, d):
        self.d = d

    def get(self, name, default=None):
        return _load(os.path.join(self.d, f"{name}.json"), {} if default is None else default)

    def put(self, name, data):
        _save(os.path.join(self.d, f"{name}.json"), data)


# ---------- linking (root, from the dashboard) ----------

def new_code(store, user, now=None):
    """A one-time code that links whichever Telegram chat sends it to `user`."""
    now = time.time() if now is None else now
    codes = {c: r for c, r in store.get("codes").items() if r.get("exp", 0) > now and r.get("user") != user}
    code = secrets.token_urlsafe(18)
    codes[code] = {"user": user, "exp": int(now + CODE_TTL)}
    store.put("codes", codes)
    return code


def status(store, user):
    rec = store.get("links").get(user)
    if not rec:
        return {"linked": False}
    return {"linked": True, "handle": rec.get("handle") or "", "since": rec.get("since"),
            "notify": rec.get("notify") if rec.get("notify") in NOTIFY else "important"}


def unlink(store, user):
    links = store.get("links")
    rec = links.pop(user, None)
    store.put("links", links)
    return rec


def set_notify(store, user, level):
    if level not in NOTIFY:
        raise sbmsg.MsgError(f"notify: one of {', '.join(NOTIFY)}")
    links = store.get("links")
    if user not in links:
        raise sbmsg.MsgError("Telegram isn't connected")
    links[user]["notify"] = level
    store.put("links", links)


def _redeem(store, code, chat, handle, now):
    codes = store.get("codes")
    rec = codes.pop(code, None)
    store.put("codes", codes)
    if not rec or rec.get("exp", 0) < now:
        return None
    user = rec["user"]
    links = {u: r for u, r in store.get("links").items() if r.get("chat") != chat}  # one account per chat
    links[user] = {"chat": chat, "handle": handle, "since": int(now), "notify": "important"}
    store.put("links", links)
    seen = store.get("seen")
    seen[user] = {"last": int(now), "ids": []}  # start from now: no flood of old messages
    store.put("seen", seen)
    return user


def user_for_chat(store, chat):
    return next((u for u, r in store.get("links").items() if r.get("chat") == chat), None)


# ---------- what people see ----------

def esc(s):
    return html.escape(str(s or ""), quote=False)


def display(user, people=None):
    rec = (people if people is not None else sbwhere.load()).get(user) or {}
    name = rec.get("name")
    return f"{name} ({user})" if name and name != user else user


def card_text(m, now, people=None):
    """A message as a phone notification: who, how urgent, title, due, and the context sentence."""
    label = LEVEL_LABEL.get(m.get("level"), "")
    head = (f"<b>{label}</b> · " if label else "") + f"from {esc(display(m.get('from'), people))}"
    lines = [head, f"<b>{esc(m.get('title'))}</b>"]
    if m.get("status"):
        lines.append(f"Status: {esc(m['status'])}")
    if m.get("due"):
        lines.append(f"Due {esc(sbmsg.fmt_due(m['due'], now))}")
    ctx = str((m.get("brief") or {}).get("context") or "").strip()
    if ctx:
        lines.append("")
        lines.append(esc(ctx[:400] + ("…" if len(ctx) > 400 else "")))
    if (m.get("count") or 1) > 1:
        lines.append(f"(sent {m['count']} times)")
    return "\n".join(lines)


def card_buttons(m):
    if m.get("reply_to") and m.get("status") in ("done", "declined"):
        return None  # an answer that closes something: nothing to do
    return {"inline_keyboard": [[{"text": "Got it", "callback_data": f"seen:{m['id']}"},
                                 {"text": "Done", "callback_data": f"done:{m['id']}"},
                                 {"text": "Reply", "callback_data": f"reply:{m['id']}"}]]}


def split_away(text):
    """'in class till 3pm' -> ('in class', '3pm'). The time part is only taken if it parses."""
    m = re.match(r"^(.*?)\s+(?:till|til|until|back at|back by|back)\s+(.+)$", text.strip(), re.I)
    if m:
        try:
            if sbmsg.parse_due(m[2]):
                return m[1].strip(), m[2].strip()
        except ValueError:
            pass
    return text.strip(), None


def important(m, now):
    """Worth a phone buzz by default: urgent or high, due within a day, or an answer to something you sent."""
    return (m.get("level") in ("critical", "high") or bool(m.get("reply_to"))
            or bool(m.get("due") and m["due"] - now < 86400))


# ---------- the bot ----------

class Bot:
    """Handles Telegram updates and pushes new messages. `api(method, params)` -> result (raises on failure);
    `members()` -> names who may use the inbox; `deliver(user, req)` -> reply dict (root's handle_msg)."""

    def __init__(self, sb, cfg, store, api, members, deliver, mail_root=None, now=time.time, as_root=True,
                 where_path=None):
        self.sb, self.cfg, self.store, self.api = sb, cfg, store, api
        self.as_root, self.where_path = as_root, where_path
        self.members, self.deliver, self.now = members, deliver, now
        self.mail_root = mail_root or sbmsg.mail_root()

    def send(self, chat, text, markup=None, **kw):
        p = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True, **kw}
        if markup:
            p["reply_markup"] = markup
        return self.api("sendMessage", p)

    # -- incoming --
    def handle(self, upd):
        try:
            if "callback_query" in upd:
                return self.on_button(upd["callback_query"])
            msg = upd.get("message") or {}
            chat = (msg.get("chat") or {}).get("id")
            if chat is None or (msg.get("chat") or {}).get("type") != "private":
                return None  # groups: never (a group chat isn't one person)
            return self.on_text(chat, msg.get("from") or {}, str(msg.get("text") or ""))
        except sbmsg.MsgError as e:
            chat = ((upd.get("message") or {}).get("chat") or {}).get("id") \
                or (((upd.get("callback_query") or {}).get("message") or {}).get("chat") or {}).get("id")
            if chat is not None:
                self.send(chat, esc(str(e)))

    def on_text(self, chat, frm, text):
        now = self.now()
        cmd, _, arg = text.strip().partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        user = user_for_chat(self.store, chat)
        if cmd == "/start":
            if arg and CODE_RE.match(arg):
                handle = "@" + frm["username"] if frm.get("username") else str(frm.get("first_name") or "")
                linked = _redeem(self.store, arg, chat, handle[:40], now)
                if linked:
                    self.sb.log_event("telegram_linked", linked)
                    return self.send(chat, f"Connected to <b>{esc(display(linked))}</b> on this box.\n\n" + HELP)
                return self.send(chat, "That link has expired or was already used. Open your treehouse page and "
                                       "press Connect Telegram again.")
            if user:
                return self.send(chat, f"You're connected as <b>{esc(display(user))}</b>.\n\n" + HELP)
            return self.send(chat, "Hi! To connect, open your treehouse page and press <b>Connect Telegram</b>.")
        if not user:
            return self.send(chat, "This chat isn't connected yet. Open your treehouse page and press "
                                   "<b>Connect Telegram</b>.")
        pend = self.store.get("pending")
        p = pend.pop(str(chat), None)
        if p:
            self.store.put("pending", pend)
        if p and p.get("exp", 0) > now and text and not text.startswith("/"):
            return self.reply(chat, user, p["reply_to"], text)
        if cmd in ("/help", "/start"):
            return self.send(chat, HELP)
        if cmd == "/where":
            return self.send(chat, self.where(user, arg))
        if cmd == "/away":
            if not arg:
                return self.send(chat, "Tell me what to say, e.g. <code>/away in class till 3pm</code>")
            status, until = split_away(arg)
            req = {"action": "presence", "status": status}
            if until:
                req["until"] = sbmsg.parse_due(until, now)
            res = sbwhere.handle(user, req, now=now, p=self.where_path, as_root=self.as_root)
            return self.send(chat, "Done. People who ask see: " + esc(res["message"]))
        if cmd == "/back":
            sbwhere.handle(user, {"action": "presence", "clear": True}, now=now, p=self.where_path, as_root=self.as_root)
            return self.send(chat, "Status cleared.")
        if cmd == "/inbox":
            return self.inbox(chat, user)
        if cmd in ("/quiet", "/loud"):
            set_notify(self.store, user, "important" if cmd == "/quiet" else "all")
            return self.send(chat, "Only urgent messages and ones due within a day from now on." if cmd == "/quiet"
                             else "Every message from now on.")
        if cmd == "/stop":
            unlink(self.store, user)
            self.sb.log_event("telegram_unlinked", user)
            return self.send(chat, "Disconnected. You can connect again from your treehouse page.")
        return self.send(chat, HELP)

    def on_button(self, q):
        chat = ((q.get("message") or {}).get("chat") or {}).get("id")
        data = str(q.get("data") or "")
        user = user_for_chat(self.store, chat)
        act, _, mid = data.partition(":")
        if not user or act not in ("seen", "done", "reply") or not sbmsg.ID_RE.match(mid):
            return self.api("answerCallbackQuery", {"callback_query_id": q.get("id"), "text": "Not available"})
        m = sbmsg._load(os.path.join(self.mail_root, user, f"{mid}.json"))
        if not m or m.get("to") != user:
            return self.api("answerCallbackQuery", {"callback_query_id": q.get("id"), "text": "That message is gone"})
        if act == "seen":
            sbmsg.mark_seen(user, [mid], now=self.now(), root=self.mail_root, as_root=self.as_root)
            return self.api("answerCallbackQuery", {"callback_query_id": q.get("id"),
                                                    "text": f"{display(m.get('from'))} will see you've read it"})
        if act == "done":
            res = self.deliver(user, {"action": "msg", "reply_to": mid, "status": "done"})
            if res.get("ok"):  # cleared in `box msg` and on the dashboard too, not only answered
                sbmsg.mark_done(user, [mid], now=self.now(), root=self.mail_root, as_root=self.as_root)
            return self.api("answerCallbackQuery", {"callback_query_id": q.get("id"),
                                                    "text": "Marked done" if res.get("ok") else res.get("error", "failed")[:190]})
        pend = self.store.get("pending")
        pend[str(chat)] = {"reply_to": mid, "exp": int(self.now() + REPLY_TTL)}
        self.store.put("pending", pend)
        self.api("answerCallbackQuery", {"callback_query_id": q.get("id")})
        return self.send(chat, f"Type your reply to <b>{esc(m.get('title'))}</b>:",
                         {"force_reply": True, "input_field_placeholder": "Your reply"})

    def reply(self, chat, user, mid, text):
        text = text.strip()
        req = {"action": "msg", "reply_to": mid, "title": text[:sbmsg.TITLE_MAX] if len(text) <= sbmsg.TITLE_MAX
               else None, "brief": {"context": text[:sbmsg.TEXT_MAX], "via": "telegram"}}
        res = self.deliver(user, req)
        return self.send(chat, "Sent." if res.get("ok") else esc(res.get("error", "Couldn't send that")))

    def where(self, user, query):
        people = self.members()
        d = sbwhere.load(self.where_path)
        now = self.now()
        if not query:
            return "\n".join(esc(sbwhere.describe(u, d.get(u) or {}, now)) for u in people) or "Nobody here yet."
        hits = sbwhere.find(query, people, d, sbmsg.aliases(self.sb))
        if not hits:
            return f"Nobody called {esc(query[:32])} here."
        return "\n".join(esc(sbwhere.describe(u, d.get(u) or {}, now)) for u in hits[:5])

    def inbox(self, chat, user):
        now = self.now()
        msgs = sorted(sbmsg.list_dir(os.path.join(self.mail_root, user)), key=lambda m: sbmsg.sort_key(m, now))
        done = sbmsg.root_done(user, self.mail_root)  # marked done/archived in box msg, the dashboard or here
        open_ = [m for m in msgs if m["id"] not in done
                 and not (m.get("reply_to") and m.get("status") in ("done", "declined"))][:5]
        if not open_:
            return self.send(chat, "Nothing waiting for you.")
        for m in open_:
            self.send(chat, card_text(m, now), card_buttons(m))

    # -- outgoing --
    def push_new(self):
        """Send every message that arrived since the last pass to its linked chat (respecting /quiet)."""
        now = self.now()
        links, seen = self.store.get("links"), self.store.get("seen")
        changed = False
        for user, link in links.items():
            mark = seen.get(user) or {"last": int(now), "ids": []}
            if link.get("notify") == "off":
                continue
            new = [m for m in sbmsg.list_dir(os.path.join(self.mail_root, user))
                   if m.get("ts", 0) >= mark["last"] - 5 and m["id"] not in mark["ids"] and m.get("from") != user]
            for m in sorted(new, key=lambda m: m.get("ts", 0)):
                mark["ids"] = (mark["ids"] + [m["id"]])[-200:]
                mark["last"] = max(mark["last"], int(m.get("ts", 0)))
                changed = True
                if link.get("notify", "important") == "important" and not important(m, now):
                    continue
                try:
                    self.send(link["chat"], card_text(m, now), card_buttons(m))
                except Exception as e:  # noqa: BLE001 - one blocked chat must not stop the rest
                    print(f"telegram: couldn't reach {user}: {e}")
            seen[user] = mark
        if changed:
            self.store.put("seen", seen)


# ---------- talking to Telegram ----------

def http_api(token, timeout=60):
    def api(method, params):
        req = urllib.request.Request(API.format(token=token, method=method), data=json.dumps(params).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                out = json.load(e)
            except ValueError:
                raise RuntimeError(f"{method}: HTTP {e.code}") from None
        if not out.get("ok"):
            raise RuntimeError(f"{method}: {out.get('description', 'failed')}")
        return out.get("result")
    return api


def read_token(path):
    try:
        with open(path) as f:
            tok = f.read().strip()
    except OSError:
        return None
    return tok if re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{30,64}", tok) else None


def run(bot, api, info_path, poll=10, log=print):
    """Forever: long-poll for updates (handle each), push new messages between polls."""
    offset = None
    try:
        me = api("getMe", {})
        _save(info_path, {"username": me.get("username"), "ok": True, "checked": int(time.time())}, 0o644)
        api("setMyCommands", {"commands": [
            {"command": "inbox", "description": "Your open messages"},
            {"command": "where", "description": "Where someone is"},
            {"command": "away", "description": "Set your status, e.g. in class till 3pm"},
            {"command": "back", "description": "Clear your status"},
            {"command": "help", "description": "What I can do"}]})
    except Exception as e:  # noqa: BLE001
        _save(info_path, {"ok": False, "error": str(e)[:200], "checked": int(time.time())}, 0o644)
        raise
    log(f"telegram: running as @{me.get('username')}")
    while True:
        try:
            ups = api("getUpdates", {"timeout": poll, "offset": offset, "allowed_updates": ["message", "callback_query"]})
            for u in ups or []:
                offset = u["update_id"] + 1
                try:
                    bot.handle(u)
                except Exception as e:  # noqa: BLE001 - one bad update never stops the bot
                    log(f"telegram: update {u.get('update_id')}: {e!r}")
            bot.push_new()
        except Exception as e:  # noqa: BLE001 - network trouble: back off and keep going
            log(f"telegram: {e}")
            time.sleep(15)
