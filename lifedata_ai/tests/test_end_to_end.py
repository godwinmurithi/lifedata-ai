"""End-to-end proof of the copilot, from HTTP POST to persisted audit row.

WHY THIS FILE IS THE IMPORTANT ONE
----------------------------------
Every other test in the suite mocks something big — the SDK, the transport, or
Superset. This one drives the *real* stack against a *scripted* provider: real
URL routing, real identity middleware, real agent loop, real Anthropic SDK, real
tool registry, real semantics registry, real database writes. Only the provider's
answer is canned.

The provider is canned on purpose, not from necessity: the account is funded and
a live call would succeed. Scripting it keeps the suite deterministic, fast and
free, and lets failure paths (a refusal, a 400, an exhausted script) be provoked
on demand — none of which a live model would reliably produce. The live model is
covered separately by the Playwright suite in ``tests_browser/``.

What is genuinely exercised here:

* ``POST /lifedata-ai/api/chat/`` through Django's URL conf and middleware.
* ``agent.loop.run_turn`` including its history rebuild and round budget.
* The Anthropic SDK's own streaming client, parsing SSE we emit ourselves.
* ``tools.registry.execute`` running a real tool that returns real data.
* ``Conversation`` / ``Message`` / ``ToolCallLog`` rows.
* The exact request body the loop sends, which is the contract most likely to
  drift silently (``output_config``, ``strict`` tools, no forbidden sampling
  parameters).

What is NOT exercised, and must not be claimed: anything about the model's own
judgement. A green run here says the plumbing is sound, not that Claude would
pick the right tool.

The tool-result assertions deliberately look for ``rpt_lab_orders`` in data the
tool fetched for itself — the governed semantics registry in the hermetic tests,
and live Superset in the marked ``superset_live`` ones. A tool that silently
returned an empty payload would still produce a well-formed SSE stream, so
asserting on real content is what makes this test load-bearing rather than
decorative.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.test import Client, override_settings
from django.urls import clear_url_caches

from lifedata_ai import middleware as middleware_module
from lifedata_ai.agent.events import PANEL_EVENTS
from lifedata_ai.middleware import SupersetIdentity
from lifedata_ai.models import Conversation, Message, ToolCallLog
from lifedata_ai.tests.conftest import system_text
from lifedata_ai.tools import get_registry

# `scripts/` sits beside the Django package and is not importable as an
# installed module; add the repository root explicitly rather than relying on
# whatever pytest happened to put on sys.path.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.mock_anthropic_server import (  # noqa: E402
    MockAnthropicServer,
    error_turn,
    refusal_turn,
    text_turn,
    tool_use_turn,
)
from scripts.mock_anthropic_server import (  # noqa: E402
    RecordedRequest as MockRecordedRequest,
)

CHAT_URL = "/lifedata-ai/api/chat/"
PANEL_URL = "/lifedata-ai/static/panel/panel.js"
PANEL_PATH = _REPO_ROOT / "lifedata_ai" / "static" / "panel" / "panel.js"

#: Sampling and reasoning parameters that are rejected outright by the current
#: models. Their absence is part of the verified request contract, so it is
#: asserted rather than assumed.
FORBIDDEN_REQUEST_KEYS = ("temperature", "top_p", "top_k", "thinking")

#: A goal whose keywords overlap `rpt_lab_orders`' declared subject areas
#: (laboratory, turnaround) and nothing else's — see semantics/registry.yaml.
LAB_GOAL = "laboratory turnaround by facility"


# --- SSE helpers ---------------------------------------------------------


@dataclass(frozen=True)
class Frame:
    """One parsed Server-Sent Event from our own chat endpoint."""

    event: str
    data: dict[str, Any]


def parse_sse(payload: bytes) -> list[Frame]:
    """Parse the endpoint's SSE output the way the panel's reader does."""
    frames: list[Frame] = []
    for block in payload.decode("utf-8").split("\n\n"):
        if not block.strip():
            continue
        event = ""
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data_lines.append(line[len("data: ") :])
        raw = "\n".join(data_lines)
        frames.append(Frame(event=event, data=json.loads(raw) if raw else {}))
    return frames


def event_names(frames: list[Frame]) -> list[str]:
    return [frame.event for frame in frames]


def joined_text(frames: list[Frame]) -> str:
    return "".join(f.data.get("text", "") for f in frames if f.event == "text_delta")


# --- Fixtures ------------------------------------------------------------


@pytest.fixture
def mock_anthropic() -> Iterator[MockAnthropicServer]:
    """A scripted Anthropic API on loopback, torn down with the test."""
    server = MockAnthropicServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def anthropic_env(
    mock_anthropic: MockAnthropicServer,
    monkeypatch: pytest.MonkeyPatch,
    unpatched_anthropic: None,
    anthropic_key: str,
) -> MockAnthropicServer:
    """Point the real SDK at the mock and give the loop a key to proceed with.

    `ANTHROPIC_BASE_URL` is the SDK's own documented override, so nothing in
    `agent/loop.py` has to be patched — the loop constructs its client exactly
    as it does in production.

    `unpatched_anthropic` undoes conftest's autouse guard. That guard forbids the
    *destination*, not the class: here the client is real but the endpoint is
    loopback, which is precisely the case the guard is written to allow through.
    Nothing in this module can reach `api.anthropic.com` — the SDK resolves
    `ANTHROPIC_BASE_URL` at construction, and it points at this fixture's server.
    """
    monkeypatch.setenv("ANTHROPIC_BASE_URL", mock_anthropic.base_url)
    return mock_anthropic


@pytest.fixture
def client_as_analyst(
    identity: SupersetIdentity, monkeypatch: pytest.MonkeyPatch
) -> Client:
    """A Django test client carrying a session cookie the middleware accepts.

    Superset itself is not contacted: `_verify_with_superset` is replaced, which
    is the single seam between "who is this" and the rest of the request. The
    cookie value is unique per test so the middleware's TTL cache cannot serve a
    stale identity from an earlier one.
    """
    monkeypatch.setattr(
        middleware_module,
        "_verify_with_superset",
        lambda cookie: identity,
    )
    client = Client()
    client.cookies["session"] = f"fake-session-{uuid.uuid4().hex}"
    return client


def post_chat(
    client: Client,
    message: str,
    *,
    conversation_id: str | None = None,
    page_context: dict[str, Any] | None = None,
) -> tuple[int, list[Frame]]:
    """POST a chat turn and drain the stream. Returns (status, frames)."""
    payload: dict[str, Any] = {
        "message": message,
        "page_context": page_context
        if page_context is not None
        else {"page_type": "dashboard", "dashboard_id": 1},
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id

    response = client.post(CHAT_URL, data=json.dumps(payload), content_type="application/json")
    if response.status_code != 200:
        return response.status_code, []
    body = b"".join(response.streaming_content)
    return response.status_code, parse_sse(body)


# --- The full path -------------------------------------------------------


@pytest.mark.django_db
def test_chat_turn_runs_a_real_tool_and_persists_the_whole_turn(
    client_as_analyst: Client, anthropic_env: MockAnthropicServer
) -> None:
    """One turn, one tool round trip, everything asserted end to end."""
    answer = (
        "rpt_lab_orders is the right dataset: one row per laboratory order, "
        "so an order is not a result."
    )
    anthropic_env.script(
        tool_use_turn(
            "recommend_dataset",
            {"analysis_goal": LAB_GOAL},
            preamble="Let me check the governed registry.",
        ),
        text_turn(answer),
    )

    status, frames = post_chat(client_as_analyst, "Which dataset should I use for lab turnaround?")
    assert status == 200

    names = event_names(frames)
    # Order is the contract the panel depends on: the stream opens with the
    # conversation id and closes with a stop, with the tool bracketed between.
    assert names[0] == "message_start"
    assert names[-1] == "message_stop"
    assert "error" not in names
    assert names.index("tool_use") < names.index("tool_result")
    assert names.index("tool_result") < len(names) - 1

    # Text arrived on both sides of the tool call, in several deltas.
    assert names.count("text_delta") > 2
    assert "Let me check the governed registry." in joined_text(frames)
    assert answer in joined_text(frames)

    tool_use = next(f for f in frames if f.event == "tool_use")
    assert tool_use.data["name"] == "recommend_dataset"
    assert tool_use.data["id"].startswith("toolu_")
    assert tool_use.data["summary"] == "matching datasets to the goal"

    tool_result = next(f for f in frames if f.event == "tool_result")
    assert tool_result.data == {
        "tool_use_id": tool_use.data["id"],
        "name": "recommend_dataset",
        "ok": True,
        "is_error": False,
    }

    stop = frames[-1]
    assert stop.data["stop_reason"] == "end_turn"
    conversation_id = frames[0].data["conversation_id"]
    assert stop.data["conversation_id"] == conversation_id

    # --- persistence ---------------------------------------------------
    conversation = Conversation.objects.get(pk=conversation_id)
    assert conversation.superset_username == "analyst"
    assert conversation.title.startswith("Which dataset")
    assert conversation.page_scope["dashboard_id"] == 1

    rows = list(conversation.messages.all())
    assert [row.role for row in rows] == ["user", "assistant", "tool", "assistant"]
    assert rows[0].content == [
        {"type": "text", "text": "Which dataset should I use for lab turnaround?"}
    ]
    # Raw Anthropic blocks, stored verbatim so the turn can be replayed.
    assert [b["type"] for b in rows[1].content] == ["text", "tool_use"]
    assert rows[1].content[1]["input"] == {"analysis_goal": LAB_GOAL}
    assert rows[1].anthropic_request_id.startswith("req_mock_")
    assert rows[3].content == [{"type": "text", "text": answer}]

    # --- the tool returned REAL data ------------------------------------
    tool_block = rows[2].content[0]
    assert tool_block["type"] == "tool_result"
    assert tool_block["tool_use_id"] == tool_use.data["id"]
    assert "is_error" not in tool_block
    result = json.loads(tool_block["content"])
    assert result["matched_on_subject_areas"] is True
    top = result["datasets"][0]
    assert top["name"] == "rpt_lab_orders", "the semantics registry was not actually read"
    assert top["grain"] == "lab_order"
    assert top["superset_dataset_id"] == 3
    assert "never treat an order as a result" in " ".join(top["notes"].lower().split())

    # --- audit ----------------------------------------------------------
    logs = list(ToolCallLog.objects.filter(conversation=conversation))
    assert len(logs) == 1
    assert logs[0].tool_name == "recommend_dataset"
    assert logs[0].superset_username == "analyst"
    assert logs[0].mode == "read"
    assert logs[0].ok is True
    assert logs[0].error == ""
    assert logs[0].arguments == {"analysis_goal": LAB_GOAL}
    assert logs[0].duration_ms is not None


@pytest.mark.django_db
def test_request_shape_matches_the_verified_contract(
    client_as_analyst: Client, anthropic_env: MockAnthropicServer
) -> None:
    """Assert what the loop actually sent to the provider, both rounds."""
    anthropic_env.script(
        tool_use_turn("explain_indicator", {"name": "lab_turnaround_hours"}),
        text_turn("Hours from order to verification, for verified orders only."),
    )
    status, frames = post_chat(client_as_analyst, "Explain lab turnaround hours.")
    assert status == 200
    assert "error" not in event_names(frames)

    assert anthropic_env.request_count == 2, "the tool result was not sent back"
    first, second = anthropic_env.requests

    for request in (first, second):
        assert request.path.startswith("/v1/messages")
        body = request.body
        assert body["model"] == settings.LIFEDATA_AI_MODEL == "claude-opus-5"
        assert body["max_tokens"] == 32000
        assert body["output_config"] == {"effort": settings.LIFEDATA_AI_EFFORT}
        assert body["stream"] is True
        assert system_text(body["system"]).strip()
        for forbidden in FORBIDDEN_REQUEST_KEYS:
            assert forbidden not in body, f"{forbidden} is rejected by current models"

        # Every registered tool is offered — read AND write. The copilot
        # proposes changes; the panel executes them (spec §1).
        assert len(request.tools) == len(get_registry().all())
        assert sorted(request.tool_names) == sorted(get_registry().names())
        for tool in request.tools:
            schema = tool["input_schema"]
            assert schema["additionalProperties"] is False
            assert not {"oneOf", "anyOf", "allOf"} & set(schema)
            assert tool["description"].strip()

        # The last tool spec carries the cache breakpoint. The tool array is the
        # largest stable block in every request; without it the 27 schemas are
        # re-billed at full price on every round of every turn.
        assert request.tools[-1].get("cache_control") == {"type": "ephemeral"}

    # Round one carries only the user turn; round two carries the assistant's
    # tool_use and the tool_result, in that order and in one user message.
    assert [m["role"] for m in first.messages] == ["user"]
    assert [m["role"] for m in second.messages] == ["user", "assistant", "user"]
    results = second.messages[2]["content"]
    assert [block["type"] for block in results] == ["tool_result"]
    payload = json.loads(results[0]["content"])
    assert payload["indicator"]["name"] == "lab_turnaround_hours"
    assert payload["indicator"]["dataset"] == "rpt_lab_orders"
    assert payload["indicator"]["verbatim"] is True

    # The system prompt is governed context, not a bare instruction.
    system = system_text(first.body["system"])
    assert "synthetic" in system.lower() or "LifeData" in system


@pytest.mark.django_db
def test_second_turn_replays_the_stored_history(
    client_as_analyst: Client, anthropic_env: MockAnthropicServer
) -> None:
    """A follow-up on the same conversation resends the earlier turns."""
    anthropic_env.script(text_turn("First answer."), text_turn("Second answer."))

    _, frames = post_chat(client_as_analyst, "First question?")
    conversation_id = frames[0].data["conversation_id"]

    _, frames_two = post_chat(
        client_as_analyst, "Second question?", conversation_id=conversation_id
    )
    assert frames_two[0].data["conversation_id"] == conversation_id
    assert joined_text(frames_two) == "Second answer."

    second = anthropic_env.requests[1]
    texts = [
        block["text"]
        for message in second.messages
        for block in message["content"]
        if block.get("type") == "text"
    ]
    assert texts == ["First question?", "First answer.", "Second question?"]
    assert Conversation.objects.count() == 1
    assert Message.objects.filter(conversation_id=conversation_id).count() == 4


@pytest.mark.django_db
def test_unknown_conversation_id_is_a_404(client_as_analyst: Client) -> None:
    """Another user's conversation must be indistinguishable from a missing one."""
    other = Conversation.objects.create(superset_username="someone-else")
    status, frames = post_chat(client_as_analyst, "Hello", conversation_id=str(other.pk))
    assert status == 404
    assert frames == []


