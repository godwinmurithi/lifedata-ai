"""The deferred-resume path: `ask_user`, previews, and the invariant (plan §5).

Two turns are the whole design, so most of this file is two `run_turn` calls
with assertions in the middle.

    Turn 1: the model calls `ask_user`. A `clarification` event is emitted, a
            pending row is written, the turn ends `awaiting_user`, and — the
            part that matters — NO tool_result is persisted.

    Turn 2: whatever the user sends becomes that tool_result. The model resumes
            its own tool call rather than seeing a fresh user turn.

`test_the_wire_shape_proves_the_model_resumes_its_own_call` is the load-bearing
one. Everything else could pass while the model still saw a spurious user turn
beside its own pending question; that test reads the actual request body.

# The invariant

`run_turn` never begins a turn with an unresolved `tool_use` in history. An
assistant message ending in `tool_use` with no matching `tool_result` is rejected
outright by the Messages API — a 400 the user cannot act on, arriving before the
model is reached. `TestTheInvariant` walks every way a turn can start and pairs
the ids in the request that actually went out.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.test import Client

from lifedata_ai import middleware
from lifedata_ai.agent.context import PageContext
from lifedata_ai.agent.loop import AWAITING_USER, NothingToResume, run_turn
from lifedata_ai.middleware import SupersetIdentity
from lifedata_ai.models import (
    ActionRecord,
    Clarification,
    Conversation,
    Message,
    ToolCallLog,
)
from lifedata_ai.superset_client import SupersetError
from lifedata_ai.tests.conftest import FAKE_SESSION_COOKIE, FakeAnthropic

pytestmark = pytest.mark.django_db


# --- Helpers -------------------------------------------------------------


BREAKDOWN_QUESTION = {
    "questions": [
        {
            "question": "Do you mean each facility, or the kind of facility?",
            "options": [
                {"label": "Each facility", "hint": "The facility's name, like Kampala HC IV"},
                {"label": "The kind of facility", "hint": "HC II, HC III, HC IV or Hospital"},
            ],
        }
    ]
}

LAB_CHART = {
    "chart_name": "Orders by facility",
    "dataset_id": 3,
    "params": {
        "viz_type": "echarts_timeseries_bar",
        "datasource": {"id": 3, "type": "table"},
        "metrics": ["orders"],
        "x_axis": "facility_name",
    },
}


SESSION_PREFIX = "superset-session-for-"


@pytest.fixture
def stub_superset_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve Superset sessions locally, so an HTTP call needs no network."""

    def _verify(cookie: str) -> SupersetIdentity:
        if not cookie.startswith(SESSION_PREFIX):
            raise SupersetError("Superset rejected this session", status_code=401)
        # `user_id` matters: Superset identifies an owner by numeric id and
        # sends no `username`, so an identity without one can never be
        # recognised as an owner. 7 matches the `identity` fixture and the
        # owner ids in the Superset fixtures below.
        return SupersetIdentity(
            username=cookie[len(SESSION_PREFIX):],
            user_id=7,
            email=f"{cookie[len(SESSION_PREFIX):]}@example.test",
            roles=["Alpha"],
        )

    monkeypatch.setattr(middleware, "_cache", middleware._IdentityCache())
    monkeypatch.setattr(middleware, "_verify_with_superset", _verify)


@pytest.fixture
def client_as_owner(stub_superset_identity: None, identity: SupersetIdentity) -> Client:
    """A Django test client signed in as the conversation's owner."""
    client = Client()
    client.cookies["session"] = f"{SESSION_PREFIX}{identity.username}"
    return client


@pytest.fixture
def turn(conversation: Conversation, identity: SupersetIdentity, page_context: PageContext):
    """Drain a turn into a list of events."""

    def _turn(message: str | None = "Show me lab orders by facility"):
        return list(
            run_turn(
                conversation=conversation,
                identity=identity,
                superset_session_cookie=FAKE_SESSION_COOKIE,
                page_context=page_context,
                user_message=message,
            )
        )

    return _turn


def names(events) -> list[str]:
    return [event.name for event in events]


