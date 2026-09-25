"""Tests for the streaming chat endpoint, `POST /lifedata-ai/api/chat/`.

The properties under test are the ones whose failure is invisible from the
server side. A malformed SSE frame, a missing terminating event or a stream
that simply stops all look identical to a dropped connection in the browser:
the panel shows a spinner forever and nobody sees a traceback. So the framing,
the event sequence and the guaranteed terminator are asserted byte by byte.

**How authentication is faked.** There is no Django login here — identity is
the Superset `session` cookie, verified by `SupersetIdentityMiddleware`, which
calls `middleware._verify_with_superset(cookie)` and caches the answer in a
module-level `_IdentityCache`. Both are monkeypatched by the autouse
`stub_superset_identity` fixture below:

* `_verify_with_superset` is replaced by a pure function that derives the
  username from the cookie value (`"superset-session-for-ana"` -> `"ana"`) and
  raises `SupersetError(401)` for anything else, so an unrecognised cookie
  still exercises the real 401 path.
* `middleware._cache` is replaced with a fresh `_IdentityCache` per test, so a
  cookie cached by one test cannot leak an identity into the next.

**How the model is faked.** Turns that need an answer use the shared
`fake_anthropic` fixture from `conftest.py`, which scripts the loop's client
directly. One test is deliberately different: the request-contract test drives
the *real* SDK over a `respx`-mocked transport (`unpatched_anthropic`), because
the properties it asserts — `stream: true` on the wire, `output_config`
surviving SDK serialisation, no `temperature`/`thinking` keys — only exist once
the SDK has built the request body. A fake client would accept anything.

No test touches the network. The autouse `http_router` fixture wraps every test
in a `respx` router with no default routes, so *any* outbound httpx call —
Anthropic or Superset — fails loudly instead of escaping. This matters
specifically because the Anthropic account has no credit: a real call would
return a 400 that could be mistaken for a genuine contract failure.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from django.conf import settings
from django.test import Client, override_settings

from lifedata_ai import middleware
from lifedata_ai.agent.loop import AgentEvent
from lifedata_ai.middleware import SupersetIdentity
from lifedata_ai.models import Conversation, Message, ToolCallLog
from lifedata_ai.superset_client import SupersetError
from lifedata_ai.tools import get_registry
from lifedata_ai.views import chat as chat_view

CHAT_URL = "/lifedata-ai/api/chat/"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

#: Cookie convention understood by the stubbed verifier. Anything not matching
#: this prefix is treated by the stub as a session Superset would reject.
SESSION_PREFIX = "superset-session-for-"


# --- Fixtures ------------------------------------------------------------


@pytest.fixture(autouse=True)
def stub_superset_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Superset sessions locally instead of over HTTP.

    Replacing the cache as well as the verifier is not belt-and-braces: the
    cache is module-level state that outlives a test, so without this a cookie
    verified in one test would resolve to the same user in the next.
    """

    def _verify(cookie: str) -> SupersetIdentity:
        if not cookie.startswith(SESSION_PREFIX):
            raise SupersetError("Superset rejected this session", status_code=401)
        username = cookie[len(SESSION_PREFIX) :]
        return SupersetIdentity(
            username=username,
            user_id=abs(hash(username)) % 10_000,
            first_name=username.title(),
            last_name="Analyst",
            email=f"{username}@example.invalid",
            roles=["Alpha"],
        )

    monkeypatch.setattr(middleware, "_cache", middleware._IdentityCache())
    monkeypatch.setattr(middleware, "_verify_with_superset", _verify)


@pytest.fixture(autouse=True)
def http_router() -> Iterator[respx.MockRouter]:
    """Fail every unmocked outbound HTTP call.

    `assert_all_called=False` because most tests register no routes at all —
    the point is the inverse assertion, that nothing escapes to the real
    Anthropic or Superset APIs.
    """
    with respx.mock(assert_all_called=False) as router:
        yield router


def signed_in(username: str) -> Client:
    """A test client carrying a Superset session cookie for `username`."""
    client = Client()
    client.cookies["session"] = f"{SESSION_PREFIX}{username}"
    return client


def post(client: Client, payload: Any) -> Any:
    """POST a JSON body, encoding it ourselves so malformed input is possible."""
    body = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    return client.post(CHAT_URL, data=body, content_type="application/json")


# --- SSE helpers ---------------------------------------------------------


