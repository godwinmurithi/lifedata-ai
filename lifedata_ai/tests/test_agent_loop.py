"""Tests for the Anthropic tool-use loop (`lifedata_ai/agent/loop.py`, spec §5).

The loop is the one place where every other component meets: the prompt, the
tool registry, the audit trail and the SSE contract the panel depends on. It is
also the one place that cannot be exercised for real — the Anthropic account
backing this project has no credit, so every call returns
``400 Your credit balance is too low to access the Anthropic API``. These tests
are therefore the *only* evidence the loop works, and they are written to pin
the properties that would otherwise fail silently in production:

* **Request shape.** `temperature`, `top_p`, `top_k` and `thinking` all return
  400 on the current models. `TestRequestShape` is the guard that stops a future
  edit reintroducing one; it fails at commit time instead of at answer time.
* **The tool round-trip.** All `tool_result` blocks belong in a *single* user
  message. Splitting them across several messages is a documented mistake — it
  trains the model out of parallel tool calls — and it is invisible unless
  asserted.
* **Every abnormal ending is an event, not a traceback.** Refusal, provider
  error, exhausted round budget, missing key, timeout: the panel must always see
  `error` followed by `message_stop`, because a stream that simply stops is
  indistinguishable from a dropped connection in the browser.

The scripted Anthropic client, identities and conversation come from
`conftest.py`; an autouse guard there makes a real API call impossible.
"""

from __future__ import annotations

from typing import Any

import pytest
from anthropic.types import TextBlock

from lifedata_ai.agent.context import PageContext
from lifedata_ai.agent.loop import (
    _MAX_PROVIDER_RETRIES,
    MAX_HISTORY_MESSAGES,
    MAX_TOKENS,
    AgentEvent,
    ConversationNotFound,
    _summarise_tool_call,
    resolve_conversation,
    run_turn,
)
from lifedata_ai.middleware import SupersetIdentity
from lifedata_ai.models import Conversation, Message, ToolCallLog
from lifedata_ai.tests.conftest import (
    CREDIT_BALANCE_TOO_LOW,
    FAKE_SESSION_COOKIE,
    FakeAnthropic,
    thinking_block,
)
from lifedata_ai.tests.conftest import system_text as _system_text
from lifedata_ai.tools import Tool, ToolContext, ToolError, ToolMode, ToolRegistry

pytestmark = pytest.mark.django_db

#: The complete, current tool set — read AND write. Hard-coded rather than
#: derived from the registry so adding or losing a tool is a deliberate edit
#: here, not a silent change in what the model is offered.
EXPECTED_TOOL_COUNT = 25

#: Parameters that are 400s on claude-opus-5 and must never be sent.
FORBIDDEN_REQUEST_KEYS = ("temperature", "top_p", "top_k", "thinking")


# --- Stub tools ----------------------------------------------------------


class _EchoTool(Tool):
    """A network-free tool whose output is unmistakably its own.

    Echoing the caller's username as well as the argument proves two things at
    once: that `run` really executed, and that the loop built the `ToolContext`
    from the verified identity rather than from anything the model said.
    """

    name = "echo_value"
    description = "Echo a value back. Test double for a Superset read tool."
    mode = ToolMode.READ
    input_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, value: str) -> dict[str, Any]:
        return {"echoed": value, "caller": context.username}


class _RefusingTool(Tool):
    """Fails the way a tool is *supposed* to fail: a `ToolError` the model can
    correct from."""

    name = "refusing_tool"
    description = "Always raises ToolError."
    mode = ToolMode.READ
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext) -> dict[str, Any]:
        raise ToolError("dataset 99 does not exist", code="not_found")


class _CrashingTool(Tool):
    """Fails the way a tool is *not* supposed to fail. The turn must survive it
    anyway — a bug in one tool cannot cost the user their answer."""

    name = "crashing_tool"
    description = "Always raises an unexpected exception."
    mode = ToolMode.READ
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext) -> dict[str, Any]:
        raise RuntimeError("divided by zero somewhere deep")


