"""Every allowlisted viz type, rendered from exactly what the pipeline writes.

This is the test that would have caught the KPI bug, and it is the reason it
exists. Three `big_number_total` tiles on an acceptance dashboard read "No data"
while everything around them rendered perfectly. The API had accepted every
write. The unit tests passed — they asserted the payload was well-formed, and it
was. Querying the tile's own definition by hand returned 14,355 rows.

The tile was saved with `metrics: ["orders"]`; `big_number_total` builds its
query from the singular `metric`. Nothing was malformed. A plugin was reading a
key nobody had set, and there is no layer between us and the browser that could
have noticed.

So this test closes the loop the only way it can be closed: take the real
`_to_superset_form_data` output for each allowlisted viz, save a chart with it,
open it in Explore, and read the page. `pie` turned out to have the same defect
and was found this way.

**When a viz type is added to `VIZ_ALLOWLIST`, it gets a case here.** The list
below is checked against that constant (validation/schemas/actions.py), so
forgetting is a failing test rather than a silently untested chart type.

    uv run pytest -m superset_live tests_browser/test_viz_renders.py
    uv run python tests_browser/test_viz_renders.py --headed
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ARTEFACTS = Path(__file__).resolve().parent / "artefacts"
BASE = os.environ.get("LIFEDATA_BASE_URL", "http://localhost:8080")
USERNAME = "admin"
PASSWORD = "lifedata-admin-dev"

#: The lab orders dataset. Every case below uses it so that a rendering failure
#: is about the viz type and not about the data.
LAB_ORDERS = 3

#: Minimal params per viz — what an executive's first request produces. Keyed by
#: viz type and checked for completeness against `viz_allowlist`.
CASES: dict[str, dict] = {
    "big_number_total": {"metrics": ["orders"]},
    "pie": {"groupby": ["test_type"], "metrics": ["orders"], "row_limit": 10},
    "echarts_timeseries_line": {"x_axis": "ordered_at", "metrics": ["avg_turnaround_hours"]},
    "echarts_timeseries_bar": {"x_axis": "facility_name", "metrics": ["orders"]},
    "table": {"groupby": ["facility_name"], "metrics": ["orders"]},
    # The Uganda choropleth. Its failure mode is the quietest of the lot: given
    # district NAMES instead of ISO codes it draws all 112 districts and fills
    # none, so the page carries no error text at all and the map is simply
    # blank. `district_iso` is supplied by the pipeline, never by the model.
    "country_map": {"groupby": ["district_name"], "metrics": ["orders"], "row_limit": 200},
}

#: Coloured districts a working Uganda map must show. The seed has 12 districts
#: with data and a healthy map scores 13 (the legend swatch counts); a map whose
#: join failed scores 0. Five leaves room for a thinner dataset without leaving
#: room for a blank map.
FILLED_SHAPE_MIN = 5

#: Text Superset shows instead of a chart. "Add required control values" is the
#: one that mattered: it is what a `pie` with no `metric` says, and it appears
#: nowhere in the dashboard-level marker list because a dashboard never shows it.
NOT_RENDERED = (
    "No data",
    "Add required control values",
    "Data error",
    "is not registered",
    "There is no chart definition",
    "Unexpected error",
)


def _compose(*args: str) -> list[str]:
    return [
        "docker", "compose", "--env-file", ".env",
        "-f", "deploy/docker-compose.yml", "exec", "-T", "django",
        "uv", "run", "--no-sync", "python", *args,
    ]


def _django_shell(code: str) -> str:
    """Both streams, always.

    `stdout or stderr` hides the traceback whenever the shell prints anything at
    all — and it always does: "9 objects imported automatically". A failing probe
    then reports only that notice, which says nothing about what went wrong.
    """
    proc = subprocess.run(
        _compose("manage.py", "shell", "-c", code),
        capture_output=True, text=True, cwd=ROOT,
    )
    return f"{proc.stdout}\n{proc.stderr}".strip()


def pipeline_form_data() -> dict[str, dict]:
    """Ask the ORCHESTRATOR for the form_data it would write.

    Inside the container deliberately: that is the code that runs in production,
    and a host-side import would test a copy that may not even load (the host has
    no GDAL). It also means the saved metrics and the viz allowlist are the
    real ones.
    """
    out = _django_shell(
        "import json;"
        "from lifedata_ai.tools.superset_write import _to_superset_form_data as f;"
        f"cases = {json.dumps(CASES)};"
        f"print('@@' + json.dumps({{v: f(dict(p, viz_type=v, time_range='Last 12 months'), {LAB_ORDERS}) "
        "for v, p in cases.items()}))"
    )
    marker = out.rfind("@@")
    if marker < 0:
        raise AssertionError(f"could not read form_data from the orchestrator:\n{out}")
    return json.loads(out[marker + 2:].splitlines()[0])


def allowlisted_viz() -> list[str]:
    out = _django_shell(
        "from lifedata_ai.validation.schemas import VIZ_ALLOWLIST;"
        "print('@@' + ','.join(VIZ_ALLOWLIST))"
    )
    marker = out.rfind("@@")
    if marker < 0:
        raise AssertionError(f"could not read viz_allowlist:\n{out}")
    return [v for v in out[marker + 2:].splitlines()[0].split(",") if v]


# --- Superset, via the API, as an admin ----------------------------------


class _Superset:
    """Just enough client to create and delete a chart."""

    def __init__(self) -> None:
        import http.cookiejar
        import urllib.request

        self._urllib = urllib.request
        jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
        token = self._call("POST", "/api/v1/security/login", {
            "username": USERNAME, "password": PASSWORD,
            "provider": "db", "refresh": True,
        })["access_token"]
        self._headers = {"Authorization": f"Bearer {token}"}
        self._headers["X-CSRFToken"] = self._call(
            "GET", "/api/v1/security/csrf_token/"
        )["result"]

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        request = self._urllib.Request(
            BASE + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", **getattr(self, "_headers", {})},
        )
        return json.load(self._opener.open(request))

    def create_chart(self, name: str, viz_type: str, form: dict) -> int:
        return self._call("POST", "/api/v1/chart/", {
            "slice_name": name, "viz_type": viz_type,
            "datasource_id": LAB_ORDERS, "datasource_type": "table",
            "params": json.dumps(form),
        })["id"]

    def delete_chart(self, chart_id: int) -> None:
        try:
            self._call("DELETE", f"/api/v1/chart/{chart_id}")
        except Exception:  # noqa: BLE001 - cleanup must not mask a real failure
            pass


# --- The test ------------------------------------------------------------


def count_filled_shapes(page) -> int:
    """District shapes carrying a choropleth colour.

    A map that fails its join is not an error state: it draws all 112 Ugandan
    districts, colours none, and puts no text on the page to assert against. The
    rendered SVG is the only honest signal.

    Two refinements, both learned by measuring rather than reasoning. The first
    version of this counted every `svg path` on the page and scored a
    deliberately broken map as healthy — the toolbar's icons are paths too, 32 of
    them at `rgba(0, 0, 0, 0.88)`, which swamped the signal. So: only inside the
    chart container, and only fills with actual chroma, because the unmatched
    districts and the chrome are all greys.

    Measured on this build — working map **13**, name-keyed map **0**.
    """
    return page.evaluate(
        """() => {
            const paths = Array.from(document.querySelectorAll(
                '.slice_container svg path, [data-test="chart-container"] svg path'));
            let coloured = 0;
            for (const p of paths) {
                const m = getComputedStyle(p).fill.match(/rgba?\\((\\d+), (\\d+), (\\d+)/);
                if (!m) continue;
                const r = +m[1], g = +m[2], b = +m[3];
                if (Math.max(r, g, b) - Math.min(r, g, b) > 12) coloured++;
            }
            return coloured;
        }"""
    )


@pytest.fixture(scope="module")
def forms(live_stack) -> dict[str, dict]:  # noqa: ARG001 - ordering dependency
    return pipeline_form_data()


def test_every_allowlisted_viz_has_a_case(live_stack) -> None:  # noqa: ARG001
    """A viz type nobody renders here is a viz type nobody has checked."""
    missing = sorted(set(allowlisted_viz()) - set(CASES))
    assert not missing, (
        f"{missing} are allowlisted but have no rendering case. Add one — the KPI "
        "tiles were broken for exactly as long as nothing looked at them."
    )


@pytest.mark.parametrize("viz_type", sorted(CASES))
def test_the_chart_actually_renders(live_stack, chromium, forms, viz_type) -> None:  # noqa: ARG001
    """Save it the way the pipeline would, then open it and read the page."""
    form = forms[viz_type]
    superset = _Superset()
    chart_id = superset.create_chart(f"VIZCHECK {viz_type}", viz_type, form)
    try:
        page = chromium.new_page(viewport={"width": 1200, "height": 700})
        # 60s rather than the 30s default. Explore loads the whole chart bundle,
        # and on a busy stack that overruns — which surfaces as a navigation
        # timeout and reads exactly like a broken chart. Two runs failed this
        # way on two DIFFERENT charts, which is what gave the flakiness away.
        page.set_default_navigation_timeout(60_000)
        try:
            page.goto(f"{BASE}/login/")
            page.fill("#username", USERNAME)
            page.fill("#password", PASSWORD)
            page.click("input[type=submit], button[type=submit]")
            page.wait_for_load_state("networkidle")
            page.goto(f"{BASE}/explore/?slice_id={chart_id}")
            # Each chart runs its own query; Explore renders the controls first.
            page.wait_for_timeout(11_000)
            ARTEFACTS.mkdir(exist_ok=True)
            page.screenshot(path=str(ARTEFACTS / f"viz-{viz_type}.png"))
            body = page.inner_text("body")
            filled = count_filled_shapes(page)
        finally:
            page.close()
    finally:
        superset.delete_chart(chart_id)

    found = [marker for marker in NOT_RENDERED if marker in body]
    assert not found, (
        f"{viz_type} did not render from the form_data the pipeline writes: {found}\n"
        f"form_data was: {json.dumps(form)}\n"
        f"See tests_browser/artefacts/viz-{viz_type}.png"
    )

    if viz_type == "country_map":
        assert filled >= FILLED_SHAPE_MIN, (
            "the Uganda map drew its boundaries but coloured no district — the join "
            f"from the data to the map failed. entity was {form.get('entity')!r}; it "
            "must be the ISO 3166-2 code column, not the district name.\n"
            "See tests_browser/artefacts/viz-country_map.png"
        )


# --- Also runnable directly ----------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args(argv)

    from playwright.sync_api import sync_playwright

    forms_by_viz = pipeline_form_data()
    missing = sorted(set(allowlisted_viz()) - set(CASES))
    if missing:
        print(f"FAIL  allowlisted but unchecked: {missing}")

    failures = list(missing)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1200, "height": 700})
        page.goto(f"{BASE}/login/")
        page.fill("#username", USERNAME)
        page.fill("#password", PASSWORD)
        page.click("input[type=submit], button[type=submit]")
        page.wait_for_load_state("networkidle")

        superset = _Superset()
        for viz_type, form in forms_by_viz.items():
            chart_id = superset.create_chart(f"VIZCHECK {viz_type}", viz_type, form)
            try:
                page.goto(f"{BASE}/explore/?slice_id={chart_id}")
                page.wait_for_timeout(11_000)
                ARTEFACTS.mkdir(exist_ok=True)
                page.screenshot(path=str(ARTEFACTS / f"viz-{viz_type}.png"))
                found = [m for m in NOT_RENDERED if m in page.inner_text("body")]
            finally:
                superset.delete_chart(chart_id)
            if found:
                failures.append(viz_type)
            print(f"{'FAIL' if found else 'PASS'}  {viz_type}"
                  + (f"\n        {found}" if found else ""))
        browser.close()

    print(f"\n=== {len(forms_by_viz) - len(failures)} passed, {len(failures)} failed ===")
    print(f"Screenshots: {ARTEFACTS}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
