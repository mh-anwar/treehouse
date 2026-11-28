"""Browser QA for the public site: a real Chromium drives the REAL sharedbox-gate and sharedbox-web through TLS.

    uv run --no-project --with playwright python tests/e2e_browser.py [REPO] [--shots DIR] [--quick | --shots-only]

How much to run: a look-only change (layout, CSS, copy) needs just `--shots DIR --shots-only` (every page as
each persona, 1280 and 390 wide, dark and light, plus the page checks below; about a minute). `--quick` clicks
one level deep. The full run (several minutes) is for changes to links, buttons, forms or flows, before a
deploy, in the background.

The fake box itself (temp state, fake root, fake Cloudflare, fake T3s and peers, the real gate and web) is
tests/fakebox.py, shared with tests/demo_site.py. Only the root boundary is faked: sharedbox-web's spool_call (what root's provisioner would answer: status, logins,
servers, pairing, requests, invites) and Cloudflare's token check (fake tokens per persona). Everything between the
browser and that boundary is the code that ships: the gate (asyncio proxy, Host/Access checks, routing, T3 front
door), sharedbox-web's page and form handlers on its unix socket, CSRF tokens, Origin checks, redirects.
A TLS front (self-signed, standing in for Cloudflare) serves https://treehouse.test:PORT, mapped to 127.0.0.1 in
the browser, so cookies, Secure flags, Origin and Sec-Fetch-* headers are the browser's own.

Personas: alice (set up), bob (invited, welcome, no AI login), the owner, a stranger (valid login, no account),
no token, and new@x.io who signs up through an invite link. For each it opens every page, clicks every link and
submits every form it finds in the DOM (a few levels deep), and fails on: unexpected 4xx/5xx, "bad origin",
tracebacks, {placeholders}, None/null/undefined, unprotected emails (Cloudflare would rewrite them), buttons in
links, tailnet-only main actions, pages with no way back to the dashboard, console errors, horizontal overflow
on a phone. Screenshots (1280 and 390 wide, dark and light) go to --shots.

Exit 0 when clean, 1 on failures, 77 when no Chromium is available.
"""
import asyncio
import glob
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer

args = [a for a in sys.argv[1:]]
QUICK = "--quick" in args
SHOTS_ONLY = "--shots-only" in args
SHOTS = None
if "--shots" in args:
    SHOTS = args[args.index("--shots") + 1]
    del args[args.index("--shots"):args.index("--shots") + 2]
args = [a for a in args if not a.startswith("--")]
WT = os.path.abspath(args[0] if args else os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


_s = socket.socket()
_s.bind(("127.0.0.1", 0))
TLS_PORT = _s.getsockname()[1]
_s.close()
PUBLIC = f"https://treehouse.test:{TLS_PORT}"
os.environ.update(FAKEBOX_PUBLIC=PUBLIC, FAKEBOX_REPO=WT, FAKEBOX_TMP_PREFIX="e2e-browser-")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fakebox  # noqa: E402  (builds and starts the fake box: real gate + web, fake root, fake T3s and peers)
from fakebox import (ACCOUNTS, GATE_PORT, INVITE_TOK, OWNER_T3_PORT, PORT_OF, ROOT_LOG, RUNNING, SHARED_BASE, TMP,
                     TS_IP, g, save_registry, sb, w)  # noqa: E402

# ---------------- TLS front (Cloudflare's edge + cloudflared, minus the rewriting) -> the real gate ----------------

subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=treehouse.test",
                "-keyout", os.path.join(TMP, "k.pem"), "-out", os.path.join(TMP, "c.pem")], check=True,
               capture_output=True)
SSL = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
SSL.load_cert_chain(os.path.join(TMP, "c.pem"), os.path.join(TMP, "k.pem"))


async def tls_front(reader, writer):
    try:
        br, bw = await asyncio.open_connection("127.0.0.1", GATE_PORT)
    except OSError:
        writer.close()
        return

    async def pipe(r, wr):
        try:
            while data := await r.read(65536):
                wr.write(data)
                await wr.drain()
        except (ConnectionError, ssl.SSLError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                wr.close()
            except (RuntimeError, ssl.SSLError):
                pass
    await asyncio.gather(pipe(reader, bw), pipe(br, writer))


fakebox.on_loop(asyncio.start_server(tls_front, "127.0.0.1", TLS_PORT, ssl=SSL))

# ---------------- the browser ----------------

FAILS, NOTES = [], []
VISITED = set()


def fail(persona, where, what):
    FAILS.append(f"[{persona}] {where}: {what}")
    print(f"FAIL [{persona}] {where}: {what}", flush=True)


def chromium():
    if os.environ.get("CHROMIUM"):
        return os.environ["CHROMIUM"]
    cands = sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome")),
                   key=lambda p: int(re.search(r"chromium-(\d+)", p).group(1)))
    return cands[-1] if cands else None


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
BAD_TEXT = [(re.compile(r"bad origin", re.I), "bad origin"), (re.compile(r"Traceback|Exception|Error:"), "python error"),
            (re.compile(r"\{[a-z_]+\}"), "{placeholder}"),
            (re.compile(r"\b(None|null|undefined|NaN)\b(?! yet)"), "None/null/undefined"),
            (re.compile(r"\[email\s*protected\]"), "Cloudflare email rewrite"),
            (re.compile(r"REQUEST_DENY|OWNER_T3_USER|ADMIN_USERS|sharedbox\.conf|\bsudo\b"), "config jargon")]


def email_outside_email_off(body):
    """Cloudflare's Email Obfuscation rewrites any email address in HTML outside <!--email_off--> blocks."""
    stripped = re.sub(r"<!--email_off-->.*?<!--/email_off-->", "", body, flags=re.S)
    stripped = re.sub(r"<script.*?</script>", "", stripped, flags=re.S)
    return EMAIL.findall(stripped) and [m.group(0) for m in EMAIL.finditer(stripped)]