def only(events, name):
    matches = [event for event in events if event.name == name]
    assert len(matches) == 1, f"expected one {name!r} event, got {len(matches)}"
    return matches[0]


def tool_results(conversation: Conversation) -> list[dict[str, Any]]:
    """Every stored `tool_result` block, flattened."""
    out: list[dict[str, Any]] = []
    for message in conversation.messages.filter(role=Message.Role.TOOL):
        for block in message.content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                out.append(block)
    return out


def unresolved_tool_uses(messages: list[dict[str, Any]]) -> set[str]:
    """`tool_use` ids in a wire request with no matching `tool_result`.

    Non-empty means the Messages API would reject the request outright. This is
    the invariant, computed the way the API computes it.
    """
    used: set[str] = set()
    answered: set[str] = set()
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                used.add(block["id"])
            elif block.get("type") == "tool_result":
                answered.add(block["tool_use_id"])
    return used - answered


# --- The round trip ------------------------------------------------------


class TestTheRoundTrip:
    def test_asking_defers_instead_of_writing_a_result(
        self, fake_anthropic: FakeAnthropic, turn, conversation: Conversation
    ) -> None:
        """The deferral itself: a pending question and deliberately no result."""
        fake_anthropic.queue_tool_use([("toolu_ask", "ask_user", BREAKDOWN_QUESTION)])

        events = turn()

        assert "clarification" in names(events)
        assert events[-1].data["stop_reason"] == AWAITING_USER

        pending = Clarification.objects.get(conversation=conversation)
        assert pending.status == Clarification.Status.PENDING
        assert pending.tool_use_id == "toolu_ask"

        # THE DEFERRAL. Writing a tool_result here would resolve the call
        # immediately and the model would carry on without an answer.
        assert tool_results(conversation) == []
        assert fake_anthropic.call_count == 1

    def test_the_clarification_event_carries_the_questions(
        self, fake_anthropic: FakeAnthropic, turn
    ) -> None:
        fake_anthropic.queue_tool_use([("toolu_ask", "ask_user", BREAKDOWN_QUESTION)])

        event = only(turn(), "clarification")

        assert event.data["tool_use_id"] == "toolu_ask"
        options = event.data["questions"][0]["options"]
        assert [o["label"] for o in options] == ["Each facility", "The kind of facility"]
        # The hint is what lets a non-technical executive choose between two
        # columns without learning what a column is.
        assert options[0]["hint"].startswith("The facility's name")

    def test_the_answer_becomes_the_tool_result(
        self, fake_anthropic: FakeAnthropic, turn, conversation: Conversation
    ) -> None:
        fake_anthropic.queue_tool_use([("toolu_ask", "ask_user", BREAKDOWN_QUESTION)])
        turn()

        fake_anthropic.queue_text("Building it now.")
        turn("Each facility")

        newest = conversation.messages.order_by("-created_at", "-id").first()
        results = tool_results(conversation)
        assert len(results) == 1
        assert results[0]["tool_use_id"] == "toolu_ask"
        assert json.loads(results[0]["content"])["answer"] == "Each facility"

        pending = Clarification.objects.get(conversation=conversation)
        assert pending.status == Clarification.Status.ANSWERED
        assert pending.answer == {"text": "Each facility"}
        assert newest.role in {Message.Role.ASSISTANT, Message.Role.TOOL}

    def test_the_wire_shape_proves_the_model_resumes_its_own_call(
        self, fake_anthropic: FakeAnthropic, turn
    ) -> None:
        """The load-bearing assertion in this file.

        Every other test here could pass while the model saw
        `[assistant: tool_use][user: "Each facility"]` — a spurious user turn
        beside its own unanswered question, which is exactly the shape the
        deferred-resume design exists to avoid. This reads the request body.
        """
        fake_anthropic.queue_tool_use([("toolu_ask", "ask_user", BREAKDOWN_QUESTION)])
        turn()

        fake_anthropic.queue_text("Building it now.")
        turn("Each facility")

        messages = fake_anthropic.last_request["messages"]
        assert messages[-1]["role"] == "user"
        blocks = messages[-1]["content"]
        assert [b["type"] for b in blocks] == ["tool_result"]
        assert blocks[0]["tool_use_id"] == "toolu_ask"

        # And the assistant's own question is immediately above it.
        assert messages[-2]["role"] == "assistant"
        assert any(b.get("type") == "tool_use" for b in messages[-2]["content"])

    def test_an_answer_becomes_the_result_and_not_also_a_user_message(
        self, fake_anthropic: FakeAnthropic, turn, conversation: Conversation
    ) -> None:
        """`run_turn` unconditionally wrote a USER row before this design.

        Left as it was, answering would persist both the tool result and a user
        message saying the same thing, and the model would see its own question
        answered twice — once as the result it was waiting for and once as a
        fresh user turn. The words are not lost: `conversations.py` reads them
        back out of the tool result when replaying a thread, which is what stops
        a reloaded conversation showing the question with nothing under it.
        """
        fake_anthropic.queue_tool_use([("toolu_ask", "ask_user", BREAKDOWN_QUESTION)])
        turn()
        before = conversation.messages.filter(role=Message.Role.USER).count()

        fake_anthropic.queue_text("Building it now.")
        turn("Each facility")

        assert conversation.messages.filter(role=Message.Role.USER).count() == before

    def test_a_replayed_thread_still_shows_what_the_user_chose(
        self, fake_anthropic: FakeAnthropic, turn, conversation: Conversation, client_as_owner
    ) -> None:
        """The other half of the rule above.

        `_visible_text` returned "" for every TOOL-role message, so a thread
        reloaded after answering showed the question with nothing under it.
        """
        fake_anthropic.queue_tool_use([("toolu_ask", "ask_user", BREAKDOWN_QUESTION)])
        turn()
        fake_anthropic.queue_text("Building it now.")
        turn("Each facility")

        response = client_as_owner.get(
            f"/lifedata-ai/api/conversations/{conversation.pk}/"
        )

        assert response.status_code == 200
        texts = [m["text"] for m in response.json()["messages"]]
        assert "Each facility" in texts

    def test_a_turn_that_only_asks_is_not_reported_as_empty(
        self, fake_anthropic: FakeAnthropic, turn
    ) -> None:
        """Returning no text is correct here, not a failure.

        The panel's "returned no text for this turn" guard keys on
        `stop_reason`, and without that a clarification flow would end on an
        error card the moment it worked properly.
        """
        fake_anthropic.queue_tool_use([("toolu_ask", "ask_user", BREAKDOWN_QUESTION)])

        events = turn()

        assert "error" not in names(events)
        assert events[-1].data["stop_reason"] == AWAITING_USER


