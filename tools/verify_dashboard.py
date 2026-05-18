#!/usr/bin/env python3
"""Headless-render the weekly gym dashboard and report any JS errors.

Requires once:
    pip install --user playwright
    python3 -m playwright install chromium

Run:
    python3 tools/verify_dashboard.py

Exits 0 if no panel-level errors detected, 1 otherwise. Always saves a
screenshot to /tmp/gym-dash.png for visual inspection.
"""
from playwright.sync_api import sync_playwright
import argparse
import sys

URL = "http://127.0.0.1:3000"
DASHBOARD_PATH = "/d/gym-weekly-display/gym-weekly-occupancy?orgId=1"
USER = "admin"
PASSWORD = "admin"
SCREENSHOT = "/tmp/gym-dash.png"

ERROR_TEXT_REGEX = (
    "ECharts Execution Error|ReferenceError|TypeError|Plugin error|"
    "Panel Error|is not defined|before initialization"
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="show browser window")
    args = parser.parse_args()

    console_errors = []
    page_errors = []
    request_failures = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        context = browser.new_context(viewport={"width": 1800, "height": 1400})
        page = context.new_page()

        page.on("console", lambda m: console_errors.append((m.type, m.text))
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("requestfailed", lambda r: request_failures.append(
            (r.url, r.failure)) if "3000" in r.url else None)

        page.goto(f"{URL}/login", timeout=15000)
        page.fill('input[name="user"]', USER)
        page.fill('input[name="password"]', PASSWORD)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle", timeout=15000)

        page.goto(f"{URL}{DASHBOARD_PATH}", timeout=20000)
        page.wait_for_selector("text=Weekly 30-Minute Occupancy Heatmap",
                               timeout=20000)
        page.wait_for_timeout(6000)  # give ECharts time to render or fail

        in_dom = page.locator(f"text=/{ERROR_TEXT_REGEX}/").all()
        in_dom_snippets = [el.inner_text()[:200] for el in in_dom]

        page.screenshot(path=SCREENSHOT, full_page=True)
        browser.close()

    print(f"console errors/warnings: {len(console_errors)}")
    for t, msg in console_errors[:25]:
        print(f"  [{t}] {msg[:240]}")
    print(f"page errors (uncaught): {len(page_errors)}")
    for e in page_errors[:10]:
        print(f"  {e[:300]}")
    print(f"in-DOM error snippets: {len(in_dom_snippets)}")
    for s in in_dom_snippets[:10]:
        print(f"  {s}")
    print(f"request failures: {len(request_failures)}")
    for u, f in request_failures[:10]:
        print(f"  {u} :: {f}")
    print(f"screenshot saved to {SCREENSHOT}")

    fatal = bool(page_errors) or bool(in_dom_snippets) or any(
        t == "error" for t, _ in console_errors)
    sys.exit(1 if fatal else 0)


if __name__ == "__main__":
    main()