class Session:
    """One persona in one browser context."""

    def __init__(self, browser, persona, token, cookie=False, width=1280, scheme="dark", js=True):
        self.persona, self.token = persona, token
        extra = {"Cf-Access-Jwt-Assertion": token} if token and not cookie else {}
        self.ctx = browser.new_context(ignore_https_errors=True, extra_http_headers=extra, java_script_enabled=js,
                                       viewport={"width": width, "height": 900}, color_scheme=scheme)
        if token and cookie:
            self.ctx.add_cookies([{"name": "CF_Authorization", "value": token, "domain": "treehouse.test",
                                   "path": "/", "secure": True, "httpOnly": True}])
        self.page = self.ctx.new_page()
        self.console, self.bad_responses, self.expect_codes = [], [], set()
        self.ctx.on("page", self._watch)
        self._watch(self.page)

    def _watch(self, page):
        # network failures are judged by status in _resp (some 403/404s are intended), not by the console line
        page.on("console", lambda m: m.type == "error" and not m.text.startswith("Failed to load resource")
                and self.console.append(m.text))
        page.on("pageerror", lambda e: self.console.append(f"pageerror: {e}"))
        page.on("response", self._resp)
        self.dialogs = getattr(self, "dialogs", [])
        page.on("dialog", lambda d: (self.dialogs.append(d.message), d.accept()))

    def _resp(self, r):
        u = urllib.parse.urlsplit(r.url)
        if u.hostname != "treehouse.test":
            return
        if r.status >= 400 and r.status not in self.expect_codes:
            self.bad_responses.append(f"{r.status} {r.request.method} {u.path}")
        ct = (r.headers.get("content-type") or "")
        if "text/html" in ct and r.status != 304:
            try:
                body = r.text()
            except Exception:  # noqa: BLE001 - body gone after navigation
                return
            leaks = email_outside_email_off(body)
            if leaks:
                self.bad_responses.append(f"email outside email_off on {u.path}: {sorted(set(leaks))}")

    def drain(self, where):
        for c in self.console:
            fail(self.persona, where, f"console error: {c}")
        for b in self.bad_responses:
            fail(self.persona, where, b)
        self.console.clear()
        self.bad_responses.clear()

    def close(self):
        self.ctx.close()


def url(path):
    return PUBLIC + path


AGENT_TEXT_PAGES = ("/_box/connect.md", "/_box/api/whoami")


def inspect(s, where, page=None, dashboard_link=True):
    """Content checks on whatever page is showing."""
    page = page or s.page
    try:
        page.wait_for_load_state("load", timeout=15000)
        text = page.inner_text("body", timeout=5000)
    except Exception as ex:  # noqa: BLE001
        fail(s.persona, where, f"page didn't load: {ex}")
        return
    if urllib.parse.urlsplit(page.url).path in AGENT_TEXT_PAGES:
        return  # plain-text instructions for an agent: box rules (sudo etc.) belong there, no nav needed
    for rx, what in BAD_TEXT:
        m = rx.search(text)
        if m:
            ctx = text[max(0, m.start() - 60):m.end() + 60].replace("\n", " ")
            fail(s.persona, where, f"{what}: ...{ctx}...")
    host = urllib.parse.urlsplit(page.url).hostname
    if host != "treehouse.test":
        return
    if page.locator("a button, button a, a a, button button").count():
        fail(s.persona, where, "a button inside a link (dead in some browsers)")
    for el in page.locator("a.btn, button").all():
        href = el.get_attribute("href") or ""
        if href.startswith("http") and urllib.parse.urlsplit(href).hostname not in ("treehouse.test", "t3.codes") \
                and not href.startswith("https://"):
            fail(s.persona, where, f"main action points at a tailnet address: {href}")
    if dashboard_link and not page.locator("a[href='/_box/me'], a[href$='/_box/me']").count():
        fail(s.persona, where, "no way back to the dashboard")
    if page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"):
        fail(s.persona, where, f"horizontal overflow at {page.viewport_size['width']}px")
    tabs = page.locator("nav.tabs")
    if tabs.count():  # the dashboard's tab bar: links, exactly one marked as the current page
        cur = tabs.first.locator("a[aria-current='page']")
        if cur.count() != 1:
            fail(s.persona, where, f"tab bar has {cur.count()} current tabs")
        elif "tab=" in page.url and f"tab={cur.get_attribute('data-tab')}" not in page.url \
                and "tab=admin" not in page.url and "tab=boxes" not in page.url:  # old links: aliases
            fail(s.persona, where, f"tab bar marks '{cur.get_attribute('data-tab')}' on {page.url}")
    VISITED.add((s.persona, urllib.parse.urlsplit(page.url).path))


def links_on(page):
    out = []
    for i, a in enumerate(page.locator("a[href]").all()):
        href = a.get_attribute("href")
        out.append((i, href, (a.inner_text() or "").strip()[:40], a.get_attribute("target")))
    return out


def form_sigs(page):
    """A stable description of each form on the page: (index, action, fields, button label)."""
    out = []
    for i, f in enumerate(page.locator("form").all()):
        vals = {}
        for inp in f.locator("input[type=hidden]").all():
            n = inp.get_attribute("name")
            if n != "token":
                vals[n] = inp.get_attribute("value")
        btn = f.locator("button").first
        label = (btn.inner_text() if btn.count() else "").strip()
        out.append((i, f.get_attribute("action"), tuple(sorted(vals.items())), label))
    return out


def press(page, target, timeout=30):
    """Click a form's button and wait for its result: in place (the page's script swaps the card and counts
    window.sbDone up) or, without the script, a page load."""
    before = page.evaluate("window.sbDone || 0")
    page.evaluate("window.__stay = 1")
    target.click()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.evaluate(f"!window.__stay || (window.sbDone || 0) > {before}"):
                break
        except Exception:  # noqa: BLE001 - mid-navigation
            pass
        page.wait_for_timeout(40)
    else:
        raise TimeoutError("no result after pressing the button")
    page.wait_for_load_state("load")


def stayed(page):
    """True when the last press() didn't load a new page (the result came in place)."""
    return page.evaluate("window.__stay === 1")


SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBfW8kyhn1bfC1CK0D4Z0N8F0rG5uRoHxdQhqC1Vd8Df qa@laptop"


def submit(page, sig):
    """Fill and submit the form matching sig on the current page. Returns False if it isn't there."""
    for i, action, vals, label in form_sigs(page):
        if (action, vals, label) == sig[1:]:
            f = page.locator("form").nth(i)
            page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")  # a person opens it
            for inp in f.locator("input:not([type=hidden]), textarea").all():
                n = inp.get_attribute("name") or ""
                if inp.get_attribute("readonly") is not None or inp.get_attribute("type") in ("checkbox", "radio"):
                    continue
                inp.fill({"sshkey": SSH_KEY, "code": "sk-paste-code-123", "username": "carol",
                          "email": "sam@example.com", "note": "Sam", "hours": "2",
                          "until": time.strftime("%Y-%m-%dT%H:%M", time.localtime(time.time() + 7200))}.get(n, "x"))
            press(page, f.locator("button").first)
            return True
    return False


def same_site(href, base):
    full = urllib.parse.urljoin(base, href)
    u = urllib.parse.urlsplit(full)
    return full if u.hostname == "treehouse.test" else None