def test_chat_requires_a_superset_session() -> None:
    """No cookie, no turn — the middleware refuses before the view runs."""
    response = Client().post(
        CHAT_URL, data=json.dumps({"message": "hi"}), content_type="application/json"
    )
    assert response.status_code == 401
    assert response.json()["error"] == "unauthenticated"


# --- Failure paths, end to end -------------------------------------------


@pytest.mark.django_db
def test_provider_credit_failure_reaches_the_user_as_an_error_frame(
    client_as_analyst: Client, anthropic_env: MockAnthropicServer
) -> None:
    """A provider 400 is relayed intact, with the provider's own wording.

    Passing the text through is deliberate: an exhausted credit balance is an
    operator problem with an obvious fix, and a generic "the model request
    failed" would send whoever sees it looking in the wrong place. This project
    has hit exactly this 400 in anger, which is why it is pinned as a test.
    """
    anthropic_env.script(
        error_turn(400, "Your credit balance is too low to access the Anthropic API.")
    )
    status, frames = post_chat(client_as_analyst, "Anything at all")
    assert status == 200

    assert event_names(frames) == ["message_start", "error", "message_stop"]
    error = frames[1]
    assert error.data["code"] == "anthropic_error"
    assert "credit balance is too low" in error.data["message"]
    assert frames[-1].data["stop_reason"] == "error"

    # The user's message is still persisted, so the turn is auditable even
    # though the provider never answered.
    conversation = Conversation.objects.get()
    assert [m.role for m in conversation.messages.all()] == ["user"]


