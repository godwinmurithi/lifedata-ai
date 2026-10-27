"""Executable browser regression for immutable master preview and live adapter.

All live responses are explicit regression fixtures, never client facts.
"""
import json
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "lifedata.settings")
import django
django.setup()
from lifedata_ai.reference_templates import build_reference_document, MASTER, MASTER_SHA256
from hashlib import sha256

ID = "11111111-1111-4111-8111-111111111111"


def main():
    assert sha256(MASTER.read_bytes()).hexdigest() == MASTER_SHA256
    bindings = {
        "residents": {"chart_ids": [1], "title": "Regression fixture population", "encoding": {"metrics": ["population"], "chart_type": "kpi", "number_format": ",d"}},
        "vital_events": {"chart_ids": [2, 3], "title": "Fixture events", "encoding": {"dimension": "month", "metrics": ["count"], "series": "kind", "time": True, "chart_type": "line"}},
        "clusters": {"chart_ids": [4], "encoding": {"metrics": ["clusters"], "chart_type": "kpi"}},
    }
    preview = build_reference_document()
    live = build_reference_document(ID, "Regression live binding fixture", bindings)
    seen = []
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))

        def route(route):
            parsed = urlparse(route.request.url)
            path = parsed.path
            if path == "/preview/":
                return route.fulfill(body=preview, content_type="text/html")
            if path == "/live/":
                return route.fulfill(body=live, content_type="text/html")
            if "/static/" in path:
                asset = ROOT / "lifedata_ai/static" / path.split("/static/", 1)[1]
                if asset.exists():
                    return route.fulfill(body=asset.read_bytes(), content_type="application/javascript" if asset.suffix == ".js" else "text/css")
            if path.endswith("/filter-options/"):
                return route.fulfill(json={"filters": [{"field": "country", "label": "Country", "values": ["Fixture country"], "truncated": False}]})
            if "/charts/" in path:
                chart_id = int(path.rstrip("/").split("/")[-1])
                seen.append({"id": chart_id, "criteria": parse_qs(parsed.query).get("criteria", ["{}"])[0]})
                if chart_id == 1:
                    payload = {"rows": [{"population": 4321}], "columns": ["population"], "form_data": {"viz_type": "big_number_total"}}
                elif chart_id == 4:
                    payload = {"rows": [{"clusters": 9}], "columns": ["clusters"], "form_data": {"viz_type": "big_number_total"}}
                else:
                    payload = {"rows": [{"month": "2026-01-01", "kind": "Births", "count": 11}, {"month": "2026-01-01", "kind": "Deaths", "count": 3}, {"month": "2026-02-01", "kind": "Births", "count": 12}], "columns": ["month", "kind", "count"], "form_data": {"viz_type": "echarts_timeseries_line", "groupby": ["month", "kind"]}}
                payload["row_limit"] = 1000
                return route.fulfill(json=payload)
            return route.fulfill(body="", status=200)

        page.route("**/*", route)
        page.goto("http://vima.test/preview/", wait_until="domcontentloaded")
        expect(page.get_by_text("Interactive preview", exact=False)).to_be_visible()
        expect(page.get_by_role("button", name="Overview", exact=True)).to_be_visible()
        page.get_by_role("button", name="Overview", exact=True).click()
        expect(page.get_by_role("heading", name="Vital events by month", exact=True)).to_be_visible()
        assert page.locator("body").inner_text().find("{{") == -1
        for name in ["Sentinel Surveillance", "Geospatial Intelligence"]:
            # Supplied source owns tab spelling; use its actual rendered labels.
            buttons = page.locator('nav[aria-label="Dashboard tabs"] button')
            buttons.nth(1 if "Sentinel" in name else 2).click()
        page.goto("http://vima.test/live/", wait_until="domcontentloaded")
        expect(page.locator('[data-reference-slot="residents"]')).to_contain_text("4,321")
        expect(page.locator('[data-reference-slot="households"]')).to_contain_text("Not available")
        assert "Mutukula" not in page.locator("body").inner_text()
        page.get_by_role("tab", name="Chart 3", exact=True).click()
        page.wait_for_function("document.querySelector('[data-reference-slot=vital_events] canvas') !== null")
        assert any(item["id"] == 3 for item in seen)
        page.get_by_label("Country", exact=False).select_option("Fixture country")
        expect(page.locator('[data-reference-slot="residents"]')).to_contain_text("4,321")
        assert any(json.loads(item["criteria"]).get("country") == ["Fixture country"] for item in seen)
        page.get_by_role("button", name="Supporting detail", exact=True).click()
        expect(page.locator('[data-reference-slot="housing"]')).to_be_visible()
        page.get_by_role("button", name="Geospatial intelligence", exact=True).click()
        expect(page.locator('[data-reference-slot="clusters"]')).to_contain_text("9")
        page.get_by_role("button", name="Sentinel surveillance", exact=True).click()
        expect(page.locator('[data-reference-slot="sentinel_sites"]')).to_contain_text("Not available")
        assert not errors, errors
        print(json.dumps({"status": "passed", "master_sha256": MASTER_SHA256, "live_queries": len(seen), "checks": ["interactive exact master preview", "no illustrative facts in live mode", "saved-query bindings", "shared chart tabs", "scoped filters", "all three reference views", "no browser errors"]}))
        browser.close()


if __name__ == "__main__":
    main()