def crawl(browser, persona, token, start="/_box/me", depth=3, cookie=False, expect=(), dashboard_link=True):
    """Every link and form reachable from `start`, by clicking them (real same-origin navigations)."""
    s = Session(browser, persona, token, cookie=cookie)
    s.expect_codes = set(expect)
    page = s.page
    queue, seen_pages, seen_forms, seen_links = [(start, ())], set(), set(), set()
    tabs = []
    if start.startswith("/_box/me"):  # every tab of the dashboard is its own page: crawl each one
        page.goto(url(start))
        tabs = [urllib.parse.urlsplit(a.get_attribute("href")) for a in page.locator("nav.tabs a").all()]
        queue += [(t.path + "?" + t.query, ()) for t in tabs]
    while queue:
        path, steps = queue.pop(0)
        key = (path, steps)
        if key in seen_pages or len(steps) > depth:
            continue
        seen_pages.add(key)

        def arrive():
            page.goto(url(path))
            for st in steps:
                if not submit(page, st):
                    return False
            return True
        if not arrive():
            continue
        where = path + "".join(f" > [{st[3]}]" for st in steps)
        print(f"  .. {persona}: {where}", flush=True)
        inspect(s, where, dashboard_link=dashboard_link)
        s.drain(where)
        base = page.url
        for i, href, text, target in links_on(page):
            full = same_site(href, base)
            if not full:
                continue
            p = urllib.parse.urlsplit(full)
            if p.path.startswith("/cdn-cgi/"):
                continue  # Cloudflare's own logout; checked separately
            if (full, target) in seen_links:
                continue  # same link elsewhere: same destination
            seen_links.add((full, target))
            if not arrive():
                break
            lw = f"{where} -> link '{text}' ({href})"
            page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")  # a person opens it
            a = page.locator("a[href]").nth(i)
            try:
                if target == "_blank":
                    with s.ctx.expect_page(timeout=15000) as pi:
                        a.click()
                    np = pi.value
                    np.wait_for_load_state("load")
                    inspect(s, lw, np, dashboard_link=False)
                    np.close()
                else:
                    a.click()
                    page.wait_for_load_state("load")
                    inspect(s, lw, dashboard_link=False)
            except Exception as ex:  # noqa: BLE001
                fail(persona, lw, f"click failed: {str(ex)[:200]}")
            s.drain(lw)
            dest = urllib.parse.urlsplit(page.url)
            if dest.hostname == "treehouse.test" and dest.path.startswith("/_box") and not steps:
                queue.append((dest.path + ("?" + dest.query if dest.query else ""), ()))
        if not arrive():
            continue
        for sig in form_sigs(page):
            fsig = sig[1:]  # the same form on another result page does the same thing: submit each once
            if fsig in seen_forms or (sig[1] or "").endswith("/logout"):
                continue
            seen_forms.add(fsig)
            queue.append((path, steps + (sig,)))
        w.portal_rate.clear()  # the per-person form rate limit (30 / 10 min): the crawl is far busier than a person
        w.rate.clear()
    for t in tabs:
        if (t.path + "?" + t.query, ()) not in seen_pages:
            fail(persona, "crawl", f"tab {t.query} never visited")
    s.close()
    return seen_pages


def flow_open_t3(browser, persona, token, t3_port, cookie=False):
    """Dashboard -> "Open my T3 Code" -> paired into the right T3; second time straight in; bookmark -> dashboard."""
    s = Session(browser, persona, token, cookie=cookie)
    page = s.page
    page.goto(url("/"))
    if urllib.parse.urlsplit(page.url).path != "/_box/me":
        fail(persona, "open /", f"typed URL should land on the dashboard, got {page.url}")
    # the start card's link when it has one (not before an AI login), else the header's "T3 in browser"
    btn = page.locator("#start a[href='/_box/app']")
    if not btn.count():
        btn = page.get_by_role("link", name=re.compile("Open my T3 Code|Open T3|T3 in browser"))
    if not btn.count():
        fail(persona, "dashboard", "no 'Open my T3 Code' button")
        s.close()
        return
    btn.first.click()
    try:
        page.wait_for_function("document.title==='Fake T3 Code'", timeout=15000)
    except Exception:  # noqa: BLE001
        fail(persona, "Open my T3 Code", f"didn't reach T3: {page.url} '{page.title()}' "
             f"{page.inner_text('body')[:200]!r}")
    else:
        if f"T3 on {t3_port}" not in page.inner_text("body"):
            fail(persona, "Open my T3 Code", f"reached the wrong T3: {page.inner_text('body')[:80]}")
    page.goto(url("/_box/me"))
    page.get_by_role("link", name=re.compile("Open my T3 Code|Open T3|T3 in browser")).first.click()
    page.wait_for_load_state("load")
    if page.title() != "Fake T3 Code":
        fail(persona, "Open my T3 Code (already paired)", f"got {page.url} '{page.title()}'")
    s.expect_codes.add(404)
    page.goto(url("/draft/abc"))  # a reload inside T3 stays in T3
    if "not found" not in page.content():
        fail(persona, "reload inside T3", f"should be T3's own answer, got {page.url}")
    page.goto(url("/"))  # bookmark: the dashboard first
    if urllib.parse.urlsplit(page.url).path != "/_box/me":
        fail(persona, "bookmark after pairing", f"expected dashboard, got {page.url}")
    s.drain("open T3 flow")
    s.close()


def flow_login(browser):
    """bob: welcome page, then log in to Claude (paste flow) and GitHub (device code + polling)."""
    s = Session(browser, "bob", "tok-bob")
    page = s.page
    page.goto(url("/"))
    if "welcome=1" not in page.url or "Welcome" not in page.inner_text("body"):
        fail("bob", "first visit", f"expected the welcome page, got {page.url}")
    inspect(s, "welcome")
    press(page, page.get_by_role("button", name="Log in with Claude"))
    inspect(s, "claude login card")
    if not page.locator("#start a.btn[href^='https://claude.ai']").count():
        fail("bob", "claude login", "no 'Open the login page' link in the start card")
    page.fill("input[name=code]", "sk-paste-code-123")
    press(page, page.get_by_role("button", name="Finish"))
    inspect(s, "claude code pasted")
    if "Logged in" not in page.locator("#start").inner_text():
        fail("bob", "claude login", "no 'Logged in' confirmation in the start card")
    if not stayed(page):
        fail("bob", "claude login", "the page reloaded instead of showing the result in place")
    page.goto(url("/_box/me?tab=account"))
    gh = page.get_by_role("button", name="Connect GitHub")
    if not gh.count():
        fail("bob", "account tab", "no Connect GitHub")
    else:
        press(page, gh)
        inspect(s, "github device code")
        if "ABCD-1234" not in page.locator("#account").inner_text():
            fail("bob", "github device code", "the code isn't shown in the account card")
        page.wait_for_timeout(4500)  # the page polls once by itself (its script ran after the swap)
        press(page, page.get_by_role("button", name="Check now"))
        inspect(s, "github check now")
        if "hasn't finished" not in page.locator("#account").inner_text():
            fail("bob", "github check now", "Check now's answer isn't in the account card")
    s.drain("login flow")
    s.close()


def flow_invite(browser):
    s = Session(browser, "new@x.io", "tok-new")
    page = s.page
    page.goto(url("/"))
    inspect(s, "stranger landing before signup")
    page.goto(url(f"/i/{INVITE_TOK}"))
    inspect(s, "invite page")
    page.fill("input[name=username]", "carol")
    page.get_by_role("button", name="Create my account").click()
    page.wait_for_load_state("load")
    if "welcome=1" not in page.url:
        fail("new@x.io", "signup", f"expected the welcome page, got {page.url}: {page.inner_text('body')[:200]!r}")
    inspect(s, "after signup")
    s.drain("invite flow")
    s.close()
    flow_open_t3(browser, "carol", "tok-new", PORT_OF["carol"])