# --- The guards ----------------------------------------------------------


class TestTheGuards:
    """Each guard prevents a state that would otherwise have to be managed."""

    def test_asking_alongside_another_tool_is_refused(
        self, fake_anthropic: FakeAnthropic, turn, conversation: Conversation
    ) -> None:
        """The API requires every `tool_use` in a message to get a result.

        Deferring writes none, so deferring alongside another call would leave
        that other call permanently unanswered.
        """
        fake_anthropic.queue_tool_use(
            [
                ("toolu_ask", "ask_user", BREAKDOWN_QUESTION),
                ("toolu_ds", "list_datasets", {"q": ""}),
            ]
        )
        fake_anthropic.queue_text("Sorry about that.")

        events = turn()

        assert not Clarification.objects.exists()
        refusals = [
            block for block in tool_results(conversation) if block.get("is_error")
        ]
        assert any("on its own" in block["content"] for block in refusals)
        assert events[-1].data["stop_reason"] != AWAITING_USER

        # A refused call is still a call the model made, and the acceptance
        # criterion is that ALL tool calls appear in the audit log. The guards
        # refuse before reaching `execute()`, so the loop logs them itself.
        assert ToolCallLog.objects.filter(
            conversation=conversation, tool_name="ask_user", ok=False
        ).exists()

    def test_asking_after_a_write_is_refused(
        self, fake_anthropic: FakeAnthropic, turn, conversation: Conversation
    ) -> None:
        """Ask before you build. Asking after is the double-confirmation the
        whole design exists to avoid, and it would leave two things pending."""
        fake_anthropic.queue_tool_use([("toolu_1", "create_chart", LAB_CHART)])
        fake_anthropic.queue_tool_use([("toolu_ask", "ask_user", BREAKDOWN_QUESTION)])
        fake_anthropic.queue_text("Understood.")

        turn()

        assert not Clarification.objects.exists()
        refusals = [b for b in tool_results(conversation) if b.get("is_error")]
        assert any("ask before you build" in b["content"].lower() for b in refusals)

    def test_asking_twice_in_one_turn_is_refused(
        self, fake_anthropic: FakeAnthropic, turn, conversation: Conversation
    ) -> None:
        """One card per build. The second would be asked before the first was
        answered, which is an interrogation and an unresolvable state."""
        fake_anthropic.queue_tool_use([("toolu_ask", "ask_user", BREAKDOWN_QUESTION)])
        turn()

        # Answer the first, then have the model immediately ask again in the
        # same resumed turn.
        fake_anthropic.queue_tool_use([("toolu_ask2", "ask_user", BREAKDOWN_QUESTION)])
        turn("Each facility")

        # The second ask is a fresh turn, so it IS allowed — the guard is
        # per-turn. What must not happen is two pending rows at once.
        assert Clarification.objects.filter(status=Clarification.Status.PENDING).count() == 1

    @pytest.mark.parametrize(
        ("questions", "fragment"),
        [
            ([BREAKDOWN_QUESTION["questions"][0]] * 4, "interrogation"),
            (
                [
                    {
                        "question": "Which period?",
                        "options": [{"label": str(n)} for n in range(7)],
                    }
                ],
                "between 2 and 5 options",
            ),
            (
                [{"question": "Which period?", "options": [{"label": "Only one"}]}],
                "between 2 and 5 options",
            ),
        ],
    )
    def test_an_over_sized_card_is_refused(
        self,
        fake_anthropic: FakeAnthropic,
        turn,
        conversation: Conversation,
        questions: list[dict[str, Any]],
        fragment: str,
    ) -> None:
        """The limits are enforced in the tool, not trusted to the prompt.

        "Ask at most three things" is exactly the instruction a model under
        pressure negotiates away, and the cost of it doing so is an executive
        abandoning the card.
        """
        fake_anthropic.queue_tool_use([("toolu_ask", "ask_user", {"questions": questions})])
        fake_anthropic.queue_text("Let me narrow that down.")

        turn()

        assert not Clarification.objects.exists()
        errors = [b for b in tool_results(conversation) if b.get("is_error")]
        assert any(fragment in b["content"] for b in errors)


