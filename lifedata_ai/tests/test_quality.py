"""Quality approvals are real evidence tied to a permitted, exact dashboard revision."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lifedata_ai import quality
from lifedata_ai.agent.openai_provider import response_input
from lifedata_ai.models import Conversation, DashboardPublication
from lifedata_ai.tools.base import ToolError
from lifedata_ai.tools.registry import ToolResult


@pytest.fixture
def build_context(db):
    publication = DashboardPublication.objects.create(
        owner_username="author",
        title="Test dashboard",
        configuration={"chart_ids": [1]},
        layout=[{"chart_id": 1, "span": 12, "height": 300}],
    )
    client = Mock()
    client.get_chart.return_value = {
        "id": 1,
        "params": {"metrics": ["count"]},
        "query_context": {"queries": []},
    }
    conversation = Conversation.objects.create(superset_username="author")
    context = SimpleNamespace(
        username="author", conversation=conversation, superset_client=lambda: client
    )
    return context, publication


def passing_review(publication):
    return {
        "automated_passed": True,
        "findings": [],
        "publication_fingerprint": quality.current_fingerprint(publication),
    }


def test_approval_invalidates_when_layout_or_saved_query_changes(build_context):
    context, publication = build_context
    build = quality.start_build(context, publication, {"audience": "programme lead"})
    build.status = "inspected"
    build.review = passing_review(publication)
    build.fingerprint = quality.current_fingerprint(publication, context.superset_client())
    build.save()
    result = quality.finalize_build(
        context,
        build,
        {
            "passed": True,
            "summary": "Both screenshot sizes are readable and show the required charts.",
        },
    )
    assert result["status"] == "approved"
    assert quality.pending_builds(context) == []
    context.superset_client().get_chart.return_value["params"]["metrics"] = ["different_count"]
    assert quality.pending_builds(context)[0]["status"] == "stale"
    publication.layout[0]["height"] = 400
    publication.save()
    assert quality.quality_summary(publication)["status"] == "stale"


def test_no_approval_without_inspection_and_visual_evidence(build_context):
    context, publication = build_context
    build = quality.start_build(context, publication)
    with pytest.raises(ToolError, match="Inspect"):
        quality.finalize_build(context, build, {"passed": True, "summary": "Looks good"})
    build.status = "inspected"
    build.review = passing_review(publication)
    build.fingerprint = quality.current_fingerprint(publication, context.superset_client())
    build.save()
    with pytest.raises(ToolError, match="screenshots"):
        quality.finalize_build(context, build, {"passed": False, "summary": "Labels are clipped"})


def test_revision_restore_preserves_original_snapshot(build_context):
    context, publication = build_context
    original = {
        "title": publication.title,
        "layout": publication.layout,
        "configuration": publication.configuration,
        "theme": publication.theme,
    }
    build = quality.start_build(context, publication, previous_snapshot=original)
    publication.title = "Draft edit"
    publication.save()
    build = quality.start_build(context, publication, previous_snapshot={"title": "Draft edit"})
    quality.restore_build(context, build)
    publication.refresh_from_db()
    assert publication.title == "Test dashboard"
    assert build.status == "draft"


def test_other_user_cannot_open_build(build_context):
    context, publication = build_context
    build = quality.start_build(context, publication)
    context.username = "other"
    with pytest.raises(ToolError):
        quality.owned_build(context, str(build.pk))


def test_tool_images_stay_multimodal_in_anthropic_and_openai():
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "actual-pixels"},
    }
    result = ToolResult("inspect_dashboard", True, {"status": "inspected", "_tool_images": [image]})
    block = result.to_tool_result_block("call1")
    assert isinstance(block["content"], list) and block["content"][1] == image
    assert "_tool_images" not in block["content"][0]["text"]
    translated = response_input([{"role": "user", "content": [block]}])
    assert "actual-pixels" not in translated[0]["output"]
    assert translated[1]["content"][0]["image_url"] == "data:image/png;base64,actual-pixels"


def test_browser_failure_stays_repairable_draft(build_context, monkeypatch):
    context, publication = build_context
    build = quality.start_build(context, publication)
    monkeypatch.setattr(quality, "verify_queries", lambda *args: ([], []))
    monkeypatch.setattr(
        quality, "inspect_browser", Mock(side_effect=RuntimeError("browser unavailable"))
    )
    review, images = quality.inspect_build(context, build)
    assert not review["automated_passed"] and not images
    assert build.status == "needs_repair"
    assert any(item["code"] == "browser_review_failed" for item in review["findings"])


def test_completion_gate_suppresses_ready_claim_and_bounds_repairs(
    db, fake_anthropic, conversation, identity, page_context, settings, monkeypatch
):
    from lifedata_ai.agent.loop import run_turn
    from lifedata_ai.tools.registry import ToolRegistry

    settings.LIFEDATA_AI_PROVIDER = "anthropic"
    settings.ANTHROPIC_API_KEY = "fake-test-key"
    settings.LIFEDATA_AI_QUALITY_REPAIR_LIMIT = 2
    monkeypatch.setattr("lifedata_ai.agent.loop.get_registry", lambda: ToolRegistry())
    monkeypatch.setattr(
        quality, "pending_builds", lambda context: [{"build_id": "draft", "status": "needs_repair"}]
    )
    fake_anthropic.queue_text("Your dashboard is ready.", repeat=3)
    events = list(
        run_turn(
            conversation=conversation,
            identity=identity,
            superset_session_cookie="fake-session",
            page_context=page_context,
            user_message="Finish my dashboard",
        )
    )
    text = "".join(event.data["text"] for event in events if event.name == "text_delta")
    assert "Your dashboard is ready." not in text
    assert "remains a draft" in text
    assert fake_anthropic.call_count == 3
    assert any(
        event.name == "message_stop" and event.data["stop_reason"] == "quality_checks_required"
        for event in events
    )


def test_completion_gate_allows_repaired_revision(
    db, fake_anthropic, conversation, identity, page_context, settings, monkeypatch
):
    from lifedata_ai.agent.loop import run_turn
    from lifedata_ai.tools.registry import ToolRegistry

    settings.LIFEDATA_AI_PROVIDER = "anthropic"
    settings.ANTHROPIC_API_KEY = "fake-test-key"
    monkeypatch.setattr("lifedata_ai.agent.loop.get_registry", lambda: ToolRegistry())
    monkeypatch.setattr(quality, "pending_builds", Mock(side_effect=[[{"status": "draft"}], []]))
    fake_anthropic.queue_text("Premature claim")
    fake_anthropic.queue_text("The reviewed dashboard is complete.")
    events = list(
        run_turn(
            conversation=conversation,
            identity=identity,
            superset_session_cookie="fake-session",
            page_context=page_context,
            user_message="Finish my dashboard",
        )
    )
    text = "".join(event.data["text"] for event in events if event.name == "text_delta")
    assert "Premature claim" not in text
    assert text == "The reviewed dashboard is complete."