def flow_apps(browser):
    s = Session(browser, "alice", "tok-alice")
    page = s.page
    for path, want in ((f"/p/{PORT_OF['alice'] + 3}/", f"App {PORT_OF['alice'] + 3}"), ("/s/web/", f"App {SHARED_BASE}")):
        page.goto(url(path))
        if page.title() != want:
            fail("alice", path, f"expected {want}, got '{page.title()}'")
    s.expect_codes |= {502, 403}
    for path in (f"/p/{PORT_OF['alice'] + 4}/", "/s/down/", f"/p/{PORT_OF['bob'] + 3}/"):
        page.goto(url(path))
        inspect(s, path)
    s.drain("apps")
    s.close()


def flow_no_token(browser):
    s = Session(browser, "no token", None)
    s.expect_codes = {403}
    for path in ("/", "/_box/me", "/_box/features", f"/i/{INVITE_TOK}"):
        r = s.page.goto(url(path))
        if r.status != 403:
            fail("no token", path, f"expected 403, got {r.status}")
        inspect(s, path, dashboard_link=False)
    s.drain("no token")
    s.close()


def logout_hrefs(browser):
    for persona, tok in (("alice", "tok-alice"), ("owner", "tok-owner"), ("stranger", "tok-stranger")):
        s = Session(browser, persona, tok)
        s.page.goto(url("/_box/me"))
        hrefs = [a.get_attribute("href") for a in s.page.locator("a").all()
                 if re.search(r"log ?out|sign out", (a.inner_text() or ""), re.I)]
        if not hrefs or any(h != "/cdn-cgi/access/logout" for h in hrefs):
            fail(persona, "log out", f"log out links: {hrefs}")
        s.drain("logout")
        s.close()


def owner_requests(browser):
    s = Session(browser, "owner", "tok-owner")
    page = s.page
    page.goto(url("/_box/me"))
    for label in ("Approve", "Mark handled", "Decline"):
        page.goto(url("/_box/me?tab=requests"))
        b = page.locator("#requests button", has_text=label)
        if not b.count():
            continue
        press(page, b.first)
        inspect(s, f"request {label}")
        if not page.locator("#requests .note.ok").count():
            fail("owner", f"request {label}", page.inner_text("body")[:200])
    s.drain("owner requests")
    s.close()


FRIEND_TAB_PAGES = ["/_box/me", "/_box/me?tab=messages", "/_box/me?tab=servers", "/_box/me?tab=machines",
                    "/_box/me?tab=account"]
SHOT_PAGES = {
    "alice": FRIEND_TAB_PAGES + ["/_box/me?tab=account&range=7d", "/_box/features"],
    "bob": ["/_box/me?welcome=1", "/_box/me"],
    "dave": ["/_box/me", "/_box/me?tab=account"],
    "erin": ["/_box/me"],
    "owner": FRIEND_TAB_PAGES + [f"/_box/me?tab={t}" for t in ("machines", "people", "running", "requests", "settings",
                                                                "events")],
    "stranger": ["/", f"/i/{INVITE_TOK}", "/_box/nope"],
}
SHOT_TOK = {"alice": "tok-alice", "bob": "tok-bob", "owner": "tok-owner", "stranger": "tok-stranger", "dave": "tok-dave",
            "erin": "tok-erin"}


def screenshots(browser):
    os.makedirs(SHOTS, exist_ok=True)
    for persona, paths in SHOT_PAGES.items():
        for width in (1280, 390):
            for scheme in ("dark", "light"):
                s = Session(browser, persona, SHOT_TOK[persona], width=width, scheme=scheme)
                s.expect_codes = {404}
                for p in paths:
                    s.page.goto(url(p))
                    s.page.wait_for_load_state("load")
                    inspect(s, f"{p} @{width} {scheme}")
                    name = re.sub(r"[^a-z0-9]+", "-", p.lower()).strip("-") or "root"
                    s.page.screenshot(path=os.path.join(SHOTS, f"{persona}-{name}-{width}-{scheme}.png"), full_page=True)
                # result cards
                if persona in ("alice", "owner"):
                    s.page.goto(url("/_box/me?tab=servers"))
                    press(s.page, s.page.locator("button", has_text="Show recent output").first)
                    s.page.screenshot(path=os.path.join(SHOTS, f"{persona}-logs-{width}-{scheme}.png"), full_page=True)
                if persona == "alice":
                    s.page.goto(url("/_box/me"))
                    b = s.page.locator("#start button:visible", has_text="Get setup instructions")
                    if not b.count():
                        fail("alice", f"screenshots {width} {scheme}", "no 'Get setup instructions' button")
                    else:
                        press(s.page, b.first)
                        inspect(s, f"setup prompt @{width} {scheme}")
                        s.page.screenshot(path=os.path.join(SHOTS, f"alice-app-prompt-{width}-{scheme}.png"), full_page=True)
                        s.page.locator("#start summary", has_text="Do it by hand").click()
                        s.page.screenshot(path=os.path.join(SHOTS, f"alice-app-byhand-{width}-{scheme}.png"), full_page=True)
                        ACCOUNTS["alice"]["app"] = True  # as if the app on her laptop connected: "Open T3" leads
                        try:
                            s.page.goto(url("/_box/me"))
                            s.page.wait_for_load_state("load")
                            s.page.screenshot(path=os.path.join(SHOTS, f"alice-me-app-set-{width}-{scheme}.png"), full_page=True)
                        finally:
                            ACCOUNTS["alice"]["app"] = False
                s.drain(f"screenshots {width} {scheme}")
                s.close()
                w.portal_rate.clear()


MAIN_JS = """() => [...document.querySelectorAll('main a.btn, main button')].filter(el =>
  !el.matches('.secondary, .sm, .linkbtn') && el.offsetParent !== null && !el.closest('details:not([open])'))
  .map(el => el.textContent.trim())"""
HOSTNAME = socket.gethostname()


def no_hostname_addresses(s, where, page=None):
    """Friends who joined the tailnet as shared-in users can't resolve the box's short name: no address shows it."""
    page = page or s.page
    vals = page.evaluate("""() => [...document.querySelectorAll('code, pre, textarea, input')]
        .map(el => el.matches('textarea, input') ? el.value : el.textContent)""")
    hrefs = page.evaluate("() => [...document.querySelectorAll('a[href]')].map(a => a.getAttribute('href'))")
    for v in vals + hrefs:
        if re.search(rf"(//|@){re.escape(HOSTNAME)}\b", v or ""):
            fail(s.persona, where, f"address uses the box's name, not its Tailscale IP: {v[:120]!r}")