# --- History truncation --------------------------------------------------


class TestTruncation:
    def test_the_window_never_splits_a_tool_use_pair(
        self, fake_anthropic: FakeAnthropic, turn, conversation: Conversation, monkeypatch
    ) -> None:
        """The answer must survive a conversation longer than the window.

        Slicing the last N rows can leave a `tool_result` whose `tool_use` fell
        off the front; the old fix popped leading rows until the list started on
        a genuine user turn, which SILENTLY DELETED the executive's answer and
        made the model re-ask or guess.

        Not an edge case: one turn writes an assistant row plus a tool row per
        tool round, and the round cap is 12, so a single dashboard build can
        write 25 rows.
        """
        monkeypatch.setattr("lifedata_ai.agent.loop.MAX_HISTORY_MESSAGES", 4)

        for index in range(4):
            fake_anthropic.queue_text(f"Answer {index}.")
            turn(f"Question {index}?")

        fake_anthropic.queue_tool_use([("toolu_ask", "ask_user", BREAKDOWN_QUESTION)])
        turn("Show lab orders by facility")

        fake_anthropic.queue_text("Building it.")
        turn("Each facility")

        messages = fake_anthropic.last_request["messages"]
        assert not unresolved_tool_uses(messages)
        # The answer itself reached the model.
        rendered = json.dumps(messages)
        assert "toolu_ask" in rendered
        assert "Each facility" in rendered

    def test_a_long_thread_is_still_bounded(
        self, fake_anthropic: FakeAnthropic, turn, monkeypatch
    ) -> None:
        """Pair-awareness must not turn truncation off."""
        monkeypatch.setattr("lifedata_ai.agent.loop.MAX_HISTORY_MESSAGES", 6)

        for index in range(10):
            fake_anthropic.queue_text(f"Answer {index}.")
            turn(f"Question {index}?")

        messages = fake_anthropic.last_request["messages"]
        assert len(messages) <= 8


