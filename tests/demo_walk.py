"""Walk the clickable demo (tests/demo_site.py) in a real Chromium, the way a person would.

    uv run --no-project --with playwright python tests/demo_walk.py [--url http://HOST:PORT] [--shots DIR]

Without --url it starts its own demo on 127.0.0.1. It checks: the person picker; every person lands on their
dashboard with the demo banner; every tab of every person, at 1280 and 390 wide (no sideways scrolling, no errors,
no leftover placeholders); and that each button the demo promises really changes what's shown after a reload:
status, start/stop, keep running, hackathon pause, a server description, signing out and in per machine, GitHub,
Telegram, messages between people, teammates' access, the owner's requests, Open T3, and Reset demo.

Exit 0 when clean, 1 on failures, 77 when no Chromium is available. With --url it resets that demo at the end.
"""
import glob
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

args = sys.argv[1:]
URL = args[args.index("--url") + 1].rstrip("/") if "--url" in args else ""
SHOTS = args[args.index("--shots") + 1] if "--shots" in args else ""
HERE = os.path.dirname(os.path.abspath(__file__))
FAILS = []
PERSONAS = ["alice", "bob", "dave", "erin", "owner", "stranger"]
BAD = [(re.compile(r"Traceback|Exception|Error:"), "python error"), (re.compile(r"bad origin", re.I), "bad origin"),
       (re.compile(r"\{[a-z_]+\}"), "{placeholder}"), (re.compile(r"\b(None|null|undefined|NaN)\b(?! yet)"), "None/null"),
       (re.compile(r"treehouse\.demo"), "the fake public name leaked")]


def fail(where, what):
    FAILS.append(f"{where}: {what}")
    print(f"FAIL {where}: {what}", flush=True)


def chromium():
    if os.environ.get("CHROMIUM"):
        return os.environ["CHROMIUM"]
    c = sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome")),
               key=lambda p: int(re.search(r"chromium-(\d+)", p).group(1)))
    return c[-1] if c else None


def start_demo():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "demo_site.py"), "--host", "127.0.0.1",
                             "--port", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(200):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/_demo", timeout=2)
            return proc, f"http://127.0.0.1:{port}"
        except OSError:
            if proc.poll() is not None:
                sys.exit("demo didn't start: " + proc.stderr.read().decode()[-2000:])
            time.sleep(0.1)
    proc.kill()
    sys.exit("demo didn't start in 20 s")


class Person:
    def __init__(self, browser, width=1280, scheme="light"):
        self.ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
        self.page = self.ctx.new_page()
        self.errors = []
        self.ctx.on("page", self.watch)
        self.watch(self.page)

    def watch(self, page):
        page.on("console", lambda m: m.type == "error" and not m.text.startswith("Failed to load resource")
                and self.errors.append(m.text))
        page.on("pageerror", lambda ex: self.errors.append(f"pageerror: {ex}"))
        page.on("dialog", lambda d: d.accept())
        page.on("response", lambda r: r.status >= 500 and self.errors.append(f"{r.status} {r.url}"))

    def go(self, path):
        self.page.goto(URL + path)
        self.page.wait_for_load_state("load")

    def be(self, persona):
        self.go(f"/_demo/as/{persona}")

    def text(self, sel="body"):
        loc = self.page.locator(sel)
        return loc.first.inner_text() if loc.count() else ""

    def check(self, where, banner=True):
        p = self.page
        body = self.text()
        if banner and "Demo: fake data, nothing here is real." not in self.text("#demo-bar"):
            fail(where, "no demo banner")
        for rx, what in BAD:
            m = rx.search(body)
            if m:
                fail(where, f"{what}: ...{body[max(0, m.start() - 60):m.end() + 60]!r}...")
        if p.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"):
            fail(where, f"sideways scrolling at {p.viewport_size['width']}px")
        for err in self.errors:
            fail(where, err)
        self.errors.clear()

    def shot(self, name):
        if SHOTS:
            os.makedirs(SHOTS, exist_ok=True)
            self.page.screenshot(path=os.path.join(SHOTS, name + ".png"), full_page=True)

    def press(self, target, timeout=30):
        """Click a dashboard button: its result comes in place (window.sbDone counts up) or as a page load."""
        p = self.page
        before = p.evaluate("window.sbDone || 0")
        p.evaluate("window.__stay = 1")
        target.click()
        end = time.time() + timeout
        while time.time() < end:
            try:
                if p.evaluate(f"!window.__stay || (window.sbDone || 0) > {before}"):
                    break
            except Exception:  # noqa: BLE001 - mid-navigation
                pass
            p.wait_for_timeout(40)
        else:
            raise TimeoutError("no result after pressing the button")
        p.wait_for_load_state("load")

    def server(self, name):
        return self.page.locator(f"#servers .item:has(input[name=key][value='{name}'])").first

    def close(self):
        self.ctx.close()