def start_cards(browser):
    """The start card: exactly one main button per state (S0..S3), and the T3 app setup prompt."""
    cases = [("erin", "tok-erin", [], "T3 isn't set up on this machine yet"),
             ("bob", "tok-bob", ["Log in with Claude"], "Log in with ChatGPT"),
             ("alice", "tok-alice", ["Open T3 in this browser"], "Already on Tailscale?"),
             ("dave", "tok-dave", ["Open T3 in this browser"], "Get setup instructions"),  # no Tailscale id on record
             ("alice", "tok-alice", ["Open T3 in this browser"], "Open T3 app", 390)]
    for persona, tok, want, text, *width in cases:
        s = Session(browser, persona, tok, **({"width": width[0]} if width else {}))
        s.page.goto(url("/_box/me"))
        where = f"start card ({persona})"
        inspect(s, where)
        got = s.page.evaluate(MAIN_JS)
        if got != want:
            fail(persona, where, f"main buttons {got}, expected {want}")
        card = s.page.inner_text("#start") if s.page.locator("#start").count() else ""
        if text not in card:
            fail(persona, where, f"start card lacks {text!r}: {card[:200]!r}")
        if "Ask the owner to add you to Tailscale" in card:
            fail(persona, where, "tells a friend who may already be on Tailscale to ask for it")
        no_hostname_addresses(s, where)
        if s.page.locator("text=Link my T3 account").count():
            fail(persona, where, "T3 Connect is shown")
        s.drain(where)
        s.close()
    # S2 -> the prompt -> S3 once the app has really connected (a T3 session labelled for it)
    s = Session(browser, "alice (prompt)", "tok-alice")
    page = s.page
    page.goto(url("/_box/me"))
    press(page, page.locator("#start button:visible", has_text="Get setup instructions"))
    inspect(s, "setup prompt")
    prompt = page.locator("#app-prompt").input_value() if page.locator("#app-prompt").count() else ""
    pair = page.locator("#start input.mono").input_value() if page.locator("#start input.mono").count() else ""
    port = PORT_OF["alice"]
    if f"t3code://pair?pairingUrl=http%3A%2F%2F{TS_IP}%3A{port}%2Fpair%23token%3D" not in prompt:
        fail("alice", "setup prompt", f"no t3code:// link built from the IP: {prompt[:300]!r}")
    if not pair.startswith(f"http://{TS_IP}:{port}/pair#token=") or pair not in prompt:
        fail("alice", "setup prompt", f"pairing URL {pair!r} not in the prompt / not on the IP")
    for must in (f"Add the backend at http://{TS_IP}:{port}?", f"{PUBLIC}/_box/me", "https://t3.codes",
                 "Settings, Environments, Add environment, pick Remote link", "Start-Process",
                 "If it still times out, stop and tell me",
                 f"curl -s --noproxy '*' --max-time 15 http://{TS_IP}:{port}/.well-known/t3/environment",
                 "0. Already working?", "ask me what the app showed"):
        if must not in prompt:
            fail("alice", "setup prompt", f"missing {must!r}")
    if re.search(r"\{[^}]*\}", prompt):
        fail("alice", "setup prompt", f"leftover placeholder: {re.search(r'[{][^}]*[}]', prompt).group(0)}")
    link = page.locator("#start a[href^='t3code://']")
    if not link.count() or "btn" in (link.first.get_attribute("class") or ""):
        fail("alice", "setup prompt", "no plain t3code:// link")
    if page.evaluate(MAIN_JS) != ["Copy prompt"]:
        fail("alice", "setup prompt", f"main buttons {page.evaluate(MAIN_JS)}")
    page.get_by_role("button", name="Copy prompt").click()
    try:
        page.wait_for_function("[...document.querySelectorAll('#start button')].some(b => b.textContent === 'Copied')",
                               timeout=3000)
    except Exception:  # noqa: BLE001
        fail("alice", "setup prompt", "Copy prompt didn't say Copied")
    if re.search(r"\b\d{1,2}:\d{2}\b", page.inner_text("#start")):
        fail("alice", "setup prompt", "shows a clock time")
    no_hostname_addresses(s, "setup prompt")
    if page.locator("#start details.byhand[open]").count():
        fail("alice", "setup prompt", "the do-it-by-hand steps are open: one obvious next step only")
    if "Paste it into Claude Code, Codex or any agent on your laptop" not in page.inner_text("#start"):
        fail("alice", "setup prompt", "no next step after Copy prompt")
    page.locator("#start summary", has_text="Do it by hand").click()
    page.locator("#start button", has_text="Copy").last.click()
    try:
        page.wait_for_function("[...document.querySelectorAll('#start details button')].some(b => b.textContent === 'Copied')",
                               timeout=3000)
    except Exception:  # noqa: BLE001
        fail("alice", "setup prompt", "the pairing link's Copy didn't say Copied")
    if any(c["name"] == "tb_app" for c in s.ctx.cookies()):
        fail("alice", "setup prompt", "sets the old tb_app cookie")
    page.goto(url("/_box/me"))  # only made a prompt, the app never connected: still the setup prompt
    if page.evaluate(MAIN_JS) != ["Open T3 in this browser"]:
        fail("alice", "after the prompt, app not connected", f"main buttons {page.evaluate(MAIN_JS)}")
    ACCOUNTS["alice"]["app"] = True
    try:
        page.goto(url("/_box/me"))
        if page.evaluate(MAIN_JS) != ["Open T3 in this browser"]:
            fail("alice", "app connected (S3)", f"main buttons {page.evaluate(MAIN_JS)}")
        if not page.locator("#start button.linkbtn", has_text="Get new setup instructions").count():
            fail("alice", "app connected (S3)", "no 'Get new setup instructions'")
        press(page, page.locator("#start button", has_text="Get new setup instructions"))
        if not page.locator("#app-prompt").count():
            fail("alice", "S3 new prompt", "no prompt")
    finally:
        ACCOUNTS["alice"]["app"] = False
    s.drain("setup prompt")
    s.close()