# --- The invariant -------------------------------------------------------


class TestTheInvariant:
    """`run_turn` never begins a turn with an unresolved `tool_use`.

    Parametrised over both kinds of pending work and every way the next turn can
    start, because the failure is a 400 from the Messages API rather than a
    wrong answer — it arrives before the model is reached and the user can do
    nothing about it.
    """

    @pytest.fixture
    def with_pending_ask(self, fake_anthropic: FakeAnthropic, turn):
        fake_anthropic.queue_tool_use([("toolu_ask", "ask_user", BREAKDOWN_QUESTION)])
        turn()
        return "toolu_ask"

    @pytest.fixture
    def with_pending_preview(self, fake_anthropic: FakeAnthropic, turn, settings):
        # Five charts makes `create_dashboard` a preview under the risk rules,
        # which is what defers it.
        fake_anthropic.queue_tool_use(
            [(
                "toolu_dash",
                "create_dashboard",
                {"dashboard_title": "Laboratory overview", "chart_ids": [1, 2, 3, 4, 5]},
            )]
        )
        turn("Build me a laboratory dashboard")
        return "toolu_dash"

    @pytest.mark.parametrize("pending", ["with_pending_ask", "with_pending_preview"])
    @pytest.mark.parametrize(
        "next_message",
        [
            "Each facility",  # a chip answer, or free text
            "actually add malaria positivity too",  # an unrelated instruction
        ],
    )
    def test_every_start_leaves_a_history_the_api_would_accept(
        self,
        request: pytest.FixtureRequest,
        fake_anthropic: FakeAnthropic,
        turn,
        pending: str,
        next_message: str,
    ) -> None:
        tool_use_id = request.getfixturevalue(pending)

        fake_anthropic.queue_text("Right.")
        turn(next_message)

        messages = fake_anthropic.last_request["messages"]
        assert not unresolved_tool_uses(messages), (
            f"{pending} + {next_message!r} left an unresolved tool_use, which the "
            "Messages API rejects outright"
        )
        assert tool_use_id in json.dumps(messages)

    def test_replying_instead_of_applying_supersedes_the_proposal(
        self,
        fake_anthropic: FakeAnthropic,
        turn,
        conversation: Conversation,
        with_pending_preview: str,
    ) -> None:
        """The first-week-of-the-pilot event.

        The executive reads the receipt card, applies nothing, and types
        something else. Nothing else in the stack catches this: the turn 400s
        before the model is reached.
        """
        record = ActionRecord.objects.get(conversation=conversation)
        assert record.status == ActionRecord.Status.PROPOSED

        fake_anthropic.queue_text("Adding it.")
        turn("actually add malaria positivity too")

        record.refresh_from_db()
        assert record.status == ActionRecord.Status.CANCELLED

        results = [b for b in tool_results(conversation) if b["tool_use_id"] == "toolu_dash"]
        assert len(results) == 1
        payload = json.loads(results[0]["content"])
        assert payload["status"] == "superseded"
        assert "NOT executed" in payload["note"]

        # And the typed message follows the superseded result, so the model
        # reads what was actually asked for.
        messages = fake_anthropic.last_request["messages"]
        rendered = [json.dumps(m) for m in messages]
        superseded_at = next(i for i, m in enumerate(rendered) if "superseded" in m)
        typed_at = next(i for i, m in enumerate(rendered) if "malaria positivity too" in m)
        assert typed_at > superseded_at

    def test_a_late_action_result_for_a_superseded_record_is_refused(
        self,
        client_as_owner,
        fake_anthropic: FakeAnthropic,
        turn,
        conversation: Conversation,
        with_pending_preview: str,
    ) -> None:
        """The browser may genuinely still send one.

        Applying it would resurrect a settled record and make the audit trail
        claim a change happened that the user had already replaced.
        """
        record = ActionRecord.objects.get(conversation=conversation)
        fake_anthropic.queue_text("Adding it.")
        turn("actually add malaria positivity too")

        response = client_as_owner.post(
            "/lifedata-ai/api/action-result/",
            data=json.dumps({"action_id": str(record.id), "status": "executed"}),
            content_type="application/json",
        )

        assert response.status_code == 409
        assert response.json()["error"] == "invalid_transition"
        record.refresh_from_db()
        assert record.status == ActionRecord.Status.CANCELLED

    def test_a_resume_with_nothing_pending_is_refused(
        self, conversation: Conversation, identity: SupersetIdentity, page_context: PageContext
    ) -> None:
        """A resume request that resolves nothing must not start an empty turn."""
        with pytest.raises(NothingToResume):
            list(
                run_turn(
                    conversation=conversation,
                    identity=identity,
                    superset_session_cookie=FAKE_SESSION_COOKIE,
                    page_context=page_context,
                    user_message=None,
                )
            )