@pytest.fixture
def stub_registry(monkeypatch: pytest.MonkeyPatch) -> ToolRegistry:
    """Swap the real registry for stub tools that touch no network.

    `execute()` is deliberately *not* stubbed: it is the only path that writes
    `ToolCallLog`, and several tests below assert on those rows.
    """
    registry = ToolRegistry()
    registry.register(_EchoTool())
    registry.register(_RefusingTool())
    registry.register(_CrashingTool())
    monkeypatch.setattr("lifedata_ai.agent.loop.get_registry", lambda: registry)
    return registry


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the provider-retry backoff.

    The loop sleeps 2s, 4s, 8s, 8s between retries. That is right in production
    and intolerable in a test suite — exercising the exhausted-retry path would
    cost 22 seconds per case. Only `agent.loop`'s own `time.sleep` is patched,
    so `time.monotonic` (and therefore every deadline assertion) is untouched.
    """
    monkeypatch.setattr("lifedata_ai.agent.loop.time.sleep", lambda _seconds: None)


# --- Helpers -------------------------------------------------------------


@pytest.fixture
def run(
    conversation: Conversation,
    identity: SupersetIdentity,
    page_context: PageContext,
):
    """Drain a turn into a list of events, with the standard fixtures wired."""

    def _run(
        user_message: str = "How many encounters last month?",
        *,
        target: Conversation | None = None,
        who: SupersetIdentity | None = None,
    ) -> list[AgentEvent]:
        return list(
            run_turn(
                conversation=target or conversation,
                identity=who or identity,
                superset_session_cookie=FAKE_SESSION_COOKIE,
                page_context=page_context,
                user_message=user_message,
            )
        )

    return _run


def names(events: list[AgentEvent]) -> list[str]:
    return [event.name for event in events]


def of_name(events: list[AgentEvent], name: str) -> list[AgentEvent]:
    return [event for event in events if event.name == name]


def only(events: list[AgentEvent], name: str) -> AgentEvent:
    matches = of_name(events, name)
    assert len(matches) == 1, f"expected exactly one {name!r} event, got {len(matches)}"
    return matches[0]


def streamed_text(events: list[AgentEvent]) -> str:
    return "".join(event.data["text"] for event in of_name(events, "text_delta"))


# --- A plain text turn ---------------------------------------------------


class TestPlainTextTurn:
    def test_events_arrive_in_order(self, fake_anthropic: FakeAnthropic, run) -> None:
        fake_anthropic.queue_text("Encounters rose in March.")

        events = run()

        assert names(events) == [
            "message_start",
            "text_delta",
            "text_delta",
            "text_delta",
            "text_delta",
            "message_stop",
        ]
        assert streamed_text(events) == "Encounters rose in March."

    def test_message_start_and_stop_carry_the_conversation_id(
        self, fake_anthropic: FakeAnthropic, run, conversation: Conversation
    ) -> None:
        fake_anthropic.queue_text("Done.")

        events = run()

        assert events[0].data == {"conversation_id": str(conversation.pk)}
        assert events[-1].data == {
            "stop_reason": "end_turn",
            "conversation_id": str(conversation.pk),
        }

    def test_only_text_deltas_are_relayed(
        self, fake_anthropic: FakeAnthropic, run
    ) -> None:
        """Thinking and input-json deltas stream too; relaying them would leak
        the model's reasoning into the answer pane."""
        fake_anthropic.queue_text("Short.")

        events = run()

        assert streamed_text(events) == "Short."
        assert "weighing" not in streamed_text(events)

    def test_one_model_round_for_a_question_needing_no_tools(
        self, fake_anthropic: FakeAnthropic, run
    ) -> None:
        fake_anthropic.queue_text("No tools needed.")

        run()

        assert fake_anthropic.call_count == 1


# --- The tool round-trip -------------------------------------------------


