import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("dashboard_eval", Path(__file__).resolve().parents[2] / "evals/dashboard_quality/run.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_tool_coverage_does_not_claim_visual_or_numerical_quality():
    case = {"required_tools": ["publish_dashboard"]}
    events = [{"event": "tool_result", "data": {"name": "publish_dashboard", "ok": True}},
              {"event": "published_dashboard", "data": {"publication_id": "abc"}}]
    score = runner.score_trace(case, events)
    assert score["tool_coverage"]["status"] == "pass"
    assert score["dimensions"]["visual_quality"]["status"] == "unverified"
    assert score["overall"] != "pass"


def test_evidence_needs_reviewer_and_artifact_and_errors_fail_coverage():
    case = {"required_tools": []}
    score = runner.score_trace(case, [{"event": "error", "data": {"message": "failure"}}],
                               {"visual_quality": {"status": "pass"}})
    assert score["tool_coverage"]["status"] == "fail"
    assert score["dimensions"]["visual_quality"]["status"] == "unverified"


def test_publication_build_id_satisfies_automatic_begin():
    case = {"required_tools": ["begin_dashboard_build", "publish_dashboard"]}
    events = [{"event": "tool_result", "data": {"name": "publish_dashboard", "ok": True}},
              {"event": "published_dashboard", "data": {"build_id": "actual-build"}}]
    assert runner.score_trace(case, events)["tool_coverage"]["status"] == "pass"