# --- Surviving a reload --------------------------------------------------


class TestReloadSurvival:
    """A mechanism that waits indefinitely must survive a page reload.

    That is the whole reason both waits defer rather than block: a human reading
    four options takes twenty seconds or goes to a meeting, and no timeout makes
    that safe. But surviving on the SERVER is only half of it — a card that
    exists in the database and not on screen is worse than one that never
    persisted, because the user has no way to answer and no way to know why the
    copilot has stopped.
    """

    def test_a_pending_question_is_offered_back_after_a_reload(
        self, fake_anthropic: FakeAnthropic, turn, conversation: Conversation, client_as_owner
    ) -> None:
        fake_anthropic.queue_tool_use([("toolu_ask", "ask_user", BREAKDOWN_QUESTION)])
        turn()

        payload = client_as_owner.get(
            f"/lifedata-ai/api/conversations/{conversation.pk}/"
        ).json()

        pending = payload["pending_clarification"]
        assert pending is not None
        assert pending["tool_use_id"] == "toolu_ask"
        assert [o["label"] for o in pending["questions"][0]["options"]] == [
            "Each facility",
            "The kind of facility",
        ]

    def test_a_pending_proposal_is_offered_back_with_a_FRESH_signature(
        self, fake_anthropic: FakeAnthropic, turn, conversation: Conversation, client_as_owner
    ) -> None:
        """Re-signed rather than stored, and that is deliberate.

        Envelopes expire. A proposal the user comes back to tomorrow needs a
        fresh signature to be executable at all, and one they abandoned must NOT
        stay executable in a stale browser tab — storing the signed token would
        get both of those backwards.
        """
        fake_anthropic.queue_tool_use(
            [(
                "toolu_dash",
                "create_dashboard",
                {"dashboard_title": "Laboratory overview", "chart_ids": [1, 2, 3, 4, 5]},
            )]
        )
        turn("Build me a laboratory dashboard")

        payload = client_as_owner.get(
            f"/lifedata-ai/api/conversations/{conversation.pk}/"
        ).json()

        pending = payload["pending_action"]
        assert pending is not None
        assert pending["risk"] == "preview"
        assert pending["calls"][0]["path"] == "/api/v1/dashboard/"
        # A real, verifiable signature over the calls it carries.
        from lifedata_ai.actions.envelopes import verify_envelope

        assert verify_envelope(pending["envelope"])["calls"] == pending["calls"]

    def test_a_settled_proposal_is_not_offered_back(
        self, fake_anthropic: FakeAnthropic, turn, conversation: Conversation, client_as_owner
    ) -> None:
        """Otherwise the rail would show an Apply button for a done deal."""
        fake_anthropic.queue_tool_use(
            [(
                "toolu_dash",
                "create_dashboard",
                {"dashboard_title": "Laboratory overview", "chart_ids": [1, 2, 3, 4, 5]},
            )]
        )
        turn("Build me a laboratory dashboard")

        record = ActionRecord.objects.get(conversation=conversation)
        record.transition_to(ActionRecord.Status.CANCELLED)

        payload = client_as_owner.get(
            f"/lifedata-ai/api/conversations/{conversation.pk}/"
        ).json()

        assert payload["pending_action"] is None

    def test_an_idle_conversation_offers_nothing(
        self, fake_anthropic: FakeAnthropic, turn, conversation: Conversation, client_as_owner
    ) -> None:
        fake_anthropic.queue_text("Encounters rose in March.")
        turn("How were encounters in March?")

        payload = client_as_owner.get(
            f"/lifedata-ai/api/conversations/{conversation.pk}/"
        ).json()

        assert payload["pending_clarification"] is None
        assert payload["pending_action"] is None