def read_sse(response: Any) -> list[tuple[str, dict[str, Any]]]:
    """Consume a streaming response and assert every frame is well formed.

    A frame is exactly `event: <name>\\n data: <json>\\n\\n`. The strictness is
    the point: a payload containing a bare newline would split into two frames
    on the wire and the browser would parse an event that was never sent.
    """
    body = b"".join(response.streaming_content).decode("utf-8")
    if not body:
        return []
    assert body.endswith("\n\n"), "the stream must end on a complete frame"

    frames = body.split("\n\n")
    assert frames[-1] == "", "trailing separator expected, no partial frame"

    parsed: list[tuple[str, dict[str, Any]]] = []
    for frame in frames[:-1]:
        lines = frame.split("\n")
        assert len(lines) == 2, f"a frame must be exactly two lines, got {lines!r}"
        assert lines[0].startswith("event: "), f"bad event line: {lines[0]!r}"
        assert lines[1].startswith("data: "), f"bad data line: {lines[1]!r}"
        data = json.loads(lines[1][len("data: ") :])
        assert isinstance(data, dict), "every data payload must be a JSON object"
        parsed.append((lines[0][len("event: ") :], data))
    return parsed


def names(frames: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [name for name, _ in frames]


def anthropic_sse(*events: tuple[str, dict[str, Any]]) -> bytes:
    """Encode an Anthropic streaming response body.

    Only the request-contract test needs this: everywhere else the scripted
    `fake_anthropic` client is both simpler and less coupled to the SDK.
    """
    return "".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events
    ).encode("utf-8")


def _message_start(*, message_id: str = "msg_test") -> tuple[str, dict[str, Any]]:
    return (
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 12, "output_tokens": 1},
            },
        },
    )


def text_turn(text: str) -> bytes:
    """A plain assistant answer that ends the turn."""
    return anthropic_sse(
        _message_start(),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 9},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    )


def anthropic_route(router: respx.MockRouter, *bodies: bytes) -> respx.Route:
    """Serve one streaming body per Anthropic call, in order."""
    responses = [
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "request-id": f"req_{index}"},
            content=body,
        )
        for index, body in enumerate(bodies)
    ]
    return router.post(ANTHROPIC_URL).mock(side_effect=responses)


# --- Authentication ------------------------------------------------------


@pytest.mark.django_db
def test_anonymous_post_is_rejected_before_the_view_runs():
    response = Client().post(CHAT_URL, data="{}", content_type="application/json")

    assert response.status_code == 401
    assert json.loads(response.content)["error"] == "unauthenticated"
    assert not Conversation.objects.exists()


@pytest.mark.django_db
def test_session_superset_rejects_is_401():
    client = Client()
    client.cookies["session"] = "forged-cookie"

    response = post(client, {"message": "hello"})

    assert response.status_code == 401
    assert json.loads(response.content)["error"] == "unauthenticated"


# --- Method and body validation ------------------------------------------


@pytest.mark.django_db
def test_get_is_method_not_allowed():
    response = signed_in("ana").get(CHAT_URL)

    assert response.status_code == 405
    assert json.loads(response.content)["error"] == "method_not_allowed"


@pytest.mark.django_db
@pytest.mark.parametrize("payload", [{}, {"message": ""}, {"message": "   \n\t "}])
def test_blank_or_missing_message_is_400(payload: dict[str, Any]):
    response = post(signed_in("ana"), payload)

    assert response.status_code == 400
    assert json.loads(response.content)["error"] == "empty_message"
    assert not Conversation.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("message", [42, None, ["hello"], {"text": "hello"}])
def test_non_string_message_is_400(message: Any):
    response = post(signed_in("ana"), {"message": message})

    assert response.status_code == 400
    assert json.loads(response.content)["error"] == "empty_message"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "body",
    [
        "{not json at all}",
        "",
        "[1, 2, 3]",
        '"a bare string"',
        "null",
        b"\xff\xfe\x00binary",
    ],
)
def test_malformed_body_is_400_not_500(body: Any):
    """A hostile or truncated body must never reach the exception handler."""
    response = post(signed_in("ana"), body)

    assert response.status_code == 400
    assert json.loads(response.content)["error"] in {"invalid_json", "empty_message"}


@pytest.mark.django_db
def test_over_long_message_is_400():
    response = post(signed_in("ana"), {"message": "x" * (chat_view.MAX_MESSAGE_LENGTH + 1)})

    assert response.status_code == 400
    assert json.loads(response.content)["error"] == "message_too_long"