def walk_pages(browser):
    """Every person, every tab, wide and narrow, light and dark."""
    for width, scheme in ((1280, "light"), (390, "dark"), (390, "light"), (1280, "dark")):
        who = Person(browser, width, scheme)
        who.go("/_demo")
        who.check(f"picker @{width}")
        if width == 1280 and scheme == "light" and who.page.locator("a.btn", has_text="Continue as").count() != 6:
            fail("picker", "expected six 'Continue as' buttons")
        who.shot(f"picker-{width}-{scheme}")
        for persona in PERSONAS:
            who.go("/_demo")
            who.page.locator("a.btn", has_text=f"Continue as {persona.title()}").click()
            who.page.wait_for_load_state("load")
            where = f"{persona} landing @{width} {scheme}"
            who.check(where)
            cur = who.page.locator("#demo-bar a[aria-current]")
            if not cur.count() or cur.inner_text().lower() != persona:
                fail(where, "the banner doesn't show who you are")
            who.shot(f"{persona}-landing-{width}-{scheme}")
            if persona == "stranger":
                if "no account" not in who.text():
                    fail(where, "the stranger doesn't see the no-account page")
                continue
            if persona == "bob":  # the welcome page once, then only "Log in to your AI" until he does
                if width == 1280 and scheme == "light" and "Welcome" not in who.text():
                    fail(where, "bob doesn't see the welcome page first")
                who.go("/_box/me")
                if "Log in with Claude" not in who.text():
                    fail(where, "bob isn't asked to log in to an AI")
                who.check(where)
                continue
            who.go("/_box/me")
            tabs = [(a.get_attribute("data-tab"), a.get_attribute("href")) for a in who.page.locator("nav.tabs a").all()]
            want = {"home", "messages", "servers", "machines", "account"} | (
                {"people", "running", "requests", "settings", "events"} if persona == "owner" else set())
            if not want <= {t for t, _ in tabs}:
                fail(where, f"tabs {[t for t, _ in tabs]}")
            for tab, href in tabs:
                who.go(href)
                w2 = f"{persona} tab {tab} @{width} {scheme}"
                who.check(w2)
                if who.page.locator("nav.tabs a[aria-current]").get_attribute("data-tab") != tab:
                    fail(w2, "tab bar doesn't mark this tab")
                who.shot(f"{persona}-{tab}-{width}-{scheme}")
        who.close()