def boxes_card(browser):
    """The Machines tab (everyone): one card per machine (this one, a live one with full numbers, an offline one).
    The owner sees all of BOXES with links; alice sees the machines she has an account on, without links to the
    owner's T3. Old ?tab=boxes links land on Machines."""
    for width, scheme in ((1280, "dark"), (390, "light")):
        s = Session(browser, "owner", "tok-owner", width=width, scheme=scheme)
        s.page.goto(url("/_box/me?tab=boxes"))
        where = f"machines tab @{width}"
        if s.page.locator("nav.tabs a.on").inner_text().strip() != "Machines":
            fail("owner", where, "?tab=boxes didn't land on Machines")
        if SHOTS and s.page.locator("#machines").count():
            os.makedirs(SHOTS, exist_ok=True)
            s.page.screenshot(path=os.path.join(SHOTS, f"owner-machines-{width}-{scheme}.png"), full_page=True)
        inspect(s, where)
        cards = s.page.locator("#machines .mach")
        texts = [cards.nth(i).inner_text() for i in range(cards.count())]
        if len(texts) != 3:
            fail("owner", where, f"expected 3 machines, got {texts}")
        else:
            this, peer, gone = texts
            for row, musts in ((this, ["treebox", "Online", "this page comes from it"]),
                               (peer, ["minastirith", "Online", "23% busy", "13.0G", "of 31.0G", "86.0G", "1.2",
                                       "8 cores", "Alice", "dave", "abc1234", "Open its dashboard"]),
                               (gone, ["krishbox", "Offline", "doesn't answer"])):
                for m in musts:
                    if m not in row:
                        fail("owner", where, f"card lacks {m!r}: {row!r}")
        if not s.page.locator("#machines a[href='http://100.64.0.6:8090/']").count():
            fail("owner", where, "no link to the other machine's dashboard")
        s.drain(where)
        s.close()
    for width in (1280, 390):
        s = Session(browser, "alice", "tok-alice", width=width, scheme="light")
        s.page.goto(url("/_box/me?tab=machines"))
        where = f"machines tab (alice) @{width}"
        if SHOTS:
            s.page.screenshot(path=os.path.join(SHOTS, f"alice-machines-{width}-light.png"), full_page=True)
        inspect(s, where)
        body = s.page.locator("#machines").inner_text() if s.page.locator("#machines").count() else ""
        for m in ("treebox", "minastirith", "krishbox", "Memory", "Active now"):
            if m not in body:
                fail("alice", where, f"lacks {m!r}")
        if s.page.locator("#machines a[href*='3773']").count() or "treehouse version" in body:
            fail("alice", where, "a friend sees the owner's links or versions")
        # her per-machine AI accounts: a different Claude account on minastirith, and Sign in there
        s.page.goto(url("/_box/me?tab=account"))
        acct = s.page.locator("#accounts").inner_text() if s.page.locator("#accounts").count() else ""
        for m in ("alice@example.com", "alice.work@example.com", "This machine's own account", "Not connected yet"):
            if m not in acct:
                fail("alice", where, f"AI accounts table lacks {m!r}")
        if width == 1280:
            row = s.page.locator("#accounts tr", has_text="minastirith")
            press(s.page, row.locator("button", has_text="Sign in").last)
            out = s.page.locator("#accounts [data-out]").inner_text()
            if "Sign in to" not in out or "minastirith" not in out or not stayed(s.page):
                fail("alice", where, f"Sign in on minastirith didn't show its steps in place: {out!r}")
        s.drain(where)
        s.close()


def running_card(browser):
    """The Running card (alice: rows, the forgotten-database flag, Stop with its result in the card), the owner's
    Running tab, and group-server descriptions (escaped; a missing one asks for it)."""
    import copy
    saved = copy.deepcopy(RUNNING)
    s = Session(browser, "alice", "tok-alice")
    s.page.goto(url("/_box/me?tab=servers"))
    where = "running card"
    inspect(s, where)
    card = s.page.locator("#running section")
    text = card.inner_text() if card.count() else ""
    for must in ("shop-db", "Supabase · 5 containers", "1.4G", "running 3d 2h", "Claude session", "blog",
                 "Show 1 more", "always get", "Your agent can list"):
        if must not in text:
            fail("alice", where, f"card lacks {must!r}")
    if card.locator("li").first.locator(".pill.warn").count() != 1:
        fail("alice", where, "a database up 3 days with 1.4G isn't flagged")
    stops = card.locator("button", has_text="Stop")
    if stops.count() != 4:  # shop-db, blog, mongo, tmux (+0 for the agent and the background line)
        fail("alice", where, f"expected 4 Stop buttons, got {stops.count()}")
    press(s.page, stops.first)
    if "Stopped shop-db" not in s.page.locator("#running [data-out]").inner_text():
        fail("alice", where, "no 'Stopped shop-db' notice in the card")
    if "shop-db" in s.page.locator("#running ul.run").first.inner_text():
        fail("alice", where, "shop-db still listed after Stop")
    if not stayed(s.page):
        fail("alice", where, f"Stop reloaded the page instead of answering in the card: {s.page.url}")
    grp = s.page.locator("#shared .srv .desc")
    if not grp.count() or "every Monday" not in grp.first.inner_text() or grp.first.locator("b").count():
        fail("alice", where, "group server description missing or not escaped")
    if "No description yet" not in s.page.inner_text("body"):
        fail("alice", where, "a group server without a description doesn't ask for one")
    if s.page.locator("button", has_text="boost").count() or "Emergency memory boost" in s.page.inner_text("body"):
        fail("alice", where, "the boost button is still there")
    s.drain(where)
    s.close()
    RUNNING.clear()
    RUNNING.update(copy.deepcopy(saved))  # alice stopped shop-db above; the owner's tab starts from the full list
    s = Session(browser, "owner", "tok-owner")
    s.page.goto(url("/_box/me?tab=running"))
    where = "owner running tab"
    inspect(s, where)
    t = s.page.locator(".runall").inner_text() if s.page.locator(".runall").count() else ""
    for must in ("alice", "bob", "uoft-index", "Nothing running", "of memory in use by 3 people", "Keep running",
                 "No description yet. Started with"):
        if must not in t:
            fail("owner", where, f"tab lacks {must!r}")
    if not s.page.locator(".runall button", has_text="Stop").count():
        fail("owner", where, "no Stop buttons on All running")
    # someone else's database: the confirmation names whose it is, the result shows in the card, no jump
    s.page.evaluate("window.scrollTo(0, 400)")
    s.dialogs.clear()
    row = s.page.locator(".runall li", has_text="shop-db")  # alice's (the owner's copy is called uoft-index)
    press(s.page, row.locator("button", has_text="Stop"))
    if not s.dialogs or "This is alice's" not in s.dialogs[0]:
        fail("owner", where, f"Stop on alice's database didn't say whose it is: {s.dialogs}")
    out = s.page.locator("#running-all [data-out]").inner_text()
    if "Alice: Stopped shop-db" not in out or not stayed(s.page):
        fail("owner", where, f"no in-place result for stopping alice's database: {out!r}")
    # T3 Code: says chats disconnect; keep-running: asked twice, sent with force
    s.dialogs.clear()
    press(s.page, s.page.locator(".runall li", has_text="T3 Code").filter(has=s.page.locator("button")).last.locator("button"))
    if not any("open chats will disconnect" in d for d in s.dialogs):
        fail("owner", where, f"stopping a T3 Code didn't warn about chats: {s.dialogs}")
    s.dialogs.clear()
    keep = s.page.locator(".runall li", has_text="Keep running").first
    press(s.page, keep.locator("button", has_text="Stop anyway"))
    if len(s.dialogs) != 2 or "keep running" not in s.dialogs[1]:
        fail("owner", where, f"a keep-running server wasn't confirmed twice: {s.dialogs}")
    if not any(isinstance(x, tuple) and x[0] == "running-stop-any" and x[3] for x in ROOT_LOG):
        fail("owner", where, "the second confirmation didn't send force")
    s.drain(where)
    # the Machines tab and the per-machine AI accounts
    for tab, musts in (("machines", ("Machines", "Online", "CPU", "Memory", "Disk", "Load", "Active now")),
                       ("account", ("AI accounts on each machine", "minastirith", "krishbox",
                                    "owner.chatgpt@x.io", "Sign in"))):
        s.page.goto(url(f"/_box/me?tab={tab}"))
        inspect(s, f"owner {tab} tab")
        body = s.page.inner_text("body")
        for m in musts:
            if m not in body:
                fail("owner", f"owner {tab} tab", f"lacks {m!r}")
    s.drain("owner machines/account")
    s.close()
    RUNNING.clear()
    RUNNING.update(saved)