@pytest.mark.django_db
def test_missing_api_key_ends_the_stream_cleanly_without_calling_the_provider(
    client_as_analyst: Client, anthropic_env: MockAnthropicServer
) -> None:
    anthropic_env.script(text_turn("should never be reached"))
    with override_settings(ANTHROPIC_API_KEY=""):
        status, frames = post_chat(client_as_analyst, "Hello")

    assert status == 200
    assert event_names(frames) == ["message_start", "error", "message_stop"]
    assert frames[1].data["code"] == "anthropic_key_missing"
    assert anthropic_env.request_count == 0


@pytest.mark.django_db
def test_refusal_is_persisted_and_reported(
    client_as_analyst: Client, anthropic_env: MockAnthropicServer
) -> None:
    """A refusal is an HTTP 200 with no content blocks; it must not crash."""
    anthropic_env.script(refusal_turn())
    status, frames = post_chat(client_as_analyst, "Give me every patient's home address")

    assert status == 200
    assert event_names(frames) == ["message_start", "error", "message_stop"]
    assert frames[1].data["code"] == "refusal"
    assert frames[-1].data["stop_reason"] == "refusal"

    rows = list(Conversation.objects.get().messages.all())
    assert [row.role for row in rows] == ["user", "assistant"]
    assert rows[1].content == []


