"""Authenticated dev-server rehearsal; verifies actual Superset cohort counts."""

import json
import os
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

LIBRARIES = "/tmp/lifedata-browser-libs/extracted/usr/lib/x86_64-linux-gnu"
if Path(LIBRARIES).exists():
    os.environ["LD_LIBRARY_PATH"] = LIBRARIES + ":" + os.environ.get("LD_LIBRARY_PATH", "")


def main():
    manifest = json.loads(Path("/tmp/lifedata-population-build.json").read_text())
    session = json.loads(Path("/tmp/lifedata-author-session.json").read_text())
    original = json.loads(Path("/tmp/lifedata-population-source.json").read_text())
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 716})
        context.add_cookies(
            [
                {
                    "name": "session",
                    "value": session["session"],
                    "domain": "sandbox.lifehealth.app",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                }
            ]
        )
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_default_timeout(60000)
        for attempt in range(4):
            response = page.goto("https://sandbox.lifehealth.app" + manifest["publication"]["url"])
            if response and response.status < 500:
                break
            page.wait_for_timeout(4000)
        count = page.get_by_test_id("population-matches")
        expect(count).to_have_text("299", timeout=120000)
        expect(page.get_by_label("Selected facility", exact=True)).to_have_value("")
        expect(page.locator(".pd-facility-marker")).to_have_count(47)
        assert page.evaluate("document.documentElement.scrollHeight <= innerHeight + 1"), (
            "Desktop requires page scrolling"
        )
        for selector in [
            ".sb-people",
            ".sb-blood",
            ".sb-donor",
            ".pd-funnel",
            ".pd-map-card",
            ".pd-records",
            ".sb-vaccines",
            ".sb-hematology",
            ".sb-systemic",
        ]:
            box = page.locator(selector).bounding_box()
            assert box and box["y"] >= 0 and box["y"] + box["height"] <= 717, (selector, box)
        print(
            "PASS initial population: 299, all 47 facilities, all panels in one viewport",
            flush=True,
        )
        page.screenshot(path="/tmp/lifedata-population-discovery.png", full_page=True)
        selections = [
            ("Willing to donate blood", "Yes", "blood_donor"),
            ("Blood group", "O+", "blood_group"),
            ("HIV screening", "NEGATIVE", "hiv_status"),
            ("Hepatitis B antigen screening", "Negative", "s_hbsag"),
            ("Hepatitis C screening", "Negative", "s_hcv"),
            ("Syphilis screening", "No", "s_syphillis"),
        ]
        page.get_by_role("button", name="All available selectors", exact=True).click()
        rows = original
        for label, value, field in selections:
            start = time.monotonic()
            page.get_by_role("dialog").get_by_label(label, exact=True).first.select_option(value)
            rows = [r for r in rows if r[field] == value]
            expect(count).to_have_text(str(len(rows)), timeout=120000)
            print(
                f"PASS {label}: {len(rows)} matches in {time.monotonic() - start:.2f}s", flush=True
            )
        page.get_by_role("dialog").get_by_label("Age (recorded years) minimum").first.fill("18")
        page.get_by_role("dialog").get_by_label("Age (recorded years) maximum").first.fill("65")
        rows = [r for r in rows if r["age"] is not None and 18 <= r["age"] <= 65]
        expect(count).to_have_text(str(len(rows)), timeout=120000)
        expect(page.get_by_test_id("population-stage-0").locator("strong")).to_have_text("299")
        expect(page.get_by_test_id("population-stage-1").locator("strong")).to_have_text("214")
        expect(page.get_by_test_id("population-stage-2").locator("strong")).to_have_text("64")
        print("PASS stage counts remain sequential when later criteria are applied", flush=True)
        page.keyboard.press("Escape")
        page.locator(".sb-blood").get_by_role("tab", name="Sickle cell", exact=True).click()
        expect(count).to_have_text(str(len(rows)))
        expect(
            page.locator(".sb-blood").get_by_role("tab", name="Sickle cell", exact=True)
        ).to_have_attribute("aria-selected", "true")
        expect(page.locator(".pd-records")).to_be_visible()
        expect(count).to_have_text(str(len(rows)))
        person = page.locator(".pd-records tbody tr button").first
        person.click()
        expect(page.get_by_role("dialog")).to_be_visible()
        expect(page.get_by_role("dialog").get_by_text("Loading permitted detail…")).to_have_count(
            0, timeout=120000
        )
        expect(
            page.get_by_role("dialog").get_by_role("heading", name="Screening", exact=True)
        ).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog")).to_have_count(0)
        expect(page.locator(".pd-map")).to_be_visible()
        page.screenshot(path="/tmp/lifedata-population-filtered.png", full_page=True)
        page.get_by_role("button", name="Reset all", exact=True).click()
        expect(count).to_have_text("299", timeout=120000)
        page.get_by_label("Subcounty", exact=True).select_option("SSI BUKUNJA")
        expect(count).to_have_text("299", timeout=120000)
        page.get_by_label("Parish", exact=True).select_option("LUGALA")
        expected = sum(r["parish"] == "LUGALA" for r in original)
        expect(count).to_have_text(str(expected), timeout=120000)
        page.get_by_role("button", name="Reset all", exact=True).click()
        expect(count).to_have_text("299", timeout=120000)
        page.get_by_role("button", name="All available selectors", exact=True).click()
        page.get_by_label("Find a selector", exact=True).fill("Haemoglobin")
        expect(
            page.get_by_role("dialog").get_by_label("Haemoglobin (recorded value) minimum")
        ).to_be_visible()
        page.keyboard.press("Escape")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
            "Mobile horizontal overflow"
        )
        page.screenshot(path="/tmp/lifedata-population-mobile.png", full_page=True)
        page.set_viewport_size({"width": 1600, "height": 716})
        page.locator(".sb-blood").get_by_role("tab", name="Bld grp", exact=True).click()
        expect(page.locator(".sb-blood svg")).to_be_visible(timeout=120000)
        page.locator(".sb-blood").get_by_role("button", name="O+: 91 people", exact=True).click()
        expect(count).to_have_text("91", timeout=120000)
        page.get_by_role("link", name="Superset ↗").click()
        page.wait_for_url("**/superset/dashboard/**", timeout=60000)
        print("PASS custom criteria handed off to native Superset", flush=True)
        page.wait_for_timeout(15000)
        page.screenshot(path="/tmp/lifedata-population-superset.png", full_page=True)
        assert not errors, errors
        browser.close()
    print(
        "PASS live discovery, sequential stages, matching profile/list/detail, geography, mobile, and Superset handoff",
        flush=True,
    )


if __name__ == "__main__":
    main()