def in_view(page, sel):
    return page.evaluate("""s => { const el = document.querySelector(s); if (!el) return false;
        const r = el.getBoundingClientRect(); return r.bottom > 0 && r.top < innerHeight }""", sel)


def in_place(browser):
    """Every button's result shows inside the card holding it, without a reload and without jumping to the top
    (logs in a scrollable box in the server's card, Stop, status, a failed send); without JavaScript the redirect
    comes back to the same tab and card. Tabs: each its own URL, old #card links land on the right tab."""
    import copy
    saved = copy.deepcopy(RUNNING)
    s = Session(browser, "alice (in place)", "tok-alice", width=390)
    page = s.page
    page.goto(url("/_box/me?tab=servers"))
    where = "in place: shared server logs"
    b = page.locator("#shared button", has_text="Show recent output").first
    b.scroll_into_view_if_needed()
    page.evaluate("window.scrollBy(0, 120 - Math.max(0, innerHeight / 2 - document.querySelector('#shared').getBoundingClientRect().top))")
    y0 = page.evaluate("scrollY")
    if y0 <= 0:
        fail("alice", where, "the test needs the page scrolled down first")
    press(page, b)
    if not stayed(page):
        fail("alice", where, "the page reloaded")
    if page.locator("#shared [data-out] pre.logs").count() != 1:
        fail("alice", where, "the logs aren't in a scrollable box inside the group server's card")
    if page.evaluate("getComputedStyle(document.querySelector('#shared pre.logs')).overflowY") not in ("auto", "scroll"):
        fail("alice", where, "the logs box doesn't scroll")
    if page.evaluate("scrollY") < 1:
        fail("alice", where, "the page jumped to the top")
    if not in_view(page, "#shared [data-out] > *"):
        fail("alice", where, "the logs aren't on screen after pressing the button")
    if page.locator("#servers pre.logs").count():
        fail("alice", where, "logs showed in another card")
    page.locator("#shared [data-close]").click()
    if page.locator("#shared pre.logs").count():
        fail("alice", where, "Close didn't close the logs")
    inspect(s, where)
    s.drain(where)
    where = "in place: own server restart"
    y0 = page.evaluate("scrollY")
    press(page, page.locator("#servers button", has_text="Restart").first)
    if not stayed(page) or "Blog: restarted" not in page.locator("#servers [data-out]").inner_text():
        fail("alice", where, f"no result in the servers card: {page.locator('#servers').inner_text()[:200]!r}")
    if page.locator("#servers [data-out] .note.ok").count() != 1:
        fail("alice", where, "the result isn't one ok notice")
    s.drain(where)
    where = "in place: Stop in Running"
    stop = page.locator("#running button", has_text="Stop").last
    stop.scroll_into_view_if_needed()
    press(page, stop)
    if not stayed(page) or "Stopped" not in page.locator("#running [data-out]").inner_text():
        fail("alice", where, "Stop's result isn't in the Running card")
    if not in_view(page, "#running [data-out] > *") or page.evaluate("scrollY") < 1:
        fail("alice", where, "Stop's result is off screen or the page jumped to the top")
    s.drain(where)
    where = "in place: status on Home"
    page.goto(url("/_box/me"))
    page.fill("#people input[name=status]", "In class")
    press(page, page.locator("#people button", has_text="Show my status"))
    if not stayed(page) or "Status saved" not in page.locator("#people [data-out]").inner_text():
        fail("alice", where, "no 'Status saved' in the Who's here card")
    s.drain(where)
    where = "in place: failed send keeps the form open"
    page.goto(url("/_box/me?tab=messages"))
    page.locator("#messages summary", has_text="Write a message").click()
    page.fill("#messages input[name=title]", "Hi")
    page.evaluate("document.querySelector('#messages textarea[name=context]').required = false")  # root says no
    press(page, page.locator("#messages details.compose button", has_text="Send"))
    if not stayed(page) or not page.locator("#messages [data-out] .note.bad").count():
        fail("alice", where, "the error isn't in the Messages card")
    if not page.locator("#messages details.compose[open]").count():
        fail("alice", where, "the form closed after a failed send")
    s.drain(where)
    where = "in place: the machine unreachable"
    page.goto(url("/_box/me?tab=servers"))
    page.route("**/_box/me", lambda r: r.abort() if r.request.method == "POST" else r.continue_())
    press(page, page.locator("#servers button", has_text="Show recent output").first)
    if "Couldn't reach this machine" not in page.locator("#servers [data-out]").inner_text():
        fail("alice", where, "no error in the card when the request fails")
    if page.locator("#servers button.busy, #servers button:disabled").count():
        fail("alice", where, "the button stays busy after a failed request")
    page.unroute("**/_box/me")
    s.bad_responses.clear()
    s.console.clear()
    where = "tabs: old #card links"
    for old, tab in (("#running", "servers"), ("#messages", "messages"), ("#account", "account"), ("#dash", "account"),
                     ("#people", "home")):
        page.goto(url("/_box/me" + old))
        try:
            page.wait_for_function(f"document.querySelector('nav.tabs a[aria-current]').dataset.tab === '{tab}'",
                                   timeout=10000)
        except Exception:  # noqa: BLE001
            fail("alice", where, f"/_box/me{old} didn't land on the {tab} tab: {page.url}")
    page.goto(url("/_box/me?tab=servers"))
    page.goto(url("/_box/me?tab=account"))
    page.go_back()
    if "tab=servers" not in page.url or page.locator("nav.tabs a[aria-current]").get_attribute("data-tab") != "servers":
        fail("alice", where, f"Back didn't return to the servers tab: {page.url}")
    focused = []
    for _ in range(12):
        page.keyboard.press("Tab")
        focused.append(page.evaluate("document.activeElement.textContent.trim()"))
    if not any(f.startswith("Messages") for f in focused):
        fail("alice", where, f"the tabs can't be reached with the keyboard: {focused}")
    s.drain(where)
    s.close()
    # without JavaScript: post/redirect/get back to the same tab, scrolled to the card, the result inside it
    s = Session(browser, "alice (no js)", "tok-alice", js=False)
    page = s.page
    page.goto(url("/_box/me?tab=servers"))
    where = "no JavaScript: logs"
    with page.expect_navigation():
        page.locator("#servers button", has_text="Show recent output").first.click()
    if "tab=servers" not in page.url or not page.url.endswith("#servers"):
        fail("alice", where, f"didn't come back to the servers card: {page.url}")
    if page.locator("#servers [data-out] pre.logs").count() != 1:
        fail("alice", where, "the logs aren't inside the servers card")
    inspect(s, where)
    s.drain(where)
    s.close()
    s = Session(browser, "owner (in place)", "tok-owner")
    page = s.page
    page.goto(url("/_box/me?tab=admin"))
    if page.locator("nav.tabs a[aria-current]").get_attribute("data-tab") != "requests":
        fail("owner", "tabs", "?tab=admin (old links) doesn't open Requests while some are open")
    if page.locator("nav.tabs .tsep").count() != 1:
        fail("owner", "tabs", "the admin tabs aren't in the same bar after one divider")
    page.goto(url("/_box/me?tab=people"))
    page.fill("#invite input[name=email]", "sam@example.com")
    press(page, page.locator("#invite button", has_text="Create their account"))
    if not stayed(page) or "Invite ready" not in page.locator("#invite [data-out]").inner_text():
        fail("owner", "invite in place", "the invite message isn't in the Invite card")
    page.goto(url("/_box/settings"))
    if "tab=settings" not in page.url:
        fail("owner", "tabs", f"/_box/settings didn't open the Settings tab: {page.url}")
    s.drain("owner in place")
    s.close()
    RUNNING.clear()
    RUNNING.update(saved)


