"""One actual VIMA UI conversation. Requires explicit --run-live (billed).

Run inside the browser-ready app container with --session-stdin; pipe the
existing authorized session JSON from the host without printing its contents.
No pending actions are automatically approved and existing artifacts must not
be changed by this task.
"""

import argparse
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


PROMPT = """Create a NEW dashboard titled 'VIMA quality workflow verification' using dataset 230 and existing suitable saved charts that you discover yourself. Do not modify any existing chart or publication. Show source-backed resident and household counts plus useful age and sex comparisons, with related views in reusable tabbed chart stacks and appropriate filters. Confirm entity grain, missingness and metric denominators; use full aggregates rather than sampled totals. Make the desktop view fit a 1440x900 viewport with readable labels and provide a usable mobile view. Use your persistent design brief, inspect the actual rendered dashboard, independently reconcile its headline counts where possible, repair findings, and complete its build only after checks pass. If a required saved chart does not exist, report that limitation without changing existing charts. You may publish this new dashboard. Preserve all existing publications unchanged."""

CAPTURE = """(() => {
 window.__vimaQualityCapture = [];
 const originalFetch = window.fetch.bind(window);
 window.fetch = async (...args) => {
   const response = await originalFetch(...args);
   const url = String(args[0] && args[0].url || args[0]);
   if (url.includes('/lifedata-ai/api/chat/')) {
     const trace = {status: response.status, text: '', done: false};
     window.__vimaQualityCapture.push(trace);
     (async () => {
       try {
         const reader = response.clone().body.getReader();
         const decoder = new TextDecoder();
         while (true) {
           const part = await reader.read();
           if (part.done) break;
           trace.text += decoder.decode(part.value, {stream: true});
         }
         trace.text += decoder.decode();
       } catch (error) { trace.error = String(error); }
       finally { trace.done = true; }
     })();
   }
   return response;
 };
})();"""


def parse_events(text):
    events = []
    name = None
    for line in text.splitlines():
        if line.startswith("event: "):
            name = line[7:].strip()
        elif line.startswith("data: "):
            try:
                events.append({"event": name, "data": json.loads(line[6:])})
            except ValueError:
                events.append({"event": "malformed_event", "data": {"line": line[:300]}})
    return events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--session-stdin", action="store_true")
    parser.add_argument("--session-file", type=Path, default=Path("/tmp/lifedata-author-session.json"))
    parser.add_argument("--base-url", default=os.environ.get("LIFEDATA_EVAL_BASE_URL", "https://sandbox.lifehealth.app"))
    parser.add_argument("--out", type=Path, default=Path("/tmp/vima-quality-conversation"))
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    if not args.run_live:
        print(json.dumps({"status": "not_run", "prompt": PROMPT, "requires": "--run-live makes one billed VIMA conversation"}, indent=2))
        return 0
    session = json.load(sys.stdin) if args.session_stdin else json.loads(args.session_file.read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    base = args.base_url.rstrip("/")
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1440, "height": 900}, ignore_https_errors=False)
        context.add_cookies([{"name": "session", "value": session["session"],
                              "url": base, "secure": urlparse(base).scheme == "https", "httpOnly": True}])
        context.add_init_script(CAPTURE)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_default_timeout(60000)
        response = page.goto(base + "/lifedata-ai/dashboards/", wait_until="domcontentloaded")
        if response is None or response.status != 200:
            raise RuntimeError("Dashboard library did not load successfully")
        page.wait_for_function("Boolean(window.__lifedataCopilot)")
        page.evaluate("window.__lifedataCopilot.newConversation(); window.__lifedataCopilot.open()")
        page.locator("#lifedata-ai-input").fill(PROMPT)
        page.locator(".ld-send").click()
        started = time.monotonic()
        while time.monotonic() - started < args.timeout:
            captures = page.evaluate("window.__vimaQualityCapture")
            if captures and captures[-1].get("done"):
                break
            if int(time.monotonic() - started) % 30 == 0:
                print(json.dumps({"status": "waiting", "elapsed_seconds": int(time.monotonic() - started)}), flush=True)
            page.wait_for_timeout(1000)
        captures = page.evaluate("window.__vimaQualityCapture")
        events = [event for capture in captures for event in parse_events(capture.get("text", ""))]
        conversation_id = page.evaluate("window.__lifedataCopilot.conversationId()")
        page.screenshot(path=str(args.out / "conversation.png"), full_page=True)
        transcript = page.locator(".ld-messages").inner_text()
        (args.out / "transcript.txt").write_text(transcript)
        answer = "".join(e["data"].get("text", "") for e in events if e["event"] == "text_delta")
        publications = [e["data"] for e in events if e["event"] == "published_dashboard"]
        report = {"prompt": PROMPT, "conversation_id": conversation_id, "events": events,
                  "answer": answer, "publications": publications, "browser_errors": errors,
                  "elapsed_seconds": round(time.monotonic() - started, 1),
                  "stream_finished": bool(captures and captures[-1].get("done")),
                  "pending_actions": page.locator(".ld-actions").inner_text(),
                  "quality_dimensions": {name: "unverified" for name in
                      ("data_correctness", "visual_quality", "reference_fidelity", "interactions")}}
        (args.out / "trace.json").write_text(json.dumps(report, indent=2))
        for index, publication in enumerate(publications[-1:]):
            url = publication.get("url")
            if not url and publication.get("publication_id"):
                url = "/lifedata-ai/dashboards/" + publication["publication_id"] + "/"
            if not url:
                continue
            dashboard = context.new_page()
            dashboard.goto(url if url.startswith("http") else base + url, wait_until="networkidle")
            dashboard.screenshot(path=str(args.out / f"dashboard-{index}-desktop.png"), full_page=True)
            dashboard.set_viewport_size({"width": 390, "height": 844})
            dashboard.wait_for_timeout(1000)
            dashboard.screenshot(path=str(args.out / f"dashboard-{index}-mobile.png"), full_page=True)
        browser.close()
    print(json.dumps({"report": str(args.out / "trace.json"), "conversation_id": conversation_id,
                      "stream_finished": report["stream_finished"], "publication_count": len(publications),
                      "browser_errors": errors, "elapsed_seconds": report["elapsed_seconds"]}, indent=2))
    return 0 if report["stream_finished"] and publications and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
