"""
HuntPilot - Step 1: Crawler + detectors (no AI yet)

What it does, for each demo user:
  1. Opens a fresh browser and signs in
  2. Finds every page from the menu and footer, and visits each one
  3. Clicks simple buttons that are not part of a form (e.g. "Download statement")
  4. Records:
       - JavaScript errors (console errors and crashes)
       - Failed network requests (status 400 or higher)
       - Broken links
       - Accessibility problems (using axe-core)
  5. Saves a screenshot of every page and a JSON report in the "reports" folder

Run it:  python step1_crawler.py
"""

import json
import os
from datetime import datetime
from urllib.parse import urljoin, urlparse

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from axe_playwright_python.sync_playwright import Axe

# ---------- Settings ----------
load_dotenv()
TARGET_URL = os.getenv("TARGET_URL", "http://localhost:8000").rstrip("/") + "/"
USERS = ["alex", "sam"]
PASSWORD = "demo123"
HEADLESS = True          # set to False to watch the browser work
REPORT_DIR = "reports"
SKIP_BUTTON_WORDS = ["log out", "logout", "sign out", "delete"]  # never click these

axe = Axe()


def sign_in(page, username):
    page.goto(TARGET_URL + "#login")
    page.get_by_test_id("username").fill(username)
    page.get_by_test_id("password").fill(PASSWORD)
    page.get_by_test_id("login-button").click()
    page.wait_for_url("**#dashboard")


def find_links(page):
    """Return all links on the current page as (text, href) pairs."""
    return page.eval_on_selector_all(
        "a[href]", "els => els.map(a => [a.innerText.trim(), a.getAttribute('href')])"
    )


def hunt(playwright, username):
    findings = []
    seen = set()

    def add(kind, page_name, detail):
        key = (kind, detail)
        if key in seen:
            return
        seen.add(key)
        findings.append({"type": kind, "page": page_name, "detail": detail})
        print(f"   [{kind}] {page_name}: {detail}")

    browser = playwright.chromium.launch(headless=HEADLESS)
    context = browser.new_context()          # fresh browser = fresh app data
    page = context.new_page()
    current = {"page": "login"}

    # Detectors that listen the whole time
    # (console "Failed to load resource" messages are skipped: the network detector already reports them)
    page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text
            and add("console-error", current["page"], m.text))
    page.on("pageerror", lambda e: add("js-crash", current["page"], str(e)))
    page.on("response", lambda r: r.status >= 400 and add(
        "network-error", current["page"], f"{r.status} {r.request.method} {r.url}"))

    print(f"\n=== Hunting as '{username}' ===")
    sign_in(page, username)

    # Discover pages: in-app links start with '#'; everything else is checked as a link
    pages_to_visit, other_links = [], set()
    for text, href in find_links(page):
        if href.startswith("#"):
            if href not in pages_to_visit:
                pages_to_visit.append(href)
        else:
            other_links.add(href)

    os.makedirs(f"{REPORT_DIR}/screenshots", exist_ok=True)

    for route in pages_to_visit:
        current["page"] = route.lstrip("#")
        page.goto(TARGET_URL + route)
        page.wait_for_timeout(500)            # give the page a moment to finish loading

        # Collect any extra links found on this page
        for text, href in find_links(page):
            if not href.startswith("#"):
                other_links.add(href)

        # Accessibility check
        result = axe.run(page)
        for v in result.response["violations"]:
            targets = [", ".join(n["target"]) for n in v["nodes"]]
            add("accessibility", current["page"], f"{v['id']}: {v['help']} -> {targets}")

        # Click simple buttons that are not inside a form
        buttons = page.locator("main button:not(form button)")
        for i in range(buttons.count()):
            label = (buttons.nth(i).inner_text() or "").strip()
            if any(w in label.lower() for w in SKIP_BUTTON_WORDS):
                continue
            buttons.nth(i).click()
            page.wait_for_timeout(800)

        page.screenshot(path=f"{REPORT_DIR}/screenshots/{username}_{current['page']}.png", full_page=True)
        print(f"   visited {route}")

    # Broken link check
    current["page"] = "link-check"
    for href in sorted(other_links):
        url = urljoin(TARGET_URL, href)
        if urlparse(url).netloc != urlparse(TARGET_URL).netloc:
            continue                          # only check links on our own site
        status = context.request.get(url).status
        if status >= 400:
            add("broken-link", "footer/menu", f"{href} returned {status}")

    browser.close()
    return {"user": username, "pages_visited": pages_to_visit, "findings": findings}


def main():
    os.makedirs(REPORT_DIR, exist_ok=True)
    results = []
    with sync_playwright() as p:
        for user in USERS:
            results.append(hunt(p, user))

    report = {"target": TARGET_URL, "run_at": datetime.now().isoformat(timespec="seconds"), "results": results}
    with open(f"{REPORT_DIR}/step1_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print("\n=== Summary ===")
    for r in results:
        print(f"{r['user']}: {len(r['findings'])} finding(s) across {len(r['pages_visited'])} pages")
    print(f"Full report: {REPORT_DIR}/step1_report.json")


if __name__ == "__main__":
    main()