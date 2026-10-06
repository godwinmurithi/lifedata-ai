"""Provider boundary and copilot integration tests, with no live API calls."""

import json

import httpx
import pytest

from lifedata_ai.agent.loop import _provider_message, run_turn
from lifedata_ai.agent.openai_provider import (
    OpenAIClient,
    OpenAIConnectionError,
    OpenAIStatusError,
    final_message,
    response_input,
)
from lifedata_ai.models import Message, ToolCallLog
from lifedata_ai.tools.superset_read import ListDatabasesTool


def sse(*events):
    return "".join("data: " + json.dumps(event) + "\n\n" for event in events)


def completed(*output):
    return {"type": "response.completed", "response": {"status": "completed", "output": output}}


def text_output(text):
    return {"type": "message", "content": [{"type": "output_text", "text": text}]}


def test_history_translates_parallel_tools_and_attachments():
    items = response_input(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "abc"},
                    },
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": "pdf",
                        },
                    },
                    {"type": "text", "text": "Inspect these"},
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private", "signature": "anthropic-only"},
                    {"type": "tool_use", "id": "call1", "name": "one", "input": {"x": 1}},
                    {"type": "tool_use", "id": "call2", "name": "two", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call1", "content": {"ok": True}},
                    {
                        "type": "tool_result",
                        "tool_use_id": "call2",
                        "content": "failed",
                        "is_error": True,
                    },
                ],
            },
        ]
    )
    assert items[0]["content"][0]["image_url"] == "data:image/png;base64,abc"
    assert items[0]["content"][1]["file_data"] == "data:application/pdf;base64,pdf"
    assert [i.get("call_id") for i in items[1:]] == ["call1", "call2", "call1", "call2"]
    assert items[-1]["output"] == "Tool error: failed"
    assert "anthropic-only" not in json.dumps(items)


def test_stream_maps_text_and_tools(respx_mock):
    route = respx_mock.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(
            200,
            headers={"x-request-id": "req-test"},
            text=sse(
                {"type": "response.output_text.delta", "delta": "Hello"},
                completed(
                    text_output("Hello"),
                    {
                        "type": "function_call",
                        "call_id": "call1",
                        "name": "test",
                        "arguments": '{"a":1}',
                    },
                ),
            ),
        )
    )
    with OpenAIClient("fake").messages.stream(
        model="gpt-5.3-codex",
        max_tokens=100,
        system=[{"type": "text", "text": "Help"}],
        messages=[{"role": "user", "content": [{"type": "text", "text": "Hi"}]}],
        tools=[{"name": "test", "input_schema": {"type": "object"}}],
        output_config={"effort": "medium"},
        timeout=5,
    ) as stream:
        assert [event.delta.text for event in stream] == ["Hello"]
        final = stream.get_final_message()
        assert final.stop_reason == "tool_use"
        assert final.content[-1].input == {"a": 1}
        assert stream.request_id == "req-test"
    payload = json.loads(route.calls[0].request.content)
    assert payload["store"] is False
    assert payload["tools"][0]["strict"] is False
    assert "cache_control" not in json.dumps(payload)


def test_incomplete_response_does_not_execute_tools():
    final = final_message(
        {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [
                {"type": "function_call", "call_id": "call1", "name": "write", "arguments": "{}"}
            ],
        }
    )
    assert final.stop_reason == "max_tokens"


@pytest.mark.parametrize("arguments", ["{broken", "[]"])
def test_invalid_tool_arguments_fail(arguments):
    with pytest.raises(OpenAIStatusError):
        final_message(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call1",
                        "name": "test",
                        "arguments": arguments,
                    }
                ]
            }
        )


@pytest.mark.django_db
def test_openai_turn_streams_and_persists(
    settings, conversation, analyst_identity, page_context, respx_mock
):
    settings.LIFEDATA_AI_PROVIDER = "openai"
    settings.OPENAI_API_KEY = "fake"
    settings.ANTHROPIC_API_KEY = ""
    route = respx_mock.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(
            200,
            text=sse(
                {"type": "response.output_text.delta", "delta": "Ready"},
                completed(text_output("Ready")),
            ),
        )
    )
    events = list(
        run_turn(
            conversation=conversation,
            user_message="Hello",
            identity=analyst_identity,
            page_context=page_context,
            superset_session_cookie="fake",
        )
    )
    assert route.called
    assert any(e.name == "text_delta" and e.data["text"] == "Ready" for e in events)
    assert events[-1].data["stop_reason"] == "end_turn"
    assert conversation.messages.filter(role=Message.Role.ASSISTANT).last().content == [
        {"type": "text", "text": "Ready"}
    ]


@pytest.mark.django_db
def test_exhausted_credits_end_turn_without_retry(
    settings, conversation, analyst_identity, page_context, respx_mock
):
    settings.LIFEDATA_AI_PROVIDER = "openai"
    settings.OPENAI_API_KEY = "fake"
    route = respx_mock.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(
            429, json={"error": {"code": "credit_balance_exhausted", "message": "No credits"}}
        )
    )
    events = list(
        run_turn(
            conversation=conversation,
            user_message="Hello",
            identity=analyst_identity,
            page_context=page_context,
            superset_session_cookie="fake",
        )
    )
    assert route.call_count == 1
    error = next(e for e in events if e.name == "error")
    assert error.data["code"] == "openai_error"
    assert "no credits" in error.data["message"]
    assert events[-1].data["stop_reason"] == "error"


def test_openai_auth_error_identifies_correct_key():
    assert "OPENAI_API_KEY" in _provider_message(OpenAIStatusError(401, "Rejected"), "openai")


@pytest.mark.django_db
def test_tool_round_trip_keeps_audit_and_replays_result(
    settings, conversation, analyst_identity, page_context, respx_mock, monkeypatch
):
    settings.LIFEDATA_AI_PROVIDER = "openai"
    settings.OPENAI_API_KEY = "fake"
    monkeypatch.setattr(ListDatabasesTool, "run", lambda self, context: {"databases": []})
    route = respx_mock.post("https://api.openai.com/v1/responses").mock(
        side_effect=[
            httpx.Response(
                200,
                text=sse(
                    completed(
                        {
                            "type": "function_call",
                            "call_id": "call-read",
                            "name": "list_databases",
                            "arguments": "{}",
                        }
                    )
                ),
            ),
            httpx.Response(200, text=sse(completed(text_output("No databases found.")))),
        ]
    )
    events = list(
        run_turn(
            conversation=conversation,
            user_message="List databases",
            identity=analyst_identity,
            page_context=page_context,
            superset_session_cookie="fake",
        )
    )
    assert events[-1].data["stop_reason"] == "end_turn"
    assert ToolCallLog.objects.filter(
        conversation=conversation, tool_name="list_databases", ok=True
    ).exists()
    payload = json.loads(route.calls[1].request.content)
    result = next(item for item in payload["input"] if item.get("type") == "function_call_output")
    assert result["call_id"] == "call-read"
    assert json.loads(result["output"]) == {"databases": []}


def test_disconnected_stream_raises(respx_mock):
    respx_mock.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(
            200, text=sse({"type": "response.output_text.delta", "delta": "partial"})
        )
    )
    with (
        pytest.raises(OpenAIConnectionError),
        OpenAIClient("fake").stream(
            model="gpt-5.3-codex",
            max_tokens=100,
            system=[],
            messages=[],
            tools=[],
            output_config={"effort": "medium"},
            timeout=5,
        ) as stream,
    ):
        list(stream)
