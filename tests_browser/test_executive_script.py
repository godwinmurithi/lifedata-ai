"""The acceptance test: four sentences, no schema words (plan §11).

    uv run python tests_browser/test_executive_script.py --headed --slow

This is the one that decides whether the ease-of-use work landed. The other
browser suites prove the copilot can talk and can write; this proves it is usable
by the person it was built for.

**The rule is the point: no dataset names, no column names, no chart types, no
ids anywhere in what is typed.** If the script needs technical vocabulary to
work, ease of use is not fixed, whatever the unit suite says.

    1. "Build me a dashboard showing how our labs are performing"
    2. "Show me where our patients come from"
    3. "Actually make that first chart bigger"
    4. "Undo that"

Then LOOK AT THE DASHBOARD. That is not optional and not a formality: the
Superset API accepts almost anything, and three separate defects in this project
— a missing chart-to-dashboard relation, an unregistered `bar` viz type, and
duplicate column labels — all returned 2xx and appeared only in a screenshot.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

BASE = "http://localhost:8080"
USERNAME = "admin"
PASSWORD = "lifedata-admin-dev"
ARTEFACTS = Path(__file__).resolve().parent / "artefacts"
COMPOSE = [
    "docker", "compose", "--env-file", ".env",
    "-f", "deploy/docker-compose.yml", "exec", "-T", "django",
    "uv", "run", "--no-sync", "python",
]

#: Text Superset renders when a chart is saved but broken. Every one of these has
#: appeared on a copilot-built dashboard at least once, and every one returned a
#: 2xx from the API that created it.
BROKEN_MARKERS = (
    "Data error",
    "There is no chart definition associated with this component",
    "is not registered",
    "Unexpected error",
)

#: Vocabulary an executive would not use. Asserted against what the COPILOT says,
#: not only against what we type — an answer full of identifiers is a failure of
#: the same kind as a prompt that requires them.
SCHEMA_WORDS = (
    "rpt_lab_orders", "rpt_encounters", "rpt_facilities",
    "rpt_patient_geography", "rpt_diabetes_screening",
    "facility_name", "district_name", "lab_order_id", "patient_id",
    "turnaround_hours", "encounter_id", "ordered_at",
    "big_number_total", "echarts_timeseries_bar", "echarts_timeseries_line",
    "groupby", "x_axis", "viz_type", "time_grain_sqla",
)

results: list[tuple[bool, str, str]] = []


def say(text: str = "") -> None:
    """Print and flush.

    This script takes minutes and someone is usually watching it. Python buffers
    stdout when it is not attached to a terminal — piped to a file or a CI log,
    nothing appears until the process exits, so a run in progress is
    indistinguishable from a run that has hung.
    """
    print(text, flush=True)


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((bool(ok), label, detail))
    say(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n        {detail}" if detail else ""))
    return bool(ok)


def django_shell(code: str) -> str:
    proc = subprocess.run(
        [*COMPOSE, "manage.py", "shell", "-c", code],
        capture_output=True, text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    return (proc.stdout or proc.stderr).strip()


def superset_json(path: str) -> dict:
    """Read something from Superset through the orchestrator container."""
    out = django_shell(
        "import httpx,json;"
        "c=httpx.Client(base_url='http://superset:8088',timeout=30);"
        "t=c.post('/api/v1/security/login',json={'username':'admin','password':"
        "'lifedata-admin-dev','provider':'db','refresh':True}).json()['access_token'];"
        f"print('<<<'+json.dumps(c.get('{path}',headers={{'Authorization':f'Bearer {{t}}'}})"
        ".json())+'>>>')"
    )
    match = re.search(r"<<<(.*)>>>", out, re.S)
    return json.loads(match.group(1)) if match else {}


# --- Driving the panel ----------------------------------------------------


def settle(page, timeout_s: int = 300) -> None:
    """Wait until the turn genuinely ends.

    `aria-busy` on the message list, which the panel sets for the duration of a
    turn and clears at message_stop. Polling until the transcript stops growing
    does NOT work: the status strip ticks "Working · 5s" -> "Working · 6s",
    which is the same length.
    """
    try:
        page.wait_for_function(
            """() => {
                const log = document.querySelector('.ld-messages');
                return log && log.getAttribute('aria-busy') !== 'true';
            }""",
            timeout=timeout_s * 1000,
        )
    except PlaywrightTimeout:
        check(False, f"a turn finished within {timeout_s}s", "still busy")
    # One more frame, so the last delta is painted before anything is read.
    page.wait_for_timeout(700)


def drive(page, panel, message: str, *, label: str) -> str:
    """Send one sentence and see it through to the end.

    A single executive request is not a single turn. It can pause for a
    clarification, and again for an Apply — each of which ENDS the turn and
    resumes when the user acts. This loop is what a person does: answer what is
    asked, approve what is proposed, until the copilot is quiet.

    The loop is bounded. An unbounded one would hang for ever on a copilot that
    kept proposing, which is a failure worth seeing rather than waiting through.
    """
    say(f"\n-- {label} --\n    > {message}")
    box = page.locator("textarea").last
    box.click()
    box.fill(message)
    box.press("Enter")
    settle(page)

    asked = 0
    applied = 0
    for _ in range(12):
        if page.locator(".ld-ask__option:not([disabled])").count():
            asked += 1
            _answer_clarification(page)
            settle(page)
            continue
        if page.locator(".ld-action__apply:not([disabled])").count():
            applied += 1
            page.locator(".ld-action__apply:not([disabled])").first.click()
            _check_busy_on_apply(page, applied)
            settle(page)
            continue
        break

    if asked:
        say(f"    (answered {asked} clarification card{'s' if asked > 1 else ''})")
    if applied:
        say(f"    (approved {applied} proposal{'s' if applied > 1 else ''})")
    return panel.inner_text()


#: Reported once, however many proposals a run approves — this is a property of
#: the panel, not of any particular Apply, and thirty identical PASS lines would
#: bury the interesting output.
_busy_on_apply_checked = False


def _check_busy_on_apply(page, applied: int) -> None:
    """Pressing Apply must mark the panel busy in the SAME tick as the click.

    Applying an envelope is several writes against Superset, and the panel used
    to stay visibly idle for all of them — busy was only set once the turn
    resumed afterwards. So `settle()` returned immediately, `drive()` moved on,
    and the next sentence hit the in-flight guard:

        The copilot is still working on the previous message in this
        conversation. Wait for it to finish, or press Stop.

    An acceptance run lost an entire dashboard to that race. A real executive
    would have hit it by typing slightly too soon, which is not a thing to ask
    people not to do. Asserted with no wait at all: a `wait_for_function` here
    would pass on the old code too, by sleeping through the window.
    """
    global _busy_on_apply_checked
    busy = page.evaluate(
        "() => document.querySelector('.ld-messages')?.getAttribute('aria-busy')"
    )
    if _busy_on_apply_checked:
        return
    _busy_on_apply_checked = True
    check(
        busy == "true",
        "pressing Apply marks the panel busy immediately",
        # Only when it FAILS. `check` prints the detail either way, so passing
        # the explanation unconditionally puts a sentence describing a race
        # underneath a PASS, which reads like a warning nobody can act on.
        ""
        if busy == "true"
        else f"aria-busy was {busy!r} on proposal {applied} — the composer would "
        "accept a message that then races the resume",
    )


def _answer_clarification(page) -> None:
    """Choose the first option of every question, then Continue if it is a set.

    Choosing the first option is arbitrary and that is fine: the property under
    test is that the copilot ASKS in plain English and then builds what it was
    told, not that any particular answer is preferred.
    """
    groups = page.locator(".ld-ask:not(.is-answered) .ld-ask__group")
    count = max(groups.count(), 1)
    for index in range(count):
        option = groups.nth(index).locator(".ld-ask__option:not([disabled])").first
        if option.count():
            option.click()
            page.wait_for_timeout(200)
    proceed = page.locator(".ld-ask__continue:not([disabled])")
    if proceed.count():
        proceed.first.click()


def schema_words_in(text: str) -> list[str]:
    return sorted({word for word in SCHEMA_WORDS if word in text})


def schema_leak_context(text: str, width: int = 90) -> str:
    """Where each schema word appears, not just that it does.

    "leaked: ['turnaround_hours']" is not actionable: the panel text this is
    matched against covers the whole panel — the transcript AND the action rail —
    and the words are matched as substrings, so `avg_turnaround_hours` reports as
    `turnaround_hours`. Whether the copilot said it, or a preview card rendered
    it, or it is a saved metric's own name inside an otherwise English sentence,
    are three different problems with three different fixes.
    """
    fragments: list[str] = []
    for word in schema_words_in(text):
        index = text.find(word)
        start = max(0, index - width // 2)
        excerpt = text[start:index + len(word) + width // 2]
        fragments.append(f"{word}: …{' '.join(excerpt.split())}…")
    return "\n        ".join(fragments)


# --- The script -----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--slow", action="store_true")
    args = parser.parse_args(argv)
    results.clear()
    ARTEFACTS.mkdir(exist_ok=True)

    say(f"\n=== The executive script, against {BASE} ===")
    # IDS, not just the count. The dashboard inspected at the end must be the one
    # step 1 built — see `_dashboard_built_in_step_one`.
    ids_before = _dashboard_ids()
    dashboards_before = len(ids_before)
    say(f"dashboards before: {dashboards_before}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed, slow_mo=250 if args.slow else 0)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()

        # `start()` is wrapped in log-and-render-nothing so a copilot defect
        # never presents as a Superset defect — which means a bootstrap failure
        # is now SILENT in the page. Capture the console, or a broken panel and
        # a merely slow one look identical from here.
        console: list[str] = []
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}")
                if m.type in ("error", "warning") else None)

        page.goto(f"{BASE}/login/", wait_until="domcontentloaded")
        page.fill("#username", USERNAME)
        page.fill("#password", PASSWORD)
        page.click("button[type='submit']")
        page.wait_for_load_state("networkidle", timeout=60_000)

        try:
            page.wait_for_function(
                "() => window.__lifedataCopilot !== undefined", timeout=60_000
            )
            booted = True
        except PlaywrightTimeout:
            booted = False
            page.screenshot(path=str(ARTEFACTS / "exec-0-bootstrap-failed.png"))
        if not check(booted, "panel bootstrapped",
                     "" if booted else f"url={page.url} console={console[:4]}"):
            # Nothing below can mean anything without a panel, and a cascade of
            # secondary failures would bury the one that matters.
            return summarise()

        page.evaluate("window.__lifedataCopilot.open()")
        page.wait_for_timeout(800)
        panel = page.locator("[role='complementary'], aside, [class*='lifedata']").first
        check(panel.is_visible(), "panel open")

        # --- 1 -----------------------------------------------------------
        answer = drive(
            page, panel,
            "Build me a dashboard showing how our labs are performing",
            label="build a dashboard",
        )
        page.screenshot(path=str(ARTEFACTS / "exec-1-dashboard-built.png"))

        ids_after = _dashboard_ids()
        built = sorted(ids_after - ids_before)
        check(bool(built), "a dashboard was created",
              f"count {dashboards_before} -> {len(ids_after)}")

        leaked = schema_words_in(answer)
        check(not leaked, "the copilot answered without schema vocabulary",
              schema_leak_context(answer) if leaked else "")

        # --- 2 -----------------------------------------------------------
        geography = drive(
            page, panel,
            "Show me where our patients come from",
            label="patient geography",
        )
        page.screenshot(path=str(ARTEFACTS / "exec-2-geography.png"))
        check(
            not re.search(r"scatter|heat ?map|hex|point map", geography, re.I)
            or "district" in geography.lower(),
            "patient geography stayed at district level",
            geography[-200:],
        )

        # --- 3 -----------------------------------------------------------
        resized = drive(
            page, panel,
            "Actually make that first chart bigger",
            label="resize",
        )
        page.screenshot(path=str(ARTEFACTS / "exec-3-resized.png"))
        check("position_json" not in resized,
              "the layout change was expressed semantically, not as raw JSON")

        # --- 4 -----------------------------------------------------------
        undone = drive(page, panel, "Undo that", label="undo")
        page.screenshot(path=str(ARTEFACTS / "exec-4-undone.png"))
        low = undone.lower()
        check(any(word in low for word in ("undo", "undone", "reverted", "restored",
                                           "put back", "changed back")),
              "the copilot engaged with the undo request",
              undone[-200:])

        # --- LOOK AT THE DASHBOARD ---------------------------------------
        dashboard = _dashboard_built_in_step_one(built)
        if check(bool(dashboard), "the new dashboard is findable",
                 str(dashboard.get("dashboard_title", ""))):
            _inspect_dashboard(page, dashboard)

        # --- the audit trail ---------------------------------------------
        audit = django_shell(
            "from lifedata_ai.models import ActionRecord;"
            "from collections import Counter;"
            "print(Counter(ActionRecord.objects.order_by('-created_at')[:25]"
            ".values_list('action_type',flat=True)))"
        )
        say(f"\n  actions this session: {audit.splitlines()[-1] if audit else '-'}")

        (ARTEFACTS / "executive-script.json").write_text(
            json.dumps({"dashboard": dashboard, "audit": audit}, indent=2, default=str),
            encoding="utf-8",
        )
        context.close()
        browser.close()

    return summarise()


def summarise() -> int:
    passed = sum(1 for ok, _, _ in results if ok)
    say(f"\n=== {passed} passed, {len(results) - passed} failed ===")
    for ok, label, detail in results:
        if not ok:
            say(f"  FAILED: {label}  {detail}")
    say(f"\nScreenshots: {ARTEFACTS}")
    return 0 if passed == len(results) else 1


def _dashboard_ids() -> set[int]:
    listing = superset_json("/api/v1/dashboard/?q=(page_size:100)")
    return {row["id"] for row in (listing.get("result") or []) if row.get("id")}


def _dashboard_built_in_step_one(built: list[int]) -> dict:
    """The dashboard step 1 CREATED, not whichever one changed most recently.

    This used to take the newest by `changed_on`, which is a different thing.
    Steps 2 to 4 also touch dashboards — a geography chart gets put on one, a
    tile is resized and then put back — so by the time the inspection ran, the
    most-recently-changed dashboard was frequently some older two-chart thing
    the copilot had merely visited.

    Everything worth checking then ran against the wrong target: the layout
    check, the filter check, and above all the "no widget came up empty" check
    that exists to catch silently broken charts. One run reported a clean pass
    over a dashboard the script had never asked for.

    Diffing ids before and after step 1 says exactly which one it built.
    """
    if not built:
        return {}
    # The last id if there are several: `create_dashboard` comes first and any
    # later one is a second thought, not the dashboard under test.
    detail = superset_json(f"/api/v1/dashboard/{built[-1]}")
    result = detail.get("result") or {}
    result.setdefault("id", built[-1])
    return result


def _inspect_dashboard(page, dashboard: dict) -> None:
    """Open it and judge it the way a person would.

    Screenshot first, then read the rendered text. The structural checks below
    are worth having, but the screenshot is the artefact that settles arguments.
    """
    dashboard_id = dashboard.get("id")
    page.goto(f"{BASE}/superset/dashboard/{dashboard_id}/", wait_until="domcontentloaded")
    # Superset renders widgets asynchronously and each runs its own query.
    page.wait_for_timeout(20_000)
    page.screenshot(path=str(ARTEFACTS / "exec-5-the-dashboard.png"), full_page=True)

    body = page.inner_text("body")
    broken = [marker for marker in BROKEN_MARKERS if marker in body]
    check(not broken, "every widget rendered", f"found: {broken}" if broken else "")

    # SEPARATE from BROKEN_MARKERS, deliberately. "No data" is not inherently a
    # defect — a filter that matches nothing says it too, and honestly. It is a
    # defect *here*: this dashboard was built seconds ago, over seeded data, by a
    # copilot that chose its own metrics and filters. An empty widget means it
    # built something that cannot answer the question it was asked.
    #
    # This check exists because a run went 13/13 green over a dashboard whose
    # three headline tiles all read "No data". Every marker above was absent,
    # the layout was right, the filter was there, cross-filters were on. The
    # tiles were saved with `metrics` when the viz reads `metric`, so they drew
    # nothing and reported nothing. Only the screenshot showed it.
    empty = body.count("No data")
    check(not empty, "no widget came up empty",
          f'{empty} widget(s) show "No data" on a dashboard built from seeded data'
          if empty else "")

    ugly = [expr for expr in ("COUNT_DISTINCT(", "AVG(", "SUM(") if expr in body]
    check(not ugly, "no raw aggregate expression is shown to the user",
          f"found: {ugly}" if ugly else "")

    detail = superset_json(f"/api/v1/dashboard/{dashboard_id}")
    result = detail.get("result") or {}
    layout = json.loads(result.get("position_json") or "{}")
    charts = [n for n in layout.values()
              if isinstance(n, dict) and n.get("type") == "CHART"]
    check(bool(charts), "the dashboard has a layout with chart nodes",
          f"{len(charts)} chart node(s)")

    metadata = json.loads(result.get("json_metadata") or "{}")
    check(metadata.get("cross_filters_enabled") is not False,
          "cross-filtering is enabled")
    filters = metadata.get("native_filter_configuration") or []
    check(bool(filters), "the dashboard has a filter",
          f"{len(filters)} filter(s)" if filters else "none — every dashboard gets a date filter")

    say(f"\n  dashboard: {result.get('dashboard_title')} (id {dashboard_id})")
    say(f"  charts in layout: {len(charts)}, filters: {len(filters)}")


# --- pytest entry point ---------------------------------------------------


def test_the_executive_script(live_stack: str) -> None:
    """Four sentences, no schema words, and a dashboard worth looking at."""
    exit_code = main([])

    failures = [f"{label} {detail}".strip() for ok, label, detail in results if not ok]
    assert exit_code == 0, "\n  ".join(["checks failed:", *failures])


if __name__ == "__main__":
    sys.exit(main())