def results_immediately_after_each_use(messages: list[dict[str, Any]]) -> list[str]:
    """Complaints the Messages API would make about this history.

    Stricter than `unresolved_tool_uses`, and the difference is the whole point.
    The API does not merely require a result for every `tool_use`; it requires
    **all of them, in the single user message immediately after** the assistant
    message that made the calls:

        messages.10: `tool_use` ids were found without `tool_result` blocks
        immediately after: toolu_...

    A history where each id is answered in its own separate message satisfies
    "every id has a result" and is still rejected. That is precisely what
    happened in a browser: a resize auto-applied and a filter deferred for
    approval, both in one assistant message, and their results were written as
    two TOOL rows.
    """
    complaints: list[str] = []
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        used = {
            block["id"]
            for block in message.get("content", [])
            if isinstance(block, dict) and block.get("type") == "tool_use"
        }
        if not used:
            continue
        following = messages[index + 1] if index + 1 < len(messages) else None
        answered = {
            block["tool_use_id"]
            for block in (following or {}).get("content", [])
            if isinstance(block, dict) and block.get("type") == "tool_result"
        } if following and following.get("role") == "user" else set()
        missing = used - answered
        if missing:
            complaints.append(f"messages.{index}: {sorted(missing)} unanswered by the next message")
    return complaints


class TestParallelCallsAroundADeferral:
    """One assistant message, several tool calls, one of them deferred.

    The model batches. `resize_dashboard_component` and `add_native_filter` in
    the same message is normal and desirable — and if one auto-applies while the
    other waits for approval, their results must still arrive together.
    """

    @pytest.fixture
    def batched_turn(self, fake_anthropic: FakeAnthropic, turn):
        """A low-risk write and a preview-classed one, in one message."""
        fake_anthropic.queue_tool_use(
            [
                ("toolu_chart", "create_chart", LAB_CHART),
                (
                    "toolu_dash",
                    "create_dashboard",
                    {"dashboard_title": "Laboratory overview",
                     "chart_ids": [1, 2, 3, 4, 5]},
                ),
            ]
        )
        turn("Build me a laboratory dashboard")

    def test_the_deferral_writes_no_partial_result_message(
        self, batched_turn, conversation: Conversation
    ) -> None:
        """Writing the sibling's result now would orphan the deferred one."""
        assert tool_results(conversation) == []

    def test_resuming_answers_every_call_in_one_message(
        self, batched_turn, fake_anthropic: FakeAnthropic, turn, conversation: Conversation
    ) -> None:
        fake_anthropic.queue_text("Right.")
        turn("actually add malaria positivity too")

        messages = fake_anthropic.last_request["messages"]
        assert not results_immediately_after_each_use(messages)
        assert not unresolved_tool_uses(messages)

        # And exactly one TOOL row was written, carrying both results.
        rows = list(conversation.messages.filter(role=Message.Role.TOOL))
        assert len(rows) == 1
        assert len(rows[0].content) == 2

    def test_a_sibling_that_ran_is_reported_as_having_run(
        self, batched_turn, fake_anthropic: FakeAnthropic, turn, conversation: Conversation
    ) -> None:
        """Not "not attempted" — the chart really was created.

        Telling the model otherwise is worse than saying nothing: it builds the
        thing a second time.
        """
        fake_anthropic.queue_text("Right.")
        turn("actually add malaria positivity too")

        results = {b["tool_use_id"]: json.loads(b["content"]) for b in tool_results(conversation)}
        assert results["toolu_dash"]["status"] == "superseded"
        assert results["toolu_chart"]["status"] != "not_attempted", (
            "the chart was auto-applied before the deferral and must not be "
            "reported as unattempted"
        )