@pytest.mark.django_db
def test_a_failing_tool_is_logged_and_returned_to_the_model(
    client_as_analyst: Client, anthropic_env: MockAnthropicServer
) -> None:
    """The model's mistakes are conversational, not fatal — and still audited."""
    anthropic_env.script(
        tool_use_turn("explain_indicator", {"name": "made_up_indicator"}),
        text_turn("There is no governed definition for that."),
    )
    status, frames = post_chat(client_as_analyst, "Explain made_up_indicator")

    assert status == 200
    assert "error" not in event_names(frames)
    result_frame = next(f for f in frames if f.event == "tool_result")
    assert result_frame.data["ok"] is False
    assert result_frame.data["is_error"] is True

    log = ToolCallLog.objects.get()
    assert log.tool_name == "explain_indicator"
    assert log.ok is False
    assert "made_up_indicator" in log.error

    # The failure was handed back to the model as a correctable tool_result,
    # complete with the list of keys it should have chosen from.
    tool_message = Conversation.objects.get().messages.all()[2]
    block = tool_message.content[0]
    assert block["is_error"] is True
    payload = json.loads(block["content"])
    assert payload["error"] == "unknown_indicator"
    assert "lab_turnaround_hours" in payload["details"]["known_indicators"]


# --- Live Superset -------------------------------------------------------