class TestToolRoundTrip:
    def test_tool_runs_and_its_real_output_is_fed_back(
        self, fake_anthropic: FakeAnthropic, stub_registry: ToolRegistry, run
    ) -> None:
        fake_anthropic.queue_tool_use([("toolu_1", "echo_value", {"value": "Gasabo"})])
        fake_anthropic.queue_text("Gasabo it is.")

        events = run()

        assert fake_anthropic.call_count == 2
        second = fake_anthropic.requests[1]
        assert second.roles == ["user", "assistant", "user"]

        results = second["messages"][2]["content"]
        assert len(results) == 1
        block = results[0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "toolu_1"
        # The tool genuinely ran: this text exists nowhere but in _EchoTool.run.
        assert '"echoed": "Gasabo"' in block["content"]
        assert '"caller": "analyst"' in block["content"]
        assert "is_error" not in block

        assert names(events) == [
            "message_start",
            "tool_use",
            "tool_result",
            "text_delta",
            "text_delta",
            "text_delta",
            "message_stop",
        ]

    def test_parallel_results_share_one_user_message(
        self, fake_anthropic: FakeAnthropic, stub_registry: ToolRegistry, run
    ) -> None:
        """Splitting tool results across several user messages is the documented
        mistake: it teaches the model that parallel calls are not honoured."""
        fake_anthropic.queue_tool_use(
            [
                ("toolu_a", "echo_value", {"value": "first"}),
                ("toolu_b", "echo_value", {"value": "second"}),
            ]
        )
        fake_anthropic.queue_text("Both read.")

        run()

        second = fake_anthropic.requests[1]
        assert second.roles == ["user", "assistant", "user"]
        results = second["messages"][2]["content"]
        assert [block["tool_use_id"] for block in results] == ["toolu_a", "toolu_b"]

        tool_rows = Message.objects.filter(role=Message.Role.TOOL)
        assert tool_rows.count() == 1, "results must be one message, not one message each"
        assert len(tool_rows.get().content) == 2

    def test_assistant_turn_is_replayed_before_the_results(
        self, fake_anthropic: FakeAnthropic, stub_registry: ToolRegistry, run
    ) -> None:
        """The `tool_use` blocks must come back verbatim; the API rejects a
        `tool_result` whose matching `tool_use` is absent."""
        fake_anthropic.queue_tool_use(
            [("toolu_1", "echo_value", {"value": "x"})], text="Let me look."
        )
        fake_anthropic.queue_text("Here.")

        run()

        assistant = fake_anthropic.requests[1]["messages"][1]
        assert assistant["role"] == "assistant"
        assert [block["type"] for block in assistant["content"]] == ["text", "tool_use"]
        assert assistant["content"][1]["id"] == "toolu_1"
        assert assistant["content"][1]["input"] == {"value": "x"}

    def test_tool_use_event_summarises_the_call(
        self, fake_anthropic: FakeAnthropic, stub_registry: ToolRegistry, run
    ) -> None:
        fake_anthropic.queue_tool_use([("toolu_1", "echo_value", {"value": "x"})])
        fake_anthropic.queue_text("Done.")

        events = run()

        tool_use = only(events, "tool_use")
        assert tool_use.data == {
            "id": "toolu_1",
            "name": "echo_value",
            "summary": "echo value",
        }

    def test_summary_includes_a_safe_identifying_argument(self) -> None:
        assert _summarise_tool_call("get_dataset", {"dataset_id": 2}) == "inspecting dataset 2"
        # Untrusted argument values are cleaned before they reach the panel.
        # `chart_name` rather than `name`: `name` carries a registry identifier
        # and is now resolved to a label or dropped, so it would no longer
        # exercise the cleaning this line is about.
        assert "\n" not in _summarise_tool_call("create_chart", {"chart_name": "a\nb"})

    def test_an_indicator_lookup_names_it_in_english(self) -> None:
        """The activity line is addressed to the user, so the same rule applies.

        An acceptance run caught "looking up the indicator definition
        lab_turnaround_hours" and reported it as the copilot leaking schema
        vocabulary. It was not the copilot — the panel renders `data.summary`,
        and this function builds it.
        """
        summary = _summarise_tool_call("explain_indicator", {"name": "lab_turnaround_hours"})

        assert summary == "looking up the indicator definition Average turnaround (hours)"
        assert "lab_turnaround_hours" not in summary

    def test_an_unresolvable_identifier_is_dropped_rather_than_shown(self) -> None:
        """Vaguer is better than wrong.

        A name that matches no metric — a typo, or an indicator with no saved
        metric citing it — must not fall back to printing the raw string.
        """
        assert (
            _summarise_tool_call("explain_indicator", {"name": "no_such_indicator"})
            == "looking up the indicator definition"
        )


# --- Request shape -------------------------------------------------------


class TestRequestShape:
    """Pins the exact keywords sent to Anthropic.

    Every assertion here corresponds to something that has, or would, return a
    400 on the current models. The real registry is used deliberately: the tool
    specs are part of the request shape.
    """

    @pytest.fixture(autouse=True)
    def _one_text_turn(self, fake_anthropic: FakeAnthropic, run) -> None:
        fake_anthropic.queue_text("Fine.")
        run()

    def test_model_comes_from_settings(
        self, fake_anthropic: FakeAnthropic, settings
    ) -> None:
        assert fake_anthropic.last_request["model"] == settings.LIFEDATA_AI_MODEL

    def test_max_tokens_is_generous_enough_for_thinking_plus_answer(
        self, fake_anthropic: FakeAnthropic
    ) -> None:
        assert fake_anthropic.last_request["max_tokens"] == MAX_TOKENS == 32000

    def test_effort_travels_inside_output_config(
        self, fake_anthropic: FakeAnthropic, settings
    ) -> None:
        """`effort` is not a top-level parameter; sending it as one is a 400."""
        assert fake_anthropic.last_request["output_config"] == {
            "effort": settings.LIFEDATA_AI_EFFORT
        }
        assert "effort" not in fake_anthropic.last_request

    def test_the_call_streams(self, fake_anthropic: FakeAnthropic) -> None:
        """`messages.stream()` *is* ``stream=True``; the conftest double raises if
        the loop ever calls the buffered `create()` instead. Passing `stream`
        explicitly to `.stream()` is a TypeError, so it must be absent."""
        assert fake_anthropic.call_count == 1
        assert "stream" not in fake_anthropic.last_request

    def test_no_sampling_or_thinking_parameters(
        self, fake_anthropic: FakeAnthropic
    ) -> None:
        for key in FORBIDDEN_REQUEST_KEYS:
            assert key not in fake_anthropic.last_request, (
                f"{key} returns 400 on claude-opus-5 and must never be sent"
            )

    def test_every_offered_tool_schema_is_a_closed_object(
        self, fake_anthropic: FakeAnthropic
    ) -> None:
        """Strict mode is asserted per-tool in `test_tools.py`; what must hold
        for *every* tool is that the schema the API sees is a closed object with
        no top-level union (both of which are outright 400s)."""
        tools = fake_anthropic.last_request["tools"]

        assert len(tools) == EXPECTED_TOOL_COUNT
        for spec in tools:
            schema = spec["input_schema"]
            assert schema["additionalProperties"] is False
            assert not {"oneOf", "anyOf", "allOf"} & set(schema), (
                f"{spec['name']} exports a top-level union, which the API refuses"
            )

    def test_the_write_tools_are_offered(self, fake_anthropic: FakeAnthropic) -> None:
        """The copilot proposes changes; the panel executes them (spec §1).

        A build that registers write tools but does not offer them is the
        failure this pins: the model then describes charts instead of building
        them, and nothing in the logs says why.
        """
        offered = {spec["name"] for spec in fake_anthropic.last_request["tools"]}

        assert {"create_chart", "update_chart", "create_dashboard"} <= offered

    def test_system_prompt_carries_the_clinical_hard_rules(
        self, fake_anthropic: FakeAnthropic
    ) -> None:
        system = fake_anthropic.last_request["system"]
        assert "No diagnosis, no treatment, no clinical advice" in _system_text(system)

    def test_timeout_is_the_remaining_turn_budget(
        self, fake_anthropic: FakeAnthropic, settings
    ) -> None:
        timeout = fake_anthropic.last_request["timeout"]
        assert 0 < timeout <= settings.LIFEDATA_AI_TURN_TIMEOUT_SECONDS

    def test_the_key_from_settings_is_the_one_used(
        self, fake_anthropic: FakeAnthropic, settings
    ) -> None:
        assert fake_anthropic.last_request.api_key == settings.ANTHROPIC_API_KEY


# --- Refusal -------------------------------------------------------------


class TestRefusal:
    def test_empty_refusal_content_does_not_crash_the_turn(
        self, fake_anthropic: FakeAnthropic, run
    ) -> None:
        """A refusal is an HTTP 200 whose `content` may be empty. Code that reads
        `content[0]` before checking `stop_reason` raises IndexError here."""
        fake_anthropic.queue_refusal()

        events = run()

        assert names(events) == ["message_start", "error", "message_stop"]
        assert only(events, "error").data["code"] == "refusal"
        assert events[-1].data["stop_reason"] == "refusal"

    def test_refusal_message_is_actionable(
        self, fake_anthropic: FakeAnthropic, run
    ) -> None:
        fake_anthropic.queue_refusal()

        message = only(run(), "error").data["message"]

        assert "declined" in message
        assert "Rephrasing" in message

    def test_refusal_is_still_persisted_for_audit(
        self, fake_anthropic: FakeAnthropic, run, conversation: Conversation
    ) -> None:
        fake_anthropic.queue_refusal(request_id="req_refused_1")

        run()

        assistant = conversation.messages.get(role=Message.Role.ASSISTANT)
        assert assistant.content == []
        assert assistant.anthropic_request_id == "req_refused_1"

    def test_refusal_with_content_keeps_the_blocks(
        self, fake_anthropic: FakeAnthropic, run, conversation: Conversation
    ) -> None:
        fake_anthropic.queue_refusal(content=[TextBlock(text="I cannot help.", type="text")])

        run()

        assistant = conversation.messages.get(role=Message.Role.ASSISTANT)
        assert assistant.content == [{"text": "I cannot help.", "type": "text"}]


# --- Provider errors -----------------------------------------------------


class TestProviderErrors:
    def test_credit_balance_too_low_is_surfaced_verbatim(
        self, fake_anthropic: FakeAnthropic, run
    ) -> None:
        """The failure this project actually hits. A generic "the model request
        failed" would send whoever reads it looking in the wrong place, so the
        provider's own wording is relayed."""
        fake_anthropic.queue_status_error(400, CREDIT_BALANCE_TOO_LOW)

        events = run()

        assert names(events) == ["message_start", "error", "message_stop"]
        error = only(events, "error")
        assert error.data["code"] == "anthropic_error"
        assert error.data["message"].startswith("The Anthropic API rejected the request:")
        assert "Your credit balance is too low" in error.data["message"]
        assert events[-1].data["stop_reason"] == "error"

    def test_a_provider_error_leaves_no_assistant_message(
        self, fake_anthropic: FakeAnthropic, run, conversation: Conversation
    ) -> None:
        fake_anthropic.queue_status_error(400, CREDIT_BALANCE_TOO_LOW)

        run()

        # The user's message is still recorded: the turn happened, it just failed.
        assert list(conversation.messages.values_list("role", flat=True)) == ["user"]

    @pytest.mark.parametrize(
        ("status_code", "fragment"),
        [
            (401, "ANTHROPIC_API_KEY"),
            (429, "rate limited"),
            (500, "temporarily unavailable"),
            (529, "temporarily unavailable"),
            (404, "the model request failed"),
        ],
    )
    def test_each_status_gets_an_operator_useful_message(
        self, fake_anthropic: FakeAnthropic, run, status_code: int, fragment: str
    ) -> None:
        # 429/500/529 are retried, so the message the user finally sees is only
        # produced once the retry budget is spent. Queue enough failures to
        # exhaust it; the non-retryable codes simply never reach the extras.
        fake_anthropic.queue_status_error(
            status_code, "upstream said no", repeat=_MAX_PROVIDER_RETRIES + 1
        )

        message = only(run(), "error").data["message"]

        assert fragment in message

    def test_unreachable_api_is_its_own_code(
        self, fake_anthropic: FakeAnthropic, run
    ) -> None:
        fake_anthropic.queue_connection_error()

        error = only(run(), "error")

        assert error.data["code"] == "anthropic_unreachable"
        assert "Could not reach the Anthropic API" in error.data["message"]

    def test_the_request_shape_is_still_inspectable_after_a_400(
        self, fake_anthropic: FakeAnthropic, run
    ) -> None:
        """Guards the guard: if a rejected parameter were reintroduced, the 400 it
        provoked must not also hide the request that caused it."""
        fake_anthropic.queue_status_error(400, CREDIT_BALANCE_TOO_LOW)

        run()

        assert fake_anthropic.call_count == 1
        for key in FORBIDDEN_REQUEST_KEYS:
            assert key not in fake_anthropic.last_request


# --- Missing key ---------------------------------------------------------


class TestMissingApiKey:
    def test_one_clear_error_and_no_network_call(
        self, fake_anthropic: FakeAnthropic, run, settings
    ) -> None:
        settings.ANTHROPIC_API_KEY = ""

        events = run()

        assert names(events) == ["message_start", "error", "message_stop"]
        error = only(events, "error")
        assert error.data["code"] == "anthropic_key_missing"
        assert "ANTHROPIC_API_KEY" in error.data["message"]
        assert fake_anthropic.call_count == 0, "no key means no request may be attempted"

    def test_the_turn_is_still_audited(
        self, fake_anthropic: FakeAnthropic, run, settings, conversation: Conversation
    ) -> None:
        """The user's message is persisted before the key is checked, so a
        misconfigured deployment still leaves a trail of what was asked."""
        settings.ANTHROPIC_API_KEY = ""

        run("what datasets exist?")

        conversation.refresh_from_db()
        assert conversation.title == "what datasets exist?"
        assert conversation.messages.get().content == [
            {"type": "text", "text": "what datasets exist?"}
        ]


# --- Round and time budgets ----------------------------------------------


class TestBudgets:
    def test_endless_tool_use_is_stopped(
        self, fake_anthropic: FakeAnthropic, stub_registry: ToolRegistry, run, settings
    ) -> None:
        settings.LIFEDATA_AI_MAX_TOOL_ROUNDS = 2
        fake_anthropic.queue_tool_use(
            [("toolu_x", "echo_value", {"value": "again"})], repeat=3
        )

        events = run()

        error = only(events, "error")
        assert error.data["code"] == "tool_round_limit"
        assert "limit of 2 tool rounds" in error.data["message"]
        assert events[-1].data["stop_reason"] == "tool_round_limit"
        # Three model rounds, but the third is refused before its tools run.
        assert fake_anthropic.call_count == 3
        assert ToolCallLog.objects.count() == 2
        assert fake_anthropic.pending == 0

    def test_repeated_pause_turn_ends_the_stream_cleanly(
        self, fake_anthropic: FakeAnthropic, run, settings
    ) -> None:
        """`pause_turn` continues without consuming a tool round, so only the
        for-else branch stops it. Rare, but it must not end silently."""
        settings.LIFEDATA_AI_MAX_TOOL_ROUNDS = 2
        fake_anthropic.queue_pause_turn(repeat=3)

        events = run()

        error = only(events, "error")
        assert error.data["code"] == "round_limit"
        assert events[-1].data["stop_reason"] == "round_limit"
        assert fake_anthropic.call_count == 3

    def test_pause_turn_resumes_without_owing_tool_results(
        self, fake_anthropic: FakeAnthropic, run
    ) -> None:
        fake_anthropic.queue_pause_turn(text="one moment")
        fake_anthropic.queue_text("Here it is.")

        events = run()

        assert fake_anthropic.requests[1].roles == ["user", "assistant"]
        assert events[-1].data["stop_reason"] == "end_turn"
        assert streamed_text(events) == "one momentHere it is."

    def test_an_exhausted_time_budget_stops_before_calling(
        self, fake_anthropic: FakeAnthropic, run, settings
    ) -> None:
        settings.LIFEDATA_AI_TURN_TIMEOUT_SECONDS = 0

        events = run()

        assert only(events, "error").data["code"] == "turn_timeout"
        assert fake_anthropic.call_count == 0

    def test_max_tokens_warns_that_the_answer_may_be_incomplete(
        self, fake_anthropic: FakeAnthropic, run
    ) -> None:
        fake_anthropic.queue_text("A partial answer", stop_reason="max_tokens")

        events = run()

        assert only(events, "error").data["code"] == "max_tokens"
        assert events[-1].data["stop_reason"] == "max_tokens"
        # The partial text still reached the user; it is a warning, not a discard.
        assert streamed_text(events) == "A partial answer"


# --- Tool failures -------------------------------------------------------


class TestToolFailures:
    def test_tool_error_becomes_an_is_error_result(
        self, fake_anthropic: FakeAnthropic, stub_registry: ToolRegistry, run
    ) -> None:
        fake_anthropic.queue_tool_use([("toolu_1", "refusing_tool", {})])
        fake_anthropic.queue_text("I will try another dataset.")

        events = run()

        assert only(events, "tool_result").data == {
            "tool_use_id": "toolu_1",
            "name": "refusing_tool",
            "ok": False,
            "is_error": True,
        }
        block = fake_anthropic.requests[1]["messages"][2]["content"][0]
        assert block["is_error"] is True
        assert "dataset 99 does not exist" in block["content"]
        # The turn continued rather than dying.
        assert events[-1].data["stop_reason"] == "end_turn"

    def test_an_unexpected_exception_does_not_kill_the_turn(
        self, fake_anthropic: FakeAnthropic, stub_registry: ToolRegistry, run
    ) -> None:
        fake_anthropic.queue_tool_use([("toolu_1", "crashing_tool", {})])
        fake_anthropic.queue_text("Recovered.")

        events = run()

        block = fake_anthropic.requests[1]["messages"][2]["content"][0]
        assert block["is_error"] is True
        assert "crashing_tool failed unexpectedly" in block["content"]
        assert events[-1].data["stop_reason"] == "end_turn"

    def test_an_unknown_tool_name_is_reported_back_not_raised(
        self, fake_anthropic: FakeAnthropic, stub_registry: ToolRegistry, run
    ) -> None:
        """A hallucinated tool name must reach the model as a correctable result."""
        fake_anthropic.queue_tool_use([("toolu_1", "drop_all_tables", {})])
        fake_anthropic.queue_text("Sorry, I misremembered.")

        events = run()

        block = fake_anthropic.requests[1]["messages"][2]["content"][0]
        assert block["is_error"] is True
        assert "Unknown tool: drop_all_tables" in block["content"]
        assert events[-1].data["stop_reason"] == "end_turn"

    def test_a_mixed_round_reports_each_result_separately(
        self, fake_anthropic: FakeAnthropic, stub_registry: ToolRegistry, run
    ) -> None:
        fake_anthropic.queue_tool_use(
            [
                ("toolu_ok", "echo_value", {"value": "fine"}),
                ("toolu_bad", "refusing_tool", {}),
            ]
        )
        fake_anthropic.queue_text("One of two worked.")

        events = run()

        assert [event.data["ok"] for event in of_name(events, "tool_result")] == [True, False]
        blocks = fake_anthropic.requests[1]["messages"][2]["content"]
        assert "is_error" not in blocks[0]
        assert blocks[1]["is_error"] is True


# --- Persistence and audit -----------------------------------------------


class TestPersistence:
    def test_raw_content_blocks_are_stored_including_thinking(
        self, fake_anthropic: FakeAnthropic, run, conversation: Conversation
    ) -> None:
        """Thinking blocks must be replayable verbatim when the conversation
        continues on the same model, so they are stored, not rendered away."""
        fake_anthropic.queue_text(
            "The answer.", leading_blocks=[thinking_block("checking the grain")]
        )

        run("why?")

        rows = list(conversation.messages.all())
        assert [row.role for row in rows] == ["user", "assistant"]
        assert rows[0].content == [{"type": "text", "text": "why?"}]
        assert rows[1].content == [
            {"signature": "sig", "thinking": "checking the grain", "type": "thinking"},
            {"text": "The answer.", "type": "text"},
        ]

    def test_assistant_message_records_the_anthropic_request_id(
        self, fake_anthropic: FakeAnthropic, run, conversation: Conversation
    ) -> None:
        """Taken from the stream, not from `response._request_id`, which is only
        populated on non-streaming calls (spec §9 traceability)."""
        fake_anthropic.queue_text("Fine.", request_id="req_01ABCDEF")

        run()

        assistant = conversation.messages.get(role=Message.Role.ASSISTANT)
        assert assistant.anthropic_request_id == "req_01ABCDEF"

    def test_a_missing_request_id_is_stored_as_empty_not_none(
        self, fake_anthropic: FakeAnthropic, run, conversation: Conversation
    ) -> None:
        fake_anthropic.queue_text("Fine.", request_id="")

        run()

        assert conversation.messages.get(role=Message.Role.ASSISTANT).anthropic_request_id == ""

    def test_a_tool_turn_writes_three_rows_in_order(
        self, fake_anthropic: FakeAnthropic, stub_registry: ToolRegistry, run,
        conversation: Conversation,
    ) -> None:
        fake_anthropic.queue_tool_use([("toolu_1", "echo_value", {"value": "x"})])
        fake_anthropic.queue_text("Done.")

        run()

        assert list(conversation.messages.values_list("role", flat=True)) == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]

    def test_the_conversation_is_titled_from_the_first_message_only(
        self, fake_anthropic: FakeAnthropic, run, conversation: Conversation
    ) -> None:
        fake_anthropic.queue_text("One.")
        fake_anthropic.queue_text("Two.")

        run("first question")
        run("second question")

        conversation.refresh_from_db()
        assert conversation.title == "first question"

    def test_a_long_first_message_is_truncated_for_the_title(
        self, fake_anthropic: FakeAnthropic, run, conversation: Conversation
    ) -> None:
        fake_anthropic.queue_text("Fine.")

        run("x" * 500)

        conversation.refresh_from_db()
        assert len(conversation.title) <= 120


