"""Browser regression for adaptive layouts, scoped filters and safe map popups.

Uses clearly labelled demonstration data, not production query results.
"""

import base64
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = Path("/home/geonode/vima-session-proposal-2026-10-20")
ID = "11111111-1111-4111-8111-111111111111"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="
)


def main():
    OUT.mkdir(exist_ok=True)
    errors = []
    seen = []
    publication = {
        "id": ID,
        "title": "Monitoring overview · demonstration",
        "theme": "light",
        "can_edit": True,
        "dashboard_id": None,
        "charts": [
            {"id": 1, "title": "Assessment submissions", "viz_type": "big_number_total"},
            {"id": 2, "title": "Monthly reporting", "viz_type": "echarts_timeseries_line"},
            {"id": 3, "title": "Coverage by country", "viz_type": "echarts_timeseries_bar"},
            {"id": 4, "title": "Reporting detail", "viz_type": "table"},
        ],
        "layout": [
            {"chart_id": 1, "span": 12, "height": 200},
            {"chart_id": 2, "span": 6, "height": 360},
            {"chart_id": 3, "span": 6, "height": 360},
            {"chart_id": 4, "span": 12, "height": 320},
        ],
        "configuration": {
            "preset": "composed-dashboard",
            "source_chart_id": 1,
            "chart_ids": [1, 2, 3, 4],
            "composition": {
                "purpose": "monitoring",
                "subtitle": "Browser preview • illustrative data • shared filters",
                "skin": "navy-teal",
                "filters": [{"field": "country", "label": "Country", "chart_ids": [1, 2, 3, 4]}],
                "sections": [
                    {"title": "At a glance", "chart_ids": [1]},
                    {"title": "Trends and coverage", "chart_ids": [2, 3]},
                    {"title": "Supporting evidence", "chart_ids": [4]},
                ],
            },
            "design": {
                "baseline": "vima-adaptive-v1",
                "colors": {"accent": "#087f8c"},
                "palette": ["#087f8c", "#4167a4", "#7863a6"],
            },
        },
    }
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1050})
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(route):
            parsed = urlparse(route.request.url)
            path = parsed.path
            if path.endswith("/countries/uganda.geojson"):
                return route.fulfill(
                    json={
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "properties": {"centroid": [33.04, 0.45]},
                                "geometry": {
                                    "type": "Polygon",
                                    "coordinates": [
                                        [
                                            [33, 0.4],
                                            [33.08, 0.4],
                                            [33.08, 0.5],
                                            [33, 0.5],
                                            [33, 0.4],
                                        ]
                                    ],
                                },
                            }
                        ],
                    }
                )
            if "/static/" in path:
                asset = ROOT / "lifedata_ai/static" / path.split("/static/", 1)[1]
                if asset.exists():
                    return route.fulfill(
                        body=asset.read_bytes(),
                        content_type="text/css"
                        if asset.suffix == ".css"
                        else "application/javascript",
                    )
            if path == f"/lifedata-ai/api/publications/{ID}/":
                return route.fulfill(json=publication)
            if path.endswith("/filter-options/"):
                return route.fulfill(
                    json={
                        "filters": [
                            {
                                "field": "country",
                                "label": "Country",
                                "chart_ids": [1, 2, 3, 4],
                                "values": ["Kenya", "Uganda"],
                                "scope_count": 4,
                                "truncated": False,
                            }
                        ]
                    }
                )
            if "/charts/" in path:
                criteria = json.loads(parse_qs(parsed.query).get("criteria", ["{}"])[0])
                seen.append(criteria)
                n = 320 if criteria.get("country") == ["Uganda"] else 1248
                identifier = int(path.rstrip("/").split("/")[-1])
                rows = (
                    [{"count": n}]
                    if identifier == 1
                    else [
                        {"month": m, "count": v}
                        for m, v in zip(
                            ["Apr", "May", "Jun", "Jul", "Aug", "Sep"],
                            [110, 146, 189, 205, 267, 331],
                            strict=True,
                        )
                    ]
                    if identifier == 2
                    else [{"country": "Kenya", "count": 928}, {"country": "Uganda", "count": 320}]
                )
                return route.fulfill(
                    json={
                        "rows": rows,
                        "columns": list(rows[0]),
                        "row_limit": 200,
                        "form_data": {
                            "x_axis": "month" if identifier == 2 else "country",
                            "metrics": ["count"],
                            "groupby": [],
                        },
                    }
                )
            if path.endswith("/workspace/"):
                sites = [
                    {
                        "facility_name": "Demonstration clinic",
                        "facility_level": "HC III",
                        "beds": 12,
                        "lat": 0.43,
                        "lng": 33.02,
                        "county": "Area A",
                        "subcounty": "Zone A",
                        "ownership": "Public",
                        "ready": "Yes",
                    },
                    {
                        "facility_name": "Demonstration hospital",
                        "facility_level": "Hospital",
                        "beds": 60,
                        "lat": 0.46,
                        "lng": 33.05,
                        "county": "Area A",
                        "subcounty": "Zone B",
                        "ownership": "Private",
                        "ready": "No",
                    },
                ]
                return route.fulfill(
                    json={
                        "rows": [{**site, "country": "Uganda"} for site in sites],
                        "total": 2,
                        "matches": 2,
                        "sites": 2,
                        "countries": 1,
                        "options": {
                            "country": ["Uganda"],
                            "facility_name": [r["facility_name"] for r in sites],
                            "ready": ["Yes", "No"],
                        },
                        "breakdowns": {
                            "ready": [{"ready": "Yes", "people": 1}, {"ready": "No", "people": 1}]
                        },
                        "series": {},
                        "stages": [{"label": "Current records", "count": 2}],
                        "truncated": False,
                        "admin_hierarchy": [],
                        "geography": {
                            "facilities": sites,
                            "all_facilities": sites,
                            "boundaries": [],
                            "counts": {},
                            "notes": ["Demonstration coordinates; no live surveillance data."],
                            "truncated": False,
                            "source_chart_ids": [1],
                        },
                    }
                )
            if parsed.hostname != "vima.test":
                return route.fulfill(body=PNG, content_type="image/png")
            if path.startswith("/lifedata-ai/api/"):
                return route.fulfill(json={})
            return route.fulfill(
                body=f'<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/lifedata-ai/static/dashboards/app.css"><script defer src="/lifedata-ai/static/dashboards/app.js"></script></head><body><div id="lifedata-dashboard" data-publication="{ID}"></div></body></html>',
                content_type="text/html",
            )

        page.route("**/*", route)
        url = f"http://vima.test/lifedata-ai/dashboards/{ID}/"
        page.goto(url)
        expect(page.locator(".vima-adaptive")).to_be_visible()
        expect(page.get_by_label("Country", exact=True)).to_be_visible()
        expect(page.locator(".chart-card")).to_have_count(4)
        page.get_by_label("Country", exact=True).select_option("Uganda")
        expect(page.locator(".card-body").first).to_contain_text("320")
        assert any(c.get("country") == ["Uganda"] for c in seen)
        page.get_by_role("button", name="Reset filters").click()
        expect(page.locator(".card-body").first).to_contain_text("1,248")
        page.screenshot(path=str(OUT / "adaptive-navy-teal-desktop.png"), full_page=True)
        publication["configuration"]["composition"]["skin"] = "warm"
        page.reload()
        expect(page.locator(".skin-warm")).to_be_visible()
        page.screenshot(path=str(OUT / "adaptive-warm-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.reload()
        expect(page.locator(".chart-card")).to_have_count(4)
        assert page.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1")
        page.screenshot(path=str(OUT / "adaptive-mobile.png"), full_page=True)
        publication["configuration"] = {
            "preset": "buikwe-workspace",
            "source_chart_id": 1,
            "facility_chart_id": 1,
            "workspace": {
                "display_title": "Map styling · demonstration",
                "selectors": [],
                "site_field": "facility_name",
                "country_field": "",
                "record_columns": ["facility_name", "ready"],
                "panels": [
                    {
                        "slot": "blood",
                        "title": "Readiness",
                        "tabs": [{"field": "ready", "label": "Recorded response"}],
                    }
                ],
                "workflow": [],
            },
            "map_style": {
                "basemap": "street",
                "markers": {
                    "shape": "icon",
                    "icon": "hospital",
                    "radius": 12,
                    "outline_color": "#ffffff",
                    "category_field": "ownership",
                    "categories": {
                        "Public": {"color": "#087f8c"},
                        "Private": {"color": "#7863a6", "shape": "diamond", "icon": "clinic"},
                    },
                },
                "popup": {
                    "title_field": "facility_name",
                    "show_select": False,
                    "fields": [
                        {"field": "ownership", "label": "Ownership"},
                        {"field": "beds", "label": "Recorded beds", "format": "number"},
                        {"field": "ready", "label": "Readiness"},
                    ],
                },
            },
        }
        page.set_viewport_size({"width": 1440, "height": 1050})
        page.reload()
        expect(page.locator(".vima-symbol-marker")).to_have_count(2)
        page.locator(".vima-symbol-marker").first.click()
        popup = page.locator(".leaflet-popup-content")
        expect(popup).to_contain_text("Demonstration clinic")
        expect(popup).to_contain_text("Ownership")
        expect(popup).to_contain_text("Recorded beds")
        expect(popup).to_contain_text("12")
        expect(popup.get_by_role("button", name="Select facility")).to_have_count(0)
        expect(page.locator(".pd-map-legend")).to_contain_text("Public")
        page.screenshot(path=str(OUT / "map-popup-and-icons-desktop.png"), full_page=True)
        # A VIMA map edit refreshes presentation in place and retains selections.
        page.get_by_role("combobox").first.select_option("Demonstration clinic")
        publication["updated_at"] = "2026-10-20T12:00:00Z"
        publication["configuration"]["map_style"]["markers"]["shape"] = "pin"
        publication["configuration"]["map_style"]["popup"]["fields"][1]["label"] = (
            "Capacity <img src=x>"
        )
        page.evaluate("window.__retainedDocument=true")
        page.evaluate(
            '(id)=>window.dispatchEvent(new CustomEvent("lifedata:publication-updated",{detail:{publication_id:id}}))',
            ID,
        )
        expect(page.locator(".vima-symbol-marker svg path").first).to_have_attribute(
            "d", "M32 61C23 47 9 35 9 25a23 23 0 1 1 46 0c0 10-14 22-23 36z"
        )
        expect(page.get_by_role("combobox").first).to_have_value("Demonstration clinic")
        page.locator(".vima-symbol-marker").first.click()
        expect(page.locator(".leaflet-popup-content")).to_contain_text("Capacity <img src=x>")
        expect(page.locator(".leaflet-popup-content img")).to_have_count(0)
        assert page.evaluate("window.__retainedDocument") is True
        # SINCEP's existing country-centroid map uses the same safe style controls.
        config = publication["configuration"]
        config.pop("facility_chart_id")
        config["workspace"]["country_field"] = "country"
        config["map_style"]["popup"]["fields"] = [
            {"field": "country", "label": "Recorded country"},
            {"field": "ownership", "label": "Ownership"},
            {"field": "location_basis", "label": "Placement basis"},
        ]
        page.reload()
        expect(page.locator(".workspace-site-marker")).to_have_count(2)
        page.locator(".workspace-site-marker").first.dispatch_event("click")
        expect(page.locator(".leaflet-popup-content")).to_contain_text("Recorded country")
        expect(page.locator(".leaflet-popup-content")).to_contain_text("Uganda")
        expect(page.locator(".leaflet-popup-content")).to_contain_text(
            "not actual site coordinates"
        )
        page.screenshot(path=str(OUT / "country-map-popup-and-icons.png"), full_page=True)
        assert not errors, errors
        browser.close()
    print(
        "PASS: desktop/mobile layouts, scoped filters, both skins, icons/popups/legend, safe text and in-place edits retaining filters; no browser errors."
    )


if __name__ == "__main__":
    main()
