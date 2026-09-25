"""Fixtures for the browser tests.

These hold the **only** coverage of the write path end to end, and until now
nothing ran them: `testpaths` excluded the directory, the files were `def main()`
scripts, and the `superset_live` marker was unregistered. They are collected now,
and this file is what stops them failing noisily on a machine with no stack up.

**They must never run inside the django container.** The panel is injected by
nginx and has to be exercised through the same origin a real user sees, with a
real Superset session cookie. Running them container-side would test a page
nobody visits.

    uv run pytest                     # unit + markdown; no stack needed
    uv run pytest -m superset_live    # against docker compose, opt-in
"""

from __future__ import annotations

import os
from time import sleep
from urllib.error import URLError
from urllib.request import urlopen

import pytest

#: Where the stack answers, through nginx — same origin as the panel.
BASE_URL = os.environ.get("LIFEDATA_BASE_URL", "http://localhost:8080")

#: The opt-in. Absent, `superset_live` tests skip with a reason rather than
#: failing, so a developer running `pytest` on a laptop sees a clean result.
LIVE_FLAG = "LIFEDATA_LIVE"


def pytest_collection_modifyitems(config, items):
    """Mark everything in this directory `superset_live` unless it says otherwise.

    The markdown tests are the exception: they inject `markdown.js` into a blank
    page and need neither Superset nor the orchestrator. Defaulting the *rest* to
    `superset_live` means a new browser test is opted out of the default run
    automatically, rather than breaking it for everyone who has no stack running.
    """
    for item in items:
        if "tests_browser" not in str(item.fspath):
            continue
        if item.get_closest_marker("live_model"):
            continue
        if "test_markdown" in item.fspath.basename:
            continue
        item.add_marker(pytest.mark.superset_live)


@pytest.fixture(scope="session")
def live_stack() -> str:
    """The base URL of a running stack, or skip.

    Three separate reasons to skip, each reported distinctly — "the stack is
    down" and "you did not opt in" are different problems and a single message
    for both sends people to the wrong place.
    """
    if os.environ.get(LIVE_FLAG) != "1":
        pytest.skip(
            f"set {LIVE_FLAG}=1 to run the browser tests against a live stack "
            "(docker compose -f deploy/docker-compose.yml up -d)"
        )

    try:
        with urlopen(f"{BASE_URL}/lifedata-ai/api/health/", timeout=5) as response:
            if response.status != 200:
                pytest.skip(f"{BASE_URL} health check returned {response.status}")
    except URLError as exc:
        pytest.skip(f"no stack at {BASE_URL}: {exc}")

    _wait_for_panel_assets()
    return BASE_URL


def _wait_for_panel_assets(attempts: int = 20) -> None:
    """Wait until Django is serving the panel bundle again.

    `runserver` autoreloads on any Python edit, and Django is what serves
    `panel.js` — so for a second or two after a code change the script 404s or
    hangs and the panel never boots. A browser test started in that window fails
    with "panel did not initialise", which looks exactly like a panel bug.

    This cost two full acceptance runs before it was written down. The health
    endpoint is not enough on its own: it comes back before the static handler
    is ready.
    """
    for _attempt in range(attempts):
        try:
            with urlopen(
                f"{BASE_URL}/lifedata-ai/static/panel/panel.js", timeout=5
            ) as response:
                if response.status == 200 and len(response.read()) > 1000:
                    return
        except URLError:
            pass
        sleep(1)
    pytest.skip(
        f"{BASE_URL} is not serving panel.js after {attempts}s — the orchestrator "
        "may still be reloading after a code change"
    )


@pytest.fixture(scope="session")
def chromium():
    """A Chromium browser, or skip with the command that installs it."""
    playwright = pytest.importorskip(
        "playwright.sync_api", reason="playwright is not installed"
    )
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - the message is the point
            pytest.skip(
                f"could not launch Chromium ({exc}). "
                "Run: uv run playwright install chromium"
            )
        yield browser
        browser.close()