class TestToolAudit:
    def test_every_invocation_is_logged_with_the_username(
        self, fake_anthropic: FakeAnthropic, stub_registry: ToolRegistry, run,
        conversation: Conversation,
    ) -> None:
        fake_anthropic.queue_tool_use(
            [
                ("toolu_a", "echo_value", {"value": "one"}),
                ("toolu_b", "refusing_tool", {}),
            ]
        )
        fake_anthropic.queue_text("Done.")

        run()

        rows = list(ToolCallLog.objects.all())
        assert [row.tool_name for row in rows] == ["echo_value", "refusing_tool"]
        assert {row.superset_username for row in rows} == {"analyst"}
        assert {row.conversation_id for row in rows} == {conversation.pk}
        assert [row.ok for row in rows] == [True, False]
        assert rows[0].mode == "read"
        assert rows[0].arguments == {"value": "one"}
        assert "dataset 99 does not exist" in rows[1].error

    def test_the_logged_username_is_the_verified_identity(
        self,
        fake_anthropic: FakeAnthropic,
        stub_registry: ToolRegistry,
        run,
        admin_identity: SupersetIdentity,
    ) -> None:
        """The audit row must name whoever Superset verified, not whoever the
        conversation happens to belong to."""
        fake_anthropic.queue_tool_use([("toolu_1", "echo_value", {"value": "x"})])
        fake_anthropic.queue_text("Done.")

        run(who=admin_identity)

        assert ToolCallLog.objects.get().superset_username == "admin"

    def test_no_rows_when_no_tool_is_called(
        self, fake_anthropic: FakeAnthropic, run
    ) -> None:
        fake_anthropic.queue_text("No tools needed.")

        run()

        assert ToolCallLog.objects.count() == 0