def _superset_session_cookie() -> str | None:
    """Log into the running Superset as admin and return its session cookie.

    Returns None — rather than raising — when Superset is unreachable or the
    credentials are absent, so the suite still runs on a laptop with no stack
    up. The credentials come from `.env`, which `lifedata.settings` loads into
    the environment at import time.
    """
    username = os.environ.get("SUPERSET_ADMIN_USERNAME")
    password = os.environ.get("SUPERSET_ADMIN_PASSWORD")
    if not username or not password:
        return None

    import httpx

    base = settings.LIFEDATA_AI_SUPERSET_INTERNAL_URL
    try:
        with httpx.Client(base_url=base, timeout=15.0, follow_redirects=False) as http:
            page = http.get("/login/")
            if page.status_code != 200:
                return None
            token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page.text)
            form = {"username": username, "password": password}
            if token:
                form["csrf_token"] = token.group(1)
            http.post("/login/", data=form)
            return http.cookies.get("session")
    except Exception:  # noqa: BLE001 - absence of a stack is not a test failure
        return None


@pytest.fixture(scope="module")
def superset_cookie() -> str:
    cookie = _superset_session_cookie()
    if not cookie:
        pytest.skip("no reachable Superset to log into (set up deploy/docker-compose.yml)")
    return cookie


@pytest.mark.django_db
def test_live_turn_reads_real_datasets_from_real_superset(
    superset_cookie: str, anthropic_env: MockAnthropicServer
) -> None:
    """The same path again, with nothing about Superset faked.

    The identity middleware really asks Superset who the caller is, and
    `list_datasets` really queries the Superset REST API with that session, so
    this test fails if the reporting datasets were never registered.
    """
    anthropic_env.script(
        tool_use_turn("list_datasets", {"q": ""}),
        text_turn("Five reporting datasets are available."),
    )
    client = Client()
    client.cookies["session"] = superset_cookie

    status, frames = post_chat(client, "What data can I query?")
    assert status == 200
    assert "error" not in event_names(frames)

    result_frame = next(f for f in frames if f.event == "tool_result")
    assert result_frame.data["ok"] is True

    conversation = Conversation.objects.get()
    payload = json.loads(conversation.messages.all()[2].content[0]["content"])
    names = [dataset["table_name"] for dataset in payload["datasets"]]
    assert "rpt_lab_orders" in names, f"Superset returned {names}"
    assert "rpt_encounters" in names
    assert payload["count"] == len(names) > 0

    # The registry cross-reference is what ties live Superset ids to governed
    # semantics; if the ids drift, this is where it shows.
    lab = next(d for d in payload["datasets"] if d["table_name"] == "rpt_lab_orders")
    assert lab["in_semantics_registry"] is True
    assert lab["grain"] == "lab_order"

    log = ToolCallLog.objects.get()
    assert log.tool_name == "list_datasets"
    assert log.ok is True
    # The username came from Superset itself, not from a fixture.
    assert log.superset_username == os.environ["SUPERSET_ADMIN_USERNAME"]


# --- The panel bundle ----------------------------------------------------


@pytest.fixture
def debug_urlconf() -> Iterator[None]:
    """Re-import the root URLconf with DEBUG on so static files are routed.

    `lifedata/urls.py` only adds `staticfiles_urlpatterns()` under DEBUG, and it
    is evaluated once at import time — by which point the test runner has
    already forced DEBUG off. Reloading it inside the override is the only way
    to exercise the real URL the panel is served from.
    """
    import lifedata.urls

    with override_settings(DEBUG=True):
        importlib.reload(lifedata.urls)
        clear_url_caches()
        try:
            yield
        finally:
            importlib.reload(lifedata.urls)
            clear_url_caches()


