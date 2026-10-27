"""Read-only staging verification using an existing authorised browser session."""

import json
from pathlib import Path

import requests


def main():
    credentials = json.loads(Path("/tmp/lifedata-author-session.json").read_text())
    session = requests.Session()
    session.cookies.set(
        "session", credentials["session"], domain="sandbox.lifehealth.app", path="/"
    )
    base = "https://sandbox.lifehealth.app/lifedata-ai/"
    response = session.get(base + "api/boundaries/?coverage=1", timeout=45)
    response.raise_for_status()
    report = response.json()
    assert report["countries_ready"] == 54
    ready = [r for r in report["layers"] if r["status"] == "ready"]
    kenya = next(r for r in ready if r["country_iso3"] == "KEN" and r["admin_level"] == 1)
    result = session.get("https://sandbox.lifehealth.app" + kenya["geometry_url"], timeout=45)
    result.raise_for_status()
    geo = result.json()
    assert geo["type"] == "FeatureCollection" and len(geo["features"]) == kenya["features"]
    assert geo["reference"]["release_id"] == kenya["release_id"] and geo["reference"]["attribution"]
    page = session.get(base + "boundaries/", timeout=45)
    page.raise_for_status()
    assert "African boundary catalogue" in page.text
    preview = session.get(base + 'dashboard-design-preview/', timeout=15)
    preview.raise_for_status()
    image = session.get(base + 'static/previews/dashboard-design-20261020/adaptive-navy-teal-desktop.png', timeout=15)
    image.raise_for_status()
    assert image.content.startswith(b'\x89PNG')
    anonymous = requests.get(base + "api/boundaries/", allow_redirects=False, timeout=30)
    assert anonymous.status_code in (302, 401, 403)
    print(
        json.dumps(
            {
                "live_catalogue": "ok",
                "countries": report["countries_ready"],
                "ready_layers": len(ready),
                "missing_core_levels": report["missing_core_levels"],
                "kenya_counties": len(geo["features"]),
                "catalogue_page_status": page.status_code,
                "anonymous_status": anonymous.status_code,
            }
        )
    )


if __name__ == "__main__":
    main()