def tailnet_view():
    """The tailnet page (port 8090, gate=False): same start card, no device-link, no /_box/ links (404 there)."""
    alice = {"id": "nAlice1", "display": "Alice A", "login": "alice@github", "node": "laptop"}

    class Tailnet(w.Handler):
        def ident(self):
            return "100.64.0.9", dict(alice)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Tailnet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    w.ALLOWED_HOSTS.add("127.0.0.1")
    import urllib.error
    import urllib.request
    for app, want in ((False, ["Open T3 in this browser"]), (True, ["Open T3 in this browser"])):
        ACCOUNTS["alice"]["app"] = app
        try:
            body = urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/me", timeout=60).read().decode()
            acct = urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/me?tab=account", timeout=60).read().decode()
        finally:
            ACCOUNTS["alice"]["app"] = False
        where = "tailnet /me" + (" (app connected)" if app else "")
        if "device-link" in body:
            fail("alice (tailnet)", where, "device-link button (refused in Cloudflare mode)")
        rel = re.findall(r"""href=['"](/_box/[^'"]*)""", body + acct)
        if rel:
            fail("alice (tailnet)", where, f"relative /_box/ links (404 on the tailnet page): {rel}")
        if "href='/me?tab=account' data-tab='account'" not in body:
            fail("alice (tailnet)", where, "the tab bar doesn't link to /me?tab=...")
        if f"href='{PUBLIC}/'" not in acct or "Without Tailscale" not in acct:
            fail("alice (tailnet)", where, "no absolute 'Without Tailscale' link to the public site")
        if re.search(rf"(//|@){re.escape(HOSTNAME)}\b", re.sub(r"<header>.*?</header>", "", body, flags=re.S)):
            fail("alice (tailnet)", where, "an address uses the box's name")
        start = body[body.find("id='start'"):body.find("</section>")]
        start = re.sub(r"<div class='narrow'>.*?<!--/narrow-->", "", start, flags=re.S)  # phones only
        mains = re.findall(r"<button(?![^>]*class=)[^>]*>([^<]*)</button>|<a class='btn' [^>]*>([^<]*)</a>", start)
        got = [a or b for a, b in mains]
        if got != want:
            fail("alice (tailnet)", where, f"start card main buttons {got}, expected {want}")
    # no JavaScript on the tailnet page: post/redirect/get to the same tab, the result inside the card
    tok = w.token_for("100.64.0.9", alice["id"])

    class NoFollow(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    op = urllib.request.build_opener(NoFollow)
    body = urllib.parse.urlencode({"token": tok, "action": "where", "op": "set", "status": "Lunch"}).encode()
    try:
        op.open(urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/me", data=body), timeout=60)
        loc = ""
    except urllib.error.HTTPError as ex:
        loc = ex.headers.get("Location") or ""
    if not re.fullmatch(r"/me\?r=[\w-]+&tab=home#people", loc):
        fail("alice (tailnet)", "status without JS", f"redirect {loc!r}")
    else:
        page = urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}{loc.split('#')[0]}", timeout=60).read().decode()
        card = page[page.find("id='people'"):]
        if "Status saved" not in card[:card.find("</section>")]:
            fail("alice (tailnet)", "status without JS", "the result isn't inside the Who's here card")
    srv.shutdown()


def main():
    exe = chromium()
    if not exe:
        print("SKIP: no Chromium (set CHROMIUM=/path/to/chrome)")
        sys.exit(77)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe, args=["--no-sandbox",
                                    "--host-resolver-rules=MAP treehouse.test 127.0.0.1"])
        flow_no_token(browser)
        if SHOTS:  # first, while the box looks like it does for a new visitor (bob's welcome flag back after)
            screenshots(browser)
        start_cards(browser)  # the start card's states, the setup prompt, the tailnet page: in every mode
        tailnet_view()
        boxes_card(browser)
        running_card(browser)
        in_place(browser)
        w.portal_rate.clear()
        reg = sb.registry()  # bob's first visit (flow_login) must still see the welcome page
        reg["users"]["bob"]["welcome"] = True
        save_registry(reg)
        g._users = (0.0, {})
        if SHOTS_ONLY:
            browser.close()
            print(f"\nscreenshots in {SHOTS}; {len(FAILS)} problem(s)")
            sys.exit(1 if FAILS else 0)
        flow_login(browser)  # bob's first visit must still see the welcome page
        flow_open_t3(browser, "alice", "tok-alice", PORT_OF["alice"])
        flow_open_t3(browser, "alice (cookie)", "tok-alice", PORT_OF["alice"], cookie=True)
        flow_open_t3(browser, "owner", "tok-owner", OWNER_T3_PORT)
        flow_apps(browser)
        flow_invite(browser)
        logout_hrefs(browser)
        owner_requests(browser)
        depth = 1 if QUICK else 3
        crawl(browser, "alice", "tok-alice", depth=depth)
        crawl(browser, "bob", "tok-bob", depth=depth)
        crawl(browser, "owner", "tok-owner", depth=depth)
        crawl(browser, "stranger", "tok-stranger", start="/", depth=depth, expect=(404,))
        crawl(browser, "stranger (cookie)", "tok-stranger", start=f"/i/{INVITE_TOK}", depth=1, cookie=True)
        browser.close()
    print(f"\nvisited {len(VISITED)} (persona, page) pairs; {len(ROOT_LOG)} calls to the fake root")
    if FAILS:
        print(f"{len(FAILS)} problem(s)")
        sys.exit(1)
    print("browser QA: clean")


main()