def test_panel_bundle_is_served_at_the_url_nginx_injects(debug_urlconf: None) -> None:
    response = Client().get(PANEL_URL)
    assert response.status_code == 200
    body = b"".join(response.streaming_content).decode("utf-8")
    assert len(body) > 20000, "the served bundle is suspiciously small"
    assert body.startswith("/**")
    assert "__lifedataCopilot" in body


def test_panel_bundle_needs_no_superset_session() -> None:
    """The panel is fetched on the login page, before any session exists.

    Asserted through the middleware's own allowlist rather than through a
    request, so it holds regardless of how static files are served.
    """
    assert PANEL_URL.startswith(tuple(middleware_module.PUBLIC_PATH_PREFIXES))


def _panel_source() -> str:
    return PANEL_PATH.read_text(encoding="utf-8")


def test_panel_calls_the_endpoints_it_must_call() -> None:
    source = _panel_source()
    assert '"/lifedata-ai"' in source
    assert '"/api/chat/"' in source
    assert '"/api/v1/security/csrf_token/"' in source
    # The chat turn is a POST with a JSON body read as a stream; EventSource
    # cannot do that, so a regression to EventSource would break the contract.
    assert "EventSource(" not in source
    assert "credentials: \"same-origin\"" in source


@pytest.mark.parametrize(
    "event_name",
    ["message_start", "text_delta", "tool_use", "tool_result", "message_stop", "error"],
)
def test_panel_handles_every_event_the_loop_emits(event_name: str) -> None:
    """The panel and the loop are edited separately; this catches the drift.

    Crude by design — it greps for the event name as a switch case. It cannot
    prove the handler is correct, only that the name is still known to the
    panel, which is the realistic failure mode.
    """
    assert f'case "{event_name}":' in _panel_source()


def test_loop_emits_no_event_the_panel_ignores() -> None:
    """Every event name the loop yields is handled, or deliberately unknown."""
    loop_source = (_REPO_ROOT / "lifedata_ai" / "agent" / "loop.py").read_text(
        encoding="utf-8"
    )
    panel = _panel_source()
    for name in sorted(PANEL_EVENTS):
        assert f'case "{name}":' in panel, f"the panel does not handle {name}"

    # And nothing the loop emits is missing from the declared set. `_event()`
    # asserts membership under DEBUG, so this is a second, static check of the
    # same property — cheap, and it covers a code path a test happened not to
    # exercise.
    scraped = set(re.findall(r'_event\(\s*"([a-z_]+)"', loop_source))
    assert scraped <= PANEL_EVENTS, f"undeclared events: {sorted(scraped - PANEL_EVENTS)}"


def test_the_event_contract_is_declared_not_scraped() -> None:
    """Why `PANEL_EVENTS` exists rather than a regex over `loop.py`.

    The previous version of the test above scraped `_event("…")` out of the
    source. It could not see `action_preview` at all, because that name was
    emitted through a conditional expression:

        yield _event(
            "action_envelope" if envelope.get("risk") == "low" else "action_preview",
            ...
        )

    So the test that existed to prove the panel handled every event was
    structurally incapable of noticing the one event most likely to be
    forgotten — and it would have gone on passing.

    A declared set cannot have that failure mode, which is the point. This test
    guards the guard: it fails if `PANEL_EVENTS` is ever emptied or narrowed to
    the point of being decorative.
    """
    assert len(PANEL_EVENTS) >= 10
    assert {"action_preview", "clarification"} <= PANEL_EVENTS


#: Every DOM sink that parses a string as HTML, plus the two that parse it as
#: code. Model output reaches the panel as untrusted text, so none of them may
#: appear in it. `innerHTML` alone is not enough of a guard: swapping it for
#: `insertAdjacentHTML` is the obvious way to "fix" a failure of this test while
#: reintroducing exactly the hole it exists to prevent.
HTML_AND_CODE_SINKS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "srcdoc",
    "eval(",
    "new Function(",
)


def test_panel_renders_model_output_as_text_not_html() -> None:
    """Model output is untrusted input; innerHTML would make it executable.

    Checked against the comment-stripped skeleton, because the file's own header
    documents the rule in prose ("rendered with textContent, never innerHTML")
    and a naive substring search would match that comment and fail on a file that
    is in fact correct.
    """
    code = strip_js_literals(_panel_source())
    for sink in HTML_AND_CODE_SINKS:
        assert sink not in code, f"panel.js reaches for {sink} on untrusted model output"
    assert "textContent" in code, "nothing in panel.js sets text safely any more"