def walk_buttons(browser):
    """Each state-changing button, as alice (then bob and the owner), checked again after a reload."""
    a = Person(browser)
    a.go("/_demo/reset")
    a.be("alice")
    p = a.page

    # status
    p.locator("#people input[name=status]").fill("Testing the demo")
    a.press(p.locator("#people button", has_text="Show my status"))
    a.go("/_box/me")
    if "Testing the demo" not in a.text("#people"):
        fail("status", "the new status isn't on Home after a reload")
    a.check("status")

    # start / stop / keep running / hackathon pause
    a.go("/_box/me?tab=servers")
    a.press(a.server("bot").locator("button", has_text=re.compile("^Start$")))
    a.press(a.server("shop").locator("button", has_text=re.compile("^Stop$")))
    a.press(a.server("bot").locator("button", has_text="Keep it running"))
    a.press(a.server("shop").locator("button", has_text="Pause it during hackathons"))
    a.go("/_box/me?tab=servers")
    bot, shop = a.server("bot").inner_text(), a.server("shop").inner_text()
    if "Start" in a.server("bot").locator("button").all_inner_texts():
        fail("start", f"bot isn't running after Start: {bot[:200]!r}")
    if "Start" not in a.server("shop").locator("button").all_inner_texts():
        fail("stop", f"shop isn't stopped after Stop: {shop[:200]!r}")
    if not a.server("bot").locator("button", has_text="Allow automatic stops").count():
        fail("keep running", "bot isn't marked keep running after a reload")
    if a.server("shop").locator("button", has_text="Pause it during hackathons").count():
        fail("hackathon", "shop's hackathon switch didn't change")
    if "bot" not in a.text("#running"):
        fail("start", "bot (started) isn't in the Running list")
    a.check("servers")

    # a description, written by the agent (a demo control), shows on the card
    a.go("/_demo")
    a.page.locator("button", has_text="describes the server shop").click()
    a.page.wait_for_load_state("load")
    a.go("/_box/me?tab=servers")
    if "Practice online shop" not in a.server("shop").inner_text():
        fail("description", "shop's new description isn't on its card")

    # sign out and in on another machine (the AI accounts table)
    a.go("/_box/me?tab=account")
    row = p.locator("#accounts tr", has_text="minastirith").filter(has_text="Claude")
    if not row.count():
        row = p.locator("#accounts tr", has_text="minastirith")
    a.press(row.first.locator("button", has_text="Sign out"))
    a.go("/_box/me?tab=account")
    row = p.locator("#accounts tr", has_text="minastirith").first
    if row.locator("button", has_text="Sign out").count() and "alice.work@example.com" in row.inner_text():
        fail("sign out", f"still signed in on minastirith: {row.inner_text()[:200]!r}")
    a.press(row.locator("button", has_text="Sign in").first)
    code = p.locator("#accounts input[name=code]")
    if code.count():
        code.first.fill("demo-code-1234")
        a.press(p.locator("#accounts [data-out] button", has_text="Finish signing in"))
    else:
        fail("sign in", f"no code box after Sign in: {a.text('#accounts')[:300]!r}")
    a.go("/_box/me?tab=account")
    if not p.locator("#accounts tr", has_text="minastirith").first.locator("button", has_text="Sign out").count():
        fail("sign in", "not signed in on minastirith after pasting the code")
    a.check("accounts")

    # GitHub: device code -> the outside page -> pretend -> connected
    gh = p.locator("button", has_text="Connect GitHub")
    a.press(gh.first)
    link = p.locator("#account a[href^='/_demo/outside']").first
    if not link.count():
        fail("github", "no link to the (demo) GitHub page")
    else:
        a.go(link.get_attribute("href"))
        a.check("github outside page")
        if "In the demo this would open GitHub" not in a.text():
            fail("github", "the outside page doesn't say what would happen")
        p.locator("button", has_text="Pretend").click()
        p.wait_for_load_state("load")
        a.go("/_box/me?tab=account")  # on a real box the page notices by itself; a reload shows it too
        if p.locator("button", has_text=re.compile("^Connect GitHub$")).count():
            fail("github", "GitHub isn't connected after pretending")

    # Telegram
    a.press(p.locator("button", has_text="Connect Telegram"))
    link = p.locator("a[href*='t.me']").first
    if not link.count():
        fail("telegram", "no Telegram link after Connect Telegram")
    else:
        a.go(link.get_attribute("href"))
        p.locator("button", has_text="Pretend I pressed Start").click()
        p.wait_for_load_state("load")
        a.go("/_box/me?tab=account")
        if p.locator("button", has_text=re.compile("^Connect Telegram$")).count():
            fail("telegram", "Telegram isn't connected after pretending")

    # teammates
    p.locator("#team select[name=to]").select_option("dave")
    a.press(p.locator("#team button", has_text="Give access"))
    a.go("/_box/me?tab=account")
    if "dave" not in a.text("#team ul.rows"):
        fail("team", "dave isn't listed after Give access")
    a.press(p.locator("#team li", has_text="dave").locator("button", has_text="Remove access"))
    a.go("/_box/me?tab=account")
    if "dave" in a.text("#team ul.rows"):
        fail("team", "dave is still listed after Remove access")
    a.check("team")

    # messages: alice writes to bob, bob sees it and marks it done
    a.go("/_box/me?tab=messages")
    p.locator("#messages summary", has_text="Write a message").click()
    p.locator("#messages select[name=to]").select_option("bob")
    p.fill("#messages input[name=title]", "Demo walk says hi")
    p.fill("#messages textarea[name=context]", "Checking that messages arrive.")
    a.press(p.locator("#messages details.compose button", has_text="Send"))
    a.press(p.locator("#messages .item", has_text="Fix the login page").locator("button", has_text="Mark done"))
    a.go("/_box/me?tab=messages")
    if "Fix the login page" in a.text("#messages"):
        fail("messages", "Mark done didn't remove the message")
    # bob signs in to Claude (paste flow) from the welcome page's start card
    a.be("bob")  # nothing but "Log in to your AI" until he does
    btn = p.locator("button", has_text="Log in with Claude")
    if btn.count():
        a.press(btn.first)
        p.fill("input[name=code]", "demo-code-1234")
        a.press(p.get_by_role("button", name="Finish"))
        a.go("/_box/me")
        if p.locator("button", has_text="Log in with Claude").count():
            fail("bob claude", "still asks bob to log in with Claude")
        if not p.locator("nav.tabs a[data-tab=servers]").count():
            fail("bob claude", "no tabs after bob logged in")
    else:
        fail("bob claude", "no 'Log in with Claude'")

    a.go("/_box/me?tab=messages")
    if "Demo walk says hi" not in a.text("#messages"):
        fail("messages", "bob doesn't see alice's message")
    a.check("bob messages")

    # the owner approves a request (and one made by the demo control)
    a.go("/_demo")
    p.locator("button", has_text="asks the owner to install").click()
    p.wait_for_load_state("load")
    a.be("owner")
    a.go("/_box/me?tab=requests")
    if "imagemagick" not in a.text("#requests"):
        fail("requests", "the new request isn't on the owner's Requests tab")
    n0 = p.locator("#requests button", has_text="Decline").count()
    a.press(p.locator("#requests button", has_text="Decline").first)
    a.go("/_box/me?tab=requests")
    if p.locator("#requests button", has_text="Decline").count() != n0 - 1:
        fail("requests", "Decline didn't remove the request")
    a.check("owner requests")

    # Open T3: a stand-in page, with the banner
    a.be("alice")
    p.locator("#start a[href='/_box/app'], a:has-text('Open T3 in this browser')").first.click()
    try:
        p.wait_for_function("document.title === 'Fake T3 Code'", timeout=15000)
        a.check("open T3")
    except Exception:  # noqa: BLE001
        fail("open T3", f"didn't reach the stand-in T3 page: {p.url} {a.text()[:200]!r}")

    # Reset demo: alice's status and bob's message are back to the start
    a.go("/_box/me")
    p.locator("#demo-bar a", has_text="Reset demo").click()
    p.wait_for_load_state("load")
    if "Testing the demo" in a.text("#people") or "At the hackathon" not in a.text("#people"):
        fail("reset", "Reset demo didn't restore alice's status")
    if "/_box/me" not in p.url:
        fail("reset", f"Reset from the banner should stay on the dashboard, got {p.url}")
    a.go("/_box/me?tab=servers")
    if "Start" not in a.server("bot").locator("button").all_inner_texts():
        fail("reset", "bot isn't stopped again after Reset demo")
    a.check("reset")
    a.close()


def main():
    global URL
    exe = chromium()
    if not exe:
        print("SKIP: no Chromium (set CHROMIUM=/path/to/chrome)")
        sys.exit(77)
    proc = None
    if not URL:
        proc, URL = start_demo()
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=exe, args=["--no-sandbox"])
            walk_pages(browser)
            walk_buttons(browser)
            browser.close()
    finally:
        try:
            urllib.request.urlopen(URL + "/_demo/reset", timeout=30)
        except OSError:
            pass
        if proc:
            proc.terminate()
            proc.wait(30)
    if FAILS:
        print(f"\n{len(FAILS)} problem(s)")
        sys.exit(1)
    print("demo walk: clean")


main()