@pytest.mark.django_db
def test_message_at_the_length_limit_is_accepted(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(chat_view, "run_turn", lambda **_: iter(()))

    response = post(signed_in("ana"), {"message": "x" * chat_view.MAX_MESSAGE_LENGTH})

    assert response.status_code == 200


@pytest.mark.django_db
def test_non_string_conversation_id_is_400():
    response = post(signed_in("ana"), {"message": "hi", "conversation_id": 7})

    assert response.status_code == 400
    assert json.loads(response.content)["error"] == "invalid_conversation_id"


@pytest.mark.django_db
def test_malformed_conversation_uuid_is_404_not_500():
    """Django raises before the query for a bad UUID; the verdict is still 404."""
    response = post(signed_in("ana"), {"message": "hi", "conversation_id": "not-a-uuid"})

    assert response.status_code == 404
    assert json.loads(response.content)["error"] == "not_found"


@pytest.mark.django_db
def test_unknown_conversation_id_is_404():
    response = post(
        signed_in("ana"),
        {"message": "hi", "conversation_id": "00000000-0000-4000-8000-000000000000"},
    )

    assert response.status_code == 404


# --- Cross-user isolation ------------------------------------------------


@pytest.mark.django_db
def test_another_users_conversation_is_never_reusable():
    """Bob's thread must be unreadable and unwritable from Ana's session.

    The endpoint answers 404 rather than 403: a 403 would confirm the id
    exists, which is the fact being protected.
    """
    bob = Conversation.objects.create(superset_username="bob", title="Bob's private thread")
    Message.objects.create(
        conversation=bob,
        role=Message.Role.USER,
        content=[{"type": "text", "text": "bob's secret question"}],
    )

    response = post(signed_in("ana"), {"message": "hi", "conversation_id": str(bob.pk)})

    assert response.status_code == 404
    assert json.loads(response.content)["error"] == "not_found"
    # Nothing of Bob's is echoed back, and nothing was appended to his thread.
    assert b"secret" not in response.content
    assert bob.messages.count() == 1
    # Nor was a conversation silently created for Ana under Bob's id.
    assert not Conversation.objects.filter(superset_username="ana").exists()


@pytest.mark.django_db
def test_a_new_conversation_belongs_to_the_caller(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(chat_view, "run_turn", lambda **_: iter(()))
    Conversation.objects.create(superset_username="bob")

    response = post(signed_in("ana"), {"message": "hello"})
    read_sse(response)

    created = Conversation.objects.get(superset_username="ana")
    assert created.superset_username == "ana"


# --- Streaming response shape --------------------------------------------


@pytest.mark.django_db
def test_successful_turn_streams_well_formed_sse(fake_anthropic: Any):
    fake_anthropic.queue_text("Encounters rose in March.")

    response = post(signed_in("ana"), {"message": "How did encounters trend?"})

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "text/event-stream"

    frames = read_sse(response)
    assert names(frames)[0] == "message_start"
    assert names(frames)[-1] == "message_stop"
    assert set(names(frames)[1:-1]) == {"text_delta"}

    start_data = frames[0][1]
    stop_data = frames[-1][1]
    assert "".join(data["text"] for _, data in frames[1:-1]) == "Encounters rose in March."
    assert stop_data["stop_reason"] == "end_turn"
    # The conversation id is echoed on both ends so the panel can bind the
    # thread before the answer finishes.
    assert start_data["conversation_id"] == stop_data["conversation_id"]
    assert Conversation.objects.filter(pk=start_data["conversation_id"]).exists()


@pytest.mark.django_db
def test_streaming_headers_defeat_intermediary_buffering(fake_anthropic: Any):
    fake_anthropic.queue_text("ok")

    response = post(signed_in("ana"), {"message": "hi"})

    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers["X-Accel-Buffering"] == "no"
    read_sse(response)


@pytest.mark.django_db
def test_tool_round_emits_tool_use_and_tool_result_frames(fake_anthropic: Any):
    """`get_page_context` answers from `ToolContext` alone — no Superset call,
    so the round is provable without loosening the network guard."""
    fake_anthropic.queue_tool_use([("toolu_1", "get_page_context", {})])
    fake_anthropic.queue_text("You are on dashboard 4.")

    response = post(
        signed_in("ana"),
        {
            "message": "What am I looking at?",
            "page_context": {"page_type": "dashboard", "dashboard_id": 4},
        },
    )

    frames = read_sse(response)
    assert names(frames)[:3] == ["message_start", "tool_use", "tool_result"]
    assert names(frames)[-1] == "message_stop"

    tool_use = frames[1][1]
    tool_result = frames[2][1]
    assert tool_use["name"] == "get_page_context"
    assert tool_result["tool_use_id"] == tool_use["id"] == "toolu_1"
    assert tool_result["ok"] is True
    assert tool_result["is_error"] is False
    # Every tool call is auditable — that is the Phase 1 acceptance criterion.
    log = ToolCallLog.objects.get()
    assert (log.tool_name, log.superset_username, log.ok) == ("get_page_context", "ana", True)


@pytest.mark.django_db
def test_turn_persists_user_and_assistant_messages(fake_anthropic: Any):
    fake_anthropic.queue_text("Answered.", request_id="req_persist")

    response = post(signed_in("ana"), {"message": "Ask me something"})
    read_sse(response)

    conversation = Conversation.objects.get(superset_username="ana")
    roles = list(conversation.messages.values_list("role", flat=True))
    assert roles == [Message.Role.USER, Message.Role.ASSISTANT]
    assistant = conversation.messages.last()
    # The request id is taken from the stream, not the accumulated message —
    # the traceability requirement quietly stops holding if that regresses.
    assert assistant.anthropic_request_id == "req_persist"
    assert conversation.title == "Ask me something"


@pytest.mark.django_db
@override_settings(ANTHROPIC_API_KEY="")
def test_missing_api_key_streams_an_error_rather_than_failing(http_router: respx.MockRouter):
    """No key must not mean no response — and must not mean a network call."""
    response = post(signed_in("ana"), {"message": "hi"})

    assert response.status_code == 200
    frames = read_sse(response)
    assert names(frames) == ["message_start", "error", "message_stop"]
    assert frames[1][1]["code"] == "anthropic_key_missing"
    assert frames[-1][1]["stop_reason"] == "error"
    assert not http_router.calls


# --- The Anthropic request contract --------------------------------------


@pytest.mark.django_db
def test_anthropic_request_matches_the_verified_contract(
    http_router: respx.MockRouter,
    unpatched_anthropic: None,
    anthropic_key: str,
):
    """Parameters verified against the live API; regressions here are 400s.

    Uniquely in this module the request goes through the *real* SDK over a
    mocked transport, because the assertions are about the serialised request
    body — `stream: true`, `output_config` surviving serialisation, and the
    absence of `temperature`, `top_p`, `top_k` and `thinking`, all of which the
    current models reject outright. A scripted client would accept any of them.
    """
    route = anthropic_route(http_router, text_turn("ok"))

    read_sse(post(signed_in("ana"), {"message": "hi"}))

    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "claude-opus-5"
    assert body["max_tokens"] == 32000
    assert body["output_config"] == {"effort": settings.LIFEDATA_AI_EFFORT}
    assert body["stream"] is True
    for forbidden in ("temperature", "top_p", "top_k", "thinking"):
        assert forbidden not in body, f"{forbidden} is rejected by the current models"

    # Every registered tool is offered, and the cache breakpoints survive
    # serialisation — this is the only test that sees the real wire body, so it
    # is the only place a silently dropped `cache_control` would show up.
    assert len(body["tools"]) == len(get_registry().all())
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert isinstance(body["system"], list)
    assert any(block.get("cache_control") for block in body["system"])


# --- Failure containment -------------------------------------------------


def _raising_after_one_event(**_: Any) -> Iterator[AgentEvent]:
    yield AgentEvent("message_start", {"conversation_id": "partial"})
    raise RuntimeError("the loop exploded halfway through")


def _raising_immediately(**_: Any) -> Iterator[AgentEvent]:
    raise RuntimeError("the loop exploded before yielding")
    yield  # pragma: no cover - makes this a generator


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("faulty_loop", "expected_prefix"),
    [
        (_raising_after_one_event, ["message_start"]),
        (_raising_immediately, []),
    ],
)
def test_exception_midstream_still_closes_the_stream(
    monkeypatch: pytest.MonkeyPatch,
    faulty_loop: Any,
    expected_prefix: list[str],
):
    """A hung SSE stream looks exactly like a frozen UI, so it must not happen.

    The generator is expected to convert the failure into an `error` frame
    followed by `message_stop`, never to propagate and truncate the response.
    """
    monkeypatch.setattr(chat_view, "run_turn", faulty_loop)

    response = post(signed_in("ana"), {"message": "hi"})

    assert response.status_code == 200
    frames = read_sse(response)
    assert names(frames) == [*expected_prefix, "error", "message_stop"]
    assert frames[-2][1]["code"] == "internal_error"
    assert frames[-1][1]["stop_reason"] == "error"
    # The internal exception text is not leaked to the browser.
    assert "exploded" not in json.dumps(frames[-2][1])


@pytest.mark.django_db
def test_payload_values_cannot_forge_an_sse_frame(monkeypatch: pytest.MonkeyPatch):
    """`json.dumps` escapes newlines; without it a value could inject an event."""
    hostile = "line one\n\nevent: message_stop\ndata: {}\n\n"

    def _echo(**_: Any) -> Iterator[AgentEvent]:
        yield AgentEvent("text_delta", {"text": hostile})
        yield AgentEvent("message_stop", {"stop_reason": "end_turn"})

    monkeypatch.setattr(chat_view, "run_turn", _echo)

    frames = read_sse(post(signed_in("ana"), {"message": "hi"}))

    # read_sse would already have failed on a smuggled frame; assert the round
    # trip is lossless too, so the escaping is not silently mangling text.
    assert names(frames) == ["text_delta", "message_stop"]
    assert frames[0][1]["text"] == hostile
