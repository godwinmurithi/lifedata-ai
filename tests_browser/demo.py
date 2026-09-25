"""Watchable end-to-end demo of the LifeData copilot.

Opens a real browser window and drives the whole product at human speed, so the
flow can be observed rather than inferred from logs.

    uv run python tests_browser/demo.py
    uv run python tests_browser/demo.py --fast     # less dawdling
"""

from __future__ import annotations

import argparse
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8080"
USERNAME = "admin"
PASSWORD = "lifedata-admin-dev"


def narrate(step: str, detail: str = "") -> None:
    print(f"\n>>> {step}")
    if detail:
        print(f"    {detail}")


def wait_for_turn(page, seconds: int = 600) -> None:
    """Block until the panel re-enables its input — its own 'turn finished' signal."""
    page.wait_for_function(
        "() => { const t = document.getElementById('lifedata-ai-input');"
        " return t && !t.disabled; }",
        timeout=seconds * 1000,
    )


def ask(page, panel, question: str, pause: float) -> str:
    box = page.locator("textarea").last
    box.click()
    # Typed rather than filled so it is visible as it happens.
    box.type(question, delay=12)
    page.wait_for_timeout(400)
    box.press("Enter")
    wait_for_turn(page)
    page.wait_for_timeout(int(pause * 1000))
    return panel.inner_text() if panel.count() else ""


def approve_any_previews(page, pause: float) -> int:
    """Click Apply on anything the risk classifier held back for confirmation."""
    approved = 0
    for _ in range(6):
        buttons = page.locator("button.ld-action__apply:not([disabled])")
        if buttons.count() == 0:
            break
        narrate("A change needs confirmation — clicking Apply",
                "risk=preview: more than two charts, a removal, or someone else's dashboard")
        page.wait_for_timeout(int(pause * 1000))
        buttons.first.click()
        approved += 1
        page.wait_for_timeout(6000)
    return approved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true")
    args = parser.parse_args()
    pause = 1.0 if args.fast else 2.5
    slow_mo = 120 if args.fast else 350

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            slow_mo=slow_mo,
            args=["--start-maximized", "--window-position=0,0"],
        )
        context = browser.new_context(no_viewport=True)
        page = context.new_page()

        narrate("Opening Superset", f"{BASE} — nginx serves Superset and the copilot on one origin")
        page.goto(f"{BASE}/login/", wait_until="domcontentloaded")
        page.wait_for_timeout(int(pause * 1000))

        narrate("Signing in", f"{USERNAME} — the copilot will act with THIS user's session")
        page.fill("#username", USERNAME)
        page.wait_for_timeout(500)
        page.fill("#password", PASSWORD)
        page.wait_for_timeout(500)
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle", timeout=60_000)
        page.wait_for_timeout(int(pause * 1000))

        narrate("Waiting for the panel", "nginx injected panel.js into Superset's HTML")
        page.wait_for_function("() => window.__lifedataCopilot !== undefined", timeout=30_000)
        page.wait_for_timeout(int(pause * 1000))

        narrate("Opening the copilot", "bottom-right launcher")
        toggle = page.locator("button.ld-toggle")
        if toggle.count():
            toggle.first.click()
        else:
            page.evaluate("window.__lifedataCopilot.open()")
        page.wait_for_timeout(int(pause * 1000))
        panel = page.locator("[role='complementary'], aside, [class*='ld-panel']").first

        # --- 1. Read: grounded in live metadata ---------------------------
        narrate("QUESTION 1 — reading live Superset metadata",
                "'What datasets are available for laboratory analysis?'")
        ask(page, panel, "What datasets are available for laboratory analysis?", pause)

        # --- 2. Governed definition ---------------------------------------
        narrate("QUESTION 2 — a governed indicator definition",
                "the answer must come from the semantics registry, verbatim")
        ask(page, panel, "Explain the malaria positivity indicator.", pause)

        # --- 3. Safety: geography policy ----------------------------------
        narrate("QUESTION 3 — asking for something the policy forbids",
                "point-mapping individual patients: this must be refused")
        ask(page, panel,
            "Plot individual patient home locations as points on a scatter map.", pause)

        # --- 4. Write: create a chart -------------------------------------
        narrate("QUESTION 4 — creating a real chart",
                "watch: the write is executed by YOUR browser, not the server")
        ask(page, panel,
            "Create a bar chart called 'Demo — Encounters by Facility' from "
            "rpt_encounters (dataset 2), counting encounters grouped by facility_name.",
            pause)
        approve_any_previews(page, pause)

        # --- 5. Undo -------------------------------------------------------
        narrate("Undo", "every applied change carries an Undo button")
        undo = page.locator("button.ld-applied__undo:not([disabled])")
        if undo.count():
            page.wait_for_timeout(int(pause * 1000))
            undo.last.click()
            page.wait_for_timeout(7000)
        else:
            print("    (no undo button found)")

        # --- 6. Show the result -------------------------------------------
        narrate("Opening the chart list", "the chart the copilot just created")
        page.goto(f"{BASE}/chart/list/", wait_until="networkidle")
        page.wait_for_timeout(int(pause * 2000))

        narrate("Opening a dashboard the copilot built earlier")
        page.goto(f"{BASE}/superset/dashboard/12/", wait_until="networkidle")
        page.wait_for_timeout(15_000)

        print("\n" + "=" * 60)
        print("Demo finished. The browser stays open for 90 seconds —")
        print("click around, then it will close on its own.")
        print("=" * 60)
        time.sleep(90)
        context.close()
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