# --- History replay ------------------------------------------------------


class TestHistoryReplay:
    def test_earlier_turns_are_replayed(
        self, fake_anthropic: FakeAnthropic, run, conversation: Conversation
    ) -> None:
        Message.objects.create(
            conversation=conversation,
            role=Message.Role.USER,
            content=[{"type": "text", "text": "earlier question"}],
        )
        Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content=[{"type": "text", "text": "earlier answer"}],
        )
        fake_anthropic.queue_text("Fine.")

        run("follow-up")

        assert fake_anthropic.last_request.roles == ["user", "assistant", "user"]
        assert fake_anthropic.last_request["messages"][0]["content"][0]["text"] == (
            "earlier question"
        )

    def test_an_orphaned_leading_tool_result_is_dropped(
        self, fake_anthropic: FakeAnthropic, run, conversation: Conversation
    ) -> None:
        """A `tool_result` whose `tool_use` fell off the front of the window is a
        400 from the API, so the replay must start on a genuine user turn."""
        Message.objects.create(
            conversation=conversation,
            role=Message.Role.TOOL,
            content=[{"type": "tool_result", "tool_use_id": "toolu_gone", "content": "{}"}],
        )
        Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content=[{"type": "text", "text": "orphaned answer"}],
        )
        fake_anthropic.queue_text("Fine.")

        run("fresh question")

        messages = fake_anthropic.last_request["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == [{"type": "text", "text": "fresh question"}]

    def test_the_replayed_window_is_bounded(
        self, fake_anthropic: FakeAnthropic, run, conversation: Conversation
    ) -> None:
        for index in range(MAX_HISTORY_MESSAGES + 5):
            Message.objects.create(
                conversation=conversation,
                role=Message.Role.USER,
                content=[{"type": "text", "text": f"question {index}"}],
            )
        fake_anthropic.queue_text("Fine.")

        run("newest")

        messages = fake_anthropic.last_request["messages"]
        assert len(messages) == MAX_HISTORY_MESSAGES
        assert messages[-1]["content"] == [{"type": "text", "text": "newest"}]

    def test_another_users_conversation_is_never_replayed(
        self, fake_anthropic: FakeAnthropic, run, identity: SupersetIdentity
    ) -> None:
        """Not a loop concern by itself — `resolve_conversation` is the gate — but
        worth pinning that the loop reads history from the conversation it was
        handed and nothing wider."""
        other = Conversation.objects.create(superset_username="someone_else")
        Message.objects.create(
            conversation=other,
            role=Message.Role.USER,
            content=[{"type": "text", "text": "secret"}],
        )
        fake_anthropic.queue_text("Fine.")

        run("mine")

        rendered = str(fake_anthropic.last_request["messages"])
        assert "secret" not in rendered


# --- Conversation resolution ---------------------------------------------


class TestResolveConversation:
    def test_a_new_conversation_records_the_page_scope(
        self, identity: SupersetIdentity, page_context: PageContext
    ) -> None:
        conversation = resolve_conversation(
            identity=identity, conversation_id=None, page_context=page_context
        )

        assert conversation.superset_username == "analyst"
        assert conversation.page_scope == page_context.to_dict()

    def test_an_existing_conversation_is_returned(
        self, identity: SupersetIdentity, conversation: Conversation, page_context: PageContext
    ) -> None:
        resolved = resolve_conversation(
            identity=identity,
            conversation_id=str(conversation.pk),
            page_context=page_context,
        )

        assert resolved.pk == conversation.pk

    def test_another_users_conversation_is_indistinguishable_from_a_missing_one(
        self, admin_identity: SupersetIdentity, conversation: Conversation,
        page_context: PageContext,
    ) -> None:
        """Both raise the same error so the endpoint cannot be used to probe
        whether a given conversation id exists."""
        with pytest.raises(ConversationNotFound):
            resolve_conversation(
                identity=admin_identity,
                conversation_id=str(conversation.pk),
                page_context=page_context,
            )

        with pytest.raises(ConversationNotFound):
            resolve_conversation(
                identity=admin_identity,
                conversation_id="8b1f4c4e-0000-4000-8000-000000000000",
                page_context=page_context,
            )
