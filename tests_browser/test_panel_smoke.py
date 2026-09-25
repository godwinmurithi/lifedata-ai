"""Browser smoke test for the copilot panel.

Runs on the HOST, not in the django container: the panel is injected by nginx
and must be exercised through the same origin a real user sees, with a real
Superset session cookie.

    uv run python tests_browser/test_panel_smoke.py
    uv run python tests_browser/test_panel_smoke.py --headed --slow

This is a smoke test, not a unit suite. It answers one question: does a person
who logs in actually get a working copilot?
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

BASE = "http://localhost:8080"
USERNAME = "admin"
PASSWORD = "lifedata-admin-dev"
ARTEFACTS = Path(__file__).resolve().parent / "artefacts"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((bool(ok), label, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n        {detail}" if detail else ""))
    return bool(ok)


def main(argv: list[str] | None = None) -> int:
    # `argv` is explicit so pytest can call this without argparse
    # swallowing pytest's own command line.
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--slow", action="store_true", help="slow motion, for watching")
    parser.add_argument("--question", default="What datasets are available for laboratory analysis?")
    args = parser.parse_args(argv)

    results.clear()
    ARTEFACTS.mkdir(exist_ok=True)
    console_errors: list[str] = []
    failed_requests: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not args.headed, slow_mo=400 if args.slow else 0
        )
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()

        # Capture everything a developer would look at in devtools.
        page.on("console", lambda m: console_errors.append(f"{m.type}: {m.text}")
                if m.type in ("error", "warning") else None)
        page.on("requestfailed", lambda r: failed_requests.append(
            f"{r.method} {r.url} — {r.failure}"))

        print(f"\n=== Copilot panel smoke test against {BASE} ===\n")

        # --- 1. Log in ----------------------------------------------------
        print("-- login --")
        page.goto(f"{BASE}/login/", wait_until="domcontentloaded")
        check("panel.js" in page.content(), "panel script injected into login page")
        check(page.evaluate("typeof window.__lifedataCopilot") == "undefined",
              "panel correctly does NOT initialise on /login (by design)")

        # Superset 6.1.0's login is an Ant Design React form: the fields carry
        # an `id` but NO `name` attribute, so the obvious input[name=...]
        # selector times out. Verified against the pinned version.
        page.fill("#username", USERNAME)
        page.fill("#password", PASSWORD)
        page.click("button[type='submit']")
        page.wait_for_load_state("networkidle", timeout=60_000)
        check("/login" not in page.url, "logged in", f"landed on {page.url}")

        # --- 2. Panel bootstraps -----------------------------------------
        print("\n-- panel bootstrap --")
        js_status = page.evaluate(
            """async () => {
                const r = await fetch('/lifedata-ai/static/panel/panel.js');
                return {status: r.status, bytes: (await r.text()).length};
            }"""
        )
        check(js_status["status"] == 200, "panel.js served",
              f"status={js_status['status']} bytes={js_status['bytes']}")

        # markdown.js is injected by panel.js as a same-origin <script>. Under
        # Superset's Talisman CSP (`script-src 'self'`) that is allowed, but a
        # missing file would degrade every answer to unformatted text silently —
        # `renderMarkdown` falls back to textContent by design.
        md_status = page.evaluate(
            """async () => {
                const r = await fetch('/lifedata-ai/static/panel/markdown.js');
                return {status: r.status, bytes: (await r.text()).length};
            }"""
        )
        check(md_status["status"] == 200, "markdown.js served",
              f"status={md_status['status']} bytes={md_status['bytes']}")

        try:
            page.wait_for_function("() => window.__lifedataCopilot !== undefined",
                                   timeout=15_000)
            booted = True
        except PlaywrightTimeout:
            booted = False
        check(booted, "panel bootstrapped (window.__lifedataCopilot exists)",
              "" if booted else "script loaded but did not initialise — see console errors below")

        try:
            page.wait_for_function("() => window.__lifedataMarkdown !== undefined",
                                   timeout=15_000)
            markdown_ready = True
        except PlaywrightTimeout:
            markdown_ready = False
        check(markdown_ready, "markdown renderer loaded and exposed",
              "" if markdown_ready else "answers would render as plain text")

        toggle = page.locator("[data-lifedata-toggle], button[aria-label*='opilot' i]")
        toggle_count = toggle.count()
        if toggle_count == 0:
            # Fall back to any fixed-position button the panel may have created.
            toggle = page.locator("button").filter(has_text=re.compile("copilot", re.I))
            toggle_count = toggle.count()
        check(toggle_count > 0, "toggle button present in the DOM",
              f"{toggle_count} candidate(s)")

        page.screenshot(path=str(ARTEFACTS / "01-after-login.png"), full_page=False)

        # --- 3. Open the panel -------------------------------------------
        print("\n-- open the panel --")
        opened = False
        if toggle_count > 0:
            try:
                toggle.first.click(timeout=10_000)
                opened = True
            except PlaywrightError as exc:
                print(f"        toggle click failed: {str(exc)[:120]}")
        if not opened and booted:
            page.evaluate("window.__lifedataCopilot.open()")
            opened = True
        check(opened, "panel opened")

        page.wait_for_timeout(1200)
        panel = page.locator("[role='complementary'], aside, [class*='lifedata']").first
        visible = panel.is_visible() if panel.count() else False
        check(visible, "panel is visible on screen")
        page.screenshot(path=str(ARTEFACTS / "02-panel-open.png"))

        # --- 4. Ask a real question --------------------------------------
        print("\n-- live conversation --")
        box = page.locator("textarea").last
        if check(box.count() > 0, "message input found"):
            box.click()
            box.fill(args.question)
            page.screenshot(path=str(ARTEFACTS / "03-question-typed.png"))
            box.press("Enter")

            # Wait for the stream to FINISH, not merely to start.
            #
            # Two earlier heuristics were both wrong, and each was wrong in an
            # instructive way:
            #
            #   1. Waiting for /laboratory/ to appear matched the model's own
            #      preamble ("I'll check what's available for laboratory
            #      analysis") and cut the tool round off mid-flight.
            #   2. Polling until the transcript stopped *growing* was defeated by
            #      the status strip, which ticks "Working · 5s" -> "Working · 6s"
            #      — the same LENGTH, so the poll declared the turn finished
            #      after four seconds while the model was still thinking.
            #
            # The panel already publishes the exact signal: `aria-busy` on the
            # message list is true for the duration of a turn and cleared at
            # message_stop. Waiting on it needs no guess about wording or timing.
            deadline = 180_000  # a tool round plus a written answer
            try:
                page.wait_for_function(
                    """() => {
                        const log = document.querySelector('.ld-messages');
                        return log && log.getAttribute('aria-busy') !== 'true';
                    }""",
                    timeout=deadline,
                )
                finished = True
            except PlaywrightTimeout:
                finished = False
            check(finished, "the turn finished within 180s",
                  "" if finished else "still busy — the panel never cleared aria-busy")

            # A moment for the final animation frame to paint the last delta.
            page.wait_for_timeout(500)
            answer = panel.inner_text() if panel.count() else page.inner_text("body")

            page.screenshot(path=str(ARTEFACTS / "04-answer.png"), full_page=False)

            check(len(answer) > len(args.question) + 40, "assistant produced a response",
                  f"{len(answer)} chars in the panel")
            # The answer must be grounded in REAL metadata — and must say so in
            # English. This assertion used to look for `rpt_lab_orders`, which
            # was right when the copilot was schema-literate and is now exactly
            # backwards: the hard rules say never to show a column or table name
            # to a user without a plain-English label, and the catalogue exists
            # so the model has one.
            labelled = [
                label for label in ("Laboratory orders", "Patient encounters",
                                    "Health facilities", "Where patients come from",
                                    "Diabetes screening")
                if label.lower() in answer.lower()
            ]
            check(bool(labelled), "answer names a REAL dataset by its friendly label",
                  f"named: {labelled or 'none — possible hallucination or error'}")

            raw = [name for name in ("rpt_lab_orders", "rpt_encounters", "rpt_facilities",
                                     "rpt_patient_geography", "rpt_diabetes_screening")
                   if name in answer]
            check(not raw, "the executive is never shown a raw table name",
                  f"leaked: {raw}" if raw else "")

            # Grounded in the projected metadata rather than invented: these are
            # saved metrics that exist on dataset 3 in this Superset right now.
            grounded = [m for m in ("malaria positivity", "lab completion rate",
                                    "average turnaround")
                        if m in answer.lower()]
            check(bool(grounded), "answer names real saved metrics",
                  f"named: {grounded or 'none'}")
            check("credit balance" not in answer.lower(), "no Anthropic billing error")

            # The copy the user reads on first open used to say "This copilot
            # reads; it does not change anything yet" — the product telling
            # people it could not do the thing it does.
            check(
                "does not change anything yet" not in answer,
                "the intro does not claim the copilot is read-only",
            )

            # The panel keeps a `raw` accumulator per bubble and re-parses it
            # every frame, so a finished answer must have become real block
            # elements rather than a single text node.
            structure = page.evaluate(
                """() => {
                    const body = document.querySelector('.ld-msg--assistant .ld-msg__body');
                    if (!body) return null;
                    return {
                        children: body.children.length,
                        tags: Array.from(body.children).map((n) => n.tagName.toLowerCase()),
                        attributes: Array.from(body.querySelectorAll('*'))
                            .flatMap((n) => Array.from(n.attributes).map((a) => a.name)),
                    };
                }"""
            )
            if check(structure is not None, "assistant bubble rendered through markdown.js"):
                check(structure["children"] > 0, "answer rendered as block elements",
                      f"tags: {structure['tags'][:8]}")
                # Model output is untrusted and this page holds the user's
                # Superset session, so nothing rendered may carry an attribute.
                check(not structure["attributes"], "no attribute on any rendered node",
                      f"found: {structure['attributes'][:5]}")

            excerpt = answer[-1200:].replace("\n\n", "\n")
            print("\n--- panel transcript (tail) ---")
            print(excerpt)
            print("--- end ---")

        # --- 5. Diagnostics ----------------------------------------------
        print("\n-- browser diagnostics --")
        # Superset itself emits console noise on every page load that has
        # nothing to do with the copilot: a Scarf telemetry pixel that its own
        # CSP blocks, and a service-worker script it does not ship. Filtering
        # these keeps the check meaningful rather than permanently red.
        # The bare "bad HTTP response code (404) ... fetching the script" line is
        # the SAME service-worker failure, logged a second time without naming
        # the script. Verified: a full login flow produces no non-2xx responses
        # at all, so nothing else is 404ing.
        superset_noise = (
            "scarf.sh",
            "service-worker",
            "ServiceWorker",
            "bad HTTP response code (404) was received when fetching the script",
        )
        real_errors = [
            e for e in console_errors
            if e.startswith("error") and not any(n in e for n in superset_noise)
        ]
        check(not real_errors, "no console errors attributable to the copilot",
              "; ".join(real_errors[:4]) if real_errors else "")
        panel_404s = [r for r in failed_requests if "lifedata-ai" in r]
        check(not panel_404s, "no failed copilot requests",
              "; ".join(panel_404s[:3]) if panel_404s else "")

        (ARTEFACTS / "console.json").write_text(
            json.dumps({"console": console_errors, "failed": failed_requests}, indent=2),
            encoding="utf-8",
        )
        context.close()
        browser.close()

    passed = sum(1 for ok, _, _ in results if ok)
    print(f"\n=== {passed} passed, {len(results) - passed} failed ===")
    for ok, label, detail in results:
        if not ok:
            print(f"  FAILED: {label}  {detail}")
    print(f"\nScreenshots: {ARTEFACTS}")
    return 0 if passed == len(results) else 1


# --- pytest entry point ---------------------------------------------------
#
# `main()` above stays exactly as it is, so `python tests_browser/test_panel_smoke.py`
# and `demo.py` keep working. This wrapper is what makes the same checks run
# under pytest, which they never did: the directory was excluded from
# `testpaths` and the `superset_live` marker was unregistered, so the ONLY
# end-to-end coverage of the panel was dead code.
#
# `live_stack` skips unless LIFEDATA_LIVE=1 and the stack answers, so a
# developer running `pytest` on a laptop sees a clean result rather than a
# confusing failure.


def test_the_panel_answers_a_real_question(live_stack: str) -> None:
    """A person who logs in gets a working copilot that answers with real metadata."""
    exit_code = main([])

    failures = [f"{label} {detail}".strip() for ok, label, detail in results if not ok]
    assert exit_code == 0, _report(failures)


def _report(failures: list[str]) -> str:
    """Every failed check in the assertion message, not just the first.

    `main()` prints as it goes, which is right when a human is watching it. Under
    pytest the printed output is captured and the assertion message is what
    lands in CI, so the failures have to travel in it.
    """
    return "\n  ".join(["checks failed:", *failures])


if __name__ == "__main__":
    sys.exit(main())