def test_the_xss_guard_is_not_blinded_by_its_own_comment_stripping() -> None:
    """Prove the guard above still fires once the stripping has run.

    `panel.js` mentions `innerHTML` in its own header comment, to say it is not
    used. `strip_js_literals` removes that — correct, but it means the guard
    passes on a file that contains the string, so its sensitivity has to be
    demonstrated rather than assumed. A stripper that over-matched and swallowed
    real code would otherwise leave the XSS guard permanently, invisibly green.

    The real source is used only as the *base* to inject into, never asserted
    against for prose, so editing panel.js's comments cannot break this.
    """
    # Mentions in a comment or a string are masked, which is why the guard needs
    # this test at all.
    assert "innerHTML" not in strip_js_literals("/* never use innerHTML here */ let a = 1;")
    assert "innerHTML" not in strip_js_literals('const warning = "do not use innerHTML";')

    # A real assignment, appended to the real bundle, is always caught.
    source = _panel_source()
    for sink in HTML_AND_CODE_SINKS:
        name = sink.rstrip("(")
        injected = f"{source}\nfunction render(t) {{ node.{name} = t; }}\n"
        assert name in strip_js_literals(injected), (
            f"the guard would not notice {name} in real code"
        )


# --- Structural check of the bundle --------------------------------------
#
# There is no JavaScript engine in the orchestrator container (no node, no
# nodejs), so the bundle CANNOT be parsed here and this file does not claim to
# have parsed it. What follows is a bracket-balance scan over the source with
# comments, string literals and regular-expression literals removed. It catches
# truncation and unbalanced edits — the failure modes that matter for a file
# assembled by hand — and nothing else. A syntax error that happens to keep the
# brackets balanced will pass.

#: Characters after which a `/` is division rather than the start of a regex.
_DIVISION_PRECEDERS = ")]}"


def strip_js_literals(source: str) -> str:
    """Return `source` with comments, strings and regex literals blanked out.

    The only genuinely ambiguous case in JavaScript lexing is `/`: it starts a
    regular expression in expression position and means division otherwise. The
    standard heuristic is used — look back at the last significant character —
    which is correct for this file (every `/` in it is either a comment, a
    regex, or inside a string).
    """
    out: list[str] = []
    index = 0
    length = len(source)
    previous = ""  # last significant code character emitted

    while index < length:
        char = source[index]

        if char == "/" and index + 1 < length and source[index + 1] == "/":
            index = source.find("\n", index)
            if index == -1:
                break
            continue
        if char == "/" and index + 1 < length and source[index + 1] == "*":
            end = source.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        if char in "\"'`":
            quote = char
            index += 1
            while index < length:
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == quote:
                    index += 1
                    break
                index += 1
            previous = "x"  # a string is a value, like an identifier
            continue
        if char == "/" and not (previous.isalnum() or previous in _DIVISION_PRECEDERS):
            index += 1
            in_class = False
            while index < length:
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == "[":
                    in_class = True
                elif source[index] == "]":
                    in_class = False
                elif source[index] == "/" and not in_class:
                    index += 1
                    break
                index += 1
            while index < length and source[index].isalpha():  # regex flags
                index += 1
            previous = "x"
            continue

        out.append(char)
        if not char.isspace():
            previous = char
        index += 1

    return "".join(out)


def test_panel_bundle_is_structurally_intact() -> None:
    """Balanced brackets and a complete tail. NOT a parse — see the note above."""
    source = _panel_source()
    skeleton = strip_js_literals(source)

    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    for char in skeleton:
        if char in "([{":
            stack.append(char)
        elif char in pairs:
            assert stack, f"unbalanced closing {char!r} in panel.js"
            assert stack.pop() == pairs[char], f"mismatched {char!r} in panel.js"
    assert not stack, f"panel.js has {len(stack)} unclosed bracket(s): {stack}"

    # An IIFE that ends exactly where it should; a truncated download or a bad
    # merge almost always loses this.
    assert source.rstrip().endswith("})();")
    assert source.count("(function () {") >= 1
    assert "\x00" not in source


def test_structural_scanner_detects_the_damage_it_claims_to() -> None:
    """The scanner is only useful if it actually fails on broken input."""
    assert strip_js_literals('var a = "/*"; { }').count("{") == 1
    assert strip_js_literals("// { { {\nvar a = 1;").count("{") == 0
    assert strip_js_literals('x.replace(/[{}]/g, "");').count("{") == 0
    assert strip_js_literals("var ratio = total / count;").count("/") == 1
    truncated = strip_js_literals("function f() { if (x) {")
    assert truncated.count("{") - truncated.count("}") == 2


