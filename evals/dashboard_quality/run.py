#!/usr/bin/env python3
"""Capture actual VIMA chat traces; evidence scoring never invents visual success."""
import argparse
import json
import os
import time
from pathlib import Path

import httpx

DIMENSIONS = ("data_correctness", "visual_quality", "reference_fidelity", "interactions")


def score_trace(case, events, evidence=None):
    """Tool coverage is mechanical; independent evidence is scored separately."""
    evidence = evidence or {}
    completed = {e["data"].get("name") for e in events
                 if e.get("event") == "tool_result" and e["data"].get("ok")}
    missing = sorted(set(case["required_tools"]) - completed)
    errors = [e["data"] for e in events if e.get("event") == "error"]
    publications = [e["data"] for e in events if e.get("event") == "published_dashboard"]
    # Publication creates its persisted build automatically. Requiring a second
    # explicit begin tool call would falsely fail the intended workflow.
    if any(p.get("build_id") for p in publications):
        missing = [name for name in missing if name != "begin_dashboard_build"]
    stop = [e["data"].get("stop_reason") for e in events if e.get("event") == "message_stop"]
    dimensions = {}
    for dimension in DIMENSIONS:
        item = evidence.get(dimension, {})
        # Evidence must name a reviewable artifact and identify who assessed it.
        valid = item.get("artifact") and item.get("reviewer") and item.get("status") in ("pass", "fail")
        dimensions[dimension] = item if valid else {"status": "unverified"}
    return {"tool_coverage": {"status": "pass" if not missing and not errors else "fail",
                              "missing": missing, "errors": errors},
            "publication_count": len(publications), "stop_reasons": stop,
            "dimensions": dimensions,
            "overall": "pass" if not missing and not errors and publications and
                       all(x["status"] == "pass" for x in dimensions.values()) else "unverified_or_failed"}


def chat(client, base, prompt, cookie, origin):
    events = []
    with client.stream("POST", base + "/lifedata-ai/api/chat/",
                       json={"message": prompt, "page_context": {"path": "/"}},
                       headers={"Cookie": "session=" + cookie, "Origin": origin}, timeout=300) as response:
        response.raise_for_status()
        name = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                name = line[7:].strip()
            elif line.startswith("data: "):
                events.append({"event": name, "data": json.loads(line[6:])})
    return events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", default="general-monitoring")
    parser.add_argument("--dataset-id", type=int)
    parser.add_argument("--live", action="store_true", help="Makes one real, billed VIMA conversation")
    parser.add_argument("--trace", type=Path, help="Score a previously captured trace without model calls")
    parser.add_argument("--evidence", type=Path, help="Independent reviewed artifact manifest")
    parser.add_argument("--out", type=Path, default=Path("/tmp/vima-dashboard-eval"))
    args = parser.parse_args()
    cases = json.loads(Path(__file__).with_name("cases.json").read_text())
    case = next((c for c in cases if c["id"] == args.case), None)
    if case is None:
        parser.error("Unknown case")
    if args.live and args.trace:
        parser.error("Choose --live or --trace")
    if not args.live and not args.trace:
        print(json.dumps({"status": "not_run", "cases": cases}, indent=2))
        return 0
    if args.live:
        cookie = os.environ.get("LIFEDATA_EVAL_SESSION_COOKIE", "")
        if not cookie or not args.dataset_id:
            parser.error("Live execution requires --dataset-id and LIFEDATA_EVAL_SESSION_COOKIE")
        base = os.environ.get("LIFEDATA_EVAL_BASE_URL", "http://localhost:8080").rstrip("/")
        with httpx.Client(follow_redirects=False) as client:
            events = chat(client, base, case["prompt"].format(dataset_id=args.dataset_id), cookie,
                          os.environ.get("LIFEDATA_EVAL_ORIGIN", "https://sandbox.lifehealth.app"))
    else:
        events = json.loads(args.trace.read_text())["events"]
    evidence = json.loads(args.evidence.read_text()) if args.evidence else {}
    report = {"case": case, "captured_at": time.time(), "events": events,
              "score": score_trace(case, events, evidence)}
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / (case["id"] + ".json")
    path.write_text(json.dumps(report, indent=2))
    print(json.dumps({"report": str(path), "score": report["score"]}, indent=2))
    return 0 if report["score"]["overall"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