# --- Mock server self-checks ---------------------------------------------
#
# The harness is only trustworthy if it is itself checked: a mock that silently
# stops recording, or answers a scripted request twice, would turn every test
# above green for the wrong reason.


@pytest.mark.django_db
def test_mock_records_every_request_it_receives(
    client_as_analyst: Client, anthropic_env: MockAnthropicServer
) -> None:
    """Recording is what every request-shape assertion above rests on.

    Three properties have to hold, and only the first is obvious:

    * every POST is recorded, in order;
    * the body is recorded *verbatim*, not summarised — otherwise an assertion
      that some key is absent would be measuring the recorder, not the loop;
    * recording happens *before* routing and *before* the scripted turn is
      chosen, so a request that 404s or that draws an error turn is captured too.

    The third is the one that would rot silently: move the ``record()`` call
    below the path check and every failure-path test loses its evidence while
    staying green.
    """
    question = "What forms exist?"
    anthropic_env.script(
        tool_use_turn("list_forms", {}),
        text_turn("Done."),
    )
    post_chat(client_as_analyst, question)

    assert anthropic_env.request_count == 2
    assert all(isinstance(r, MockRecordedRequest) for r in anthropic_env.requests)
    assert all(r.body["stream"] is True for r in anthropic_env.requests)
    assert all(r.method == "POST" for r in anthropic_env.requests)
    assert all(r.path.startswith("/v1/messages") for r in anthropic_env.requests)
    assert anthropic_env.last_request is anthropic_env.requests[-1]

    # Verbatim, not summarised: the user's own words survive into the record,
    # and the two rounds are distinct objects rather than one aliased body.
    first, second = anthropic_env.requests
    assert question in json.dumps(first.body)
    assert first.body is not second.body
    assert len(second.messages) > len(first.messages), "round two was not recorded separately"

    # The SDK's own headers, proving the real client made the call.
    assert anthropic_env.requests[0].headers["x-api-key"]
    assert anthropic_env.requests[0].headers["anthropic-version"]
    # Headers are normalised to lower case, so assertions need not guess casing.
    assert all(key == key.lower() for key in anthropic_env.requests[0].headers)

    # A POST the server refuses to route is still recorded. Sent with the
    # standard library so this holds no matter what HTTP client the SDK uses.
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(  # noqa: S310 - loopback, this fixture's own server
            urllib.request.Request(
                f"{anthropic_env.base_url}/v1/not-a-real-endpoint",
                data=json.dumps({"probe": True}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )
    assert refused.value.code == 404, "the mock answers paths it was never taught"

    assert anthropic_env.request_count == 3
    unrouted = anthropic_env.last_request
    assert unrouted.path == "/v1/not-a-real-endpoint"
    assert unrouted.body == {"probe": True}
    # Unroutable, so it consumed no scripted turn.
    assert unrouted.tools == [] and unrouted.tool_names == []


@pytest.mark.django_db
def test_exhausted_script_fails_loudly_rather_than_looping(
    client_as_analyst: Client, anthropic_env: MockAnthropicServer
) -> None:
    """A loop that asks for more turns than scripted must not hang or invent one.

    The bound on `request_count` is the half of this that matters. Without it the
    test passes just as happily when the loop burns all
    ``LIFEDATA_AI_MAX_TOOL_ROUNDS`` rounds before giving up — a runaway loop still
    ends in an error frame, so the frame assertions alone cannot tell the two
    apart. Exactly two rounds: the scripted `tool_use`, then the round that finds
    the queue empty.
    """
    anthropic_env.script(tool_use_turn("list_forms", {}))
    status, frames = post_chat(client_as_analyst, "What forms exist?")

    assert status == 200
    assert anthropic_env.request_count == 2, "the loop kept asking after the script ran dry"
    assert frames[-2].event == "error"
    assert "script exhausted" in frames[-2].data["message"]
    assert frames[-2].data["code"] == "anthropic_error"
    # The stream is still closed properly, so the panel leaves its typing state.
    assert frames[-1].event == "message_stop"
    assert frames[-1].data["stop_reason"] == "error"

    # The tool round before the failure really happened and was audited; the
    # turn is only half-finished, not discarded.
    assert ToolCallLog.objects.get().tool_name == "list_forms"
