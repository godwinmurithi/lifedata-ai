"""The Anthropic tool-use loop for one chat turn (spec §5).

The loop is a **generator**, not a function that returns an answer. Everything
it learns — a token of text, a tool starting, a tool finishing — is yielded
immediately as an `AgentEvent` so the view can relay it over SSE. A turn that
builds a dashboard can take two minutes; buffering it would leave the panel
showing a spinner with nothing to say.

Four things are load-bearing here and are easy to get wrong:

* **Persistence stores raw Anthropic content blocks** (spec §4). Not the
  rendered text — the blocks, verbatim, including thinking blocks, because
  those must be passed back unchanged when the conversation continues on the
  same model. `_load_history` rebuilds the message list from those rows, so a
  refreshed panel resumes exactly where it left off.
* **Every tool call goes through `tools.registry.execute`.** That is the only
  path that writes `ToolCallLog`, which is the Phase 1 acceptance criterion.
  The loop must never call `tool.run()` directly.
* **`stop_reason` is checked before `content` is read.** A refusal is a normal
  HTTP 200 with possibly-empty content; code that reaches for `content[0]`
  first crashes on it.
* **A turn never begins with an unresolved `tool_use` in history.** See below.

# Waiting for a human

Two tools end a turn rather than blocking it: `ask_user`, and any write whose
risk classes it as a preview. Both are waiting for a *person*, and a person
takes twenty seconds or goes to a meeting. Holding a WSGI thread and a database
connection for that is bad; being unable to survive a page reload is fatal,
because reload-survival is the whole point of a mechanism that waits for someone.

So the loop **ends the turn but resumes the tool call**:

1. The model calls the tool. It runs through the normal audited path.
2. The loop persists the assistant message carrying the `tool_use` block, emits
   the matching SSE event, and breaks with `stop_reason="awaiting_user"` —
   deliberately writing **no** `tool_result`.
3. Whatever the user does next — clicking a chip, pressing Apply, or typing
   something else entirely — becomes that `tool_result`, written as a `TOOL`-role
   message. `_WIRE_ROLE` already maps `TOOL -> "user"`.

The model then sees `[assistant: tool_use][user: tool_result]` and continues as
though the tool had simply taken ninety seconds to return. "Only start when you
have full context" becomes structural rather than instructed: the model
*cannot* proceed without the result.

**The invariant, stated plainly, because breaking it is a 400 from the Messages
API rather than a wrong answer:** `run_turn` never begins a turn with an
unresolved `tool_use` in history. Every pending call has exactly three exits —
answered/applied, cancelled, or superseded — and `_resolve_pending` takes one of
them before any model call happens.

**And it is stricter than it first sounds.** The API does not merely require a
result for every `tool_use`; it requires *all of them, together, in the single
user message immediately after* the assistant message that made the calls:

    messages.10: `tool_use` ids were found without `tool_result` blocks
    immediately after: toolu_...

That matters because the model batches, and it should — `resize_dashboard_component`
and `add_native_filter` in one message is exactly right. If one auto-applies and
the other defers for approval, their results are learned at different times and
the temptation is to write each as it arrives. Doing so satisfies "every id has a
result" and is still rejected. So the deferral persists nothing, stashes what it
already knows on the record, and `_resolve_pending` emits one message covering
every outstanding id at once.

Hard stops come from settings: `LIFEDATA_AI_MAX_TOOL_ROUNDS` bounds how many
times the model may call tools in one turn, `LIFEDATA_AI_MAX_WRITES_PER_TURN`
bounds proposed changes, and `LIFEDATA_AI_TURN_TIMEOUT_SECONDS` bounds wall-clock
time — of *model* work; time spent awaiting the browser is added back to the
deadline (see `_await_action_outcome`).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from lifedata_ai.agent.context import PageContext, clean_text
from lifedata_ai.agent.events import PANEL_EVENTS
from lifedata_ai.agent.deepseek_provider import DeepSeekClient
from lifedata_ai.agent.openai_provider import (
    OpenAIClient,
    OpenAIConnectionError,
    OpenAIStatusError,
)
from lifedata_ai.agent.prompts import build_system_blocks
from lifedata_ai.middleware import SupersetIdentity
from lifedata_ai.models import ActionRecord, Clarification, Conversation, Message
from lifedata_ai.tools import ToolContext, ToolMode, audit_refusal, execute, get_registry

logger = logging.getLogger(__name__)


# --- Tuning --------------------------------------------------------------

#: Hard cap on thinking + response text together. Thinking is on by default on
#: claude-opus-5, so a tight budget here truncates the answer mid-sentence
#: rather than merely shortening it. Streaming removes the HTTP-timeout reason
#: to keep it small, so it is set generously.
MAX_TOKENS = 32000

#: How many stored messages are replayed into a new turn.
#:
#: 40 was far too small and it was not obvious why. One turn writes an
#: ASSISTANT row plus a TOOL row per tool round, and `LIFEDATA_AI_MAX_TOOL_ROUNDS`
#: is 12 — so a single dashboard build can write 25 rows. The *second* build in a
#: conversation already overflowed the window, silently dropping the first one
#: and, with it, the answers the user had given.
MAX_HISTORY_MESSAGES = 120

#: Conversation titles are derived from the opening message; this is only a
#: display label, so it is truncated rather than summarised.
TITLE_LENGTH = 120

#: How often the awaited-browser poll emits a heartbeat. Nine browser round
#: trips behind a static panel reads as a hang, so the poll loop — which already
#: sleeps in a generator that yielded nothing — now says what it is waiting for.
_PROGRESS_INTERVAL_SECONDS = 3.0

#: Human phrasing for the activity line the panel shows while a tool runs. The
#: panel no longer keeps its own copy — it renders `data.summary` — so this map
#: is the single source of that wording.
_TOOL_SUMMARIES: dict[str, str] = {
    "list_databases": "listing databases",
    "list_datasets": "browsing datasets",
    "get_dataset": "inspecting dataset",
    "list_charts": "browsing charts",
    "get_chart": "inspecting chart",
    "list_dashboards": "browsing dashboards",
    "get_dashboard": "inspecting dashboard",
    "get_chart_data": "reading aggregate results",
    "get_page_context": "reading page context",
    "review_dashboard_design": "reviewing the dashboard design",
    "ask_user": "asking you a question",
    "list_forms": "listing dynamic forms",
    "get_form_schema": "reading form schema",
    "create_chart": "building chart",
    "update_chart": "updating chart",
    "rename_chart": "renaming chart",
    "create_dashboard": "building dashboard",
    "rename_dashboard": "renaming dashboard",
    "add_chart_to_dashboard": "adding a chart to dashboard",
    "remove_chart_from_dashboard": "removing a chart from dashboard",
    "move_dashboard_component": "rearranging dashboard",
    "resize_dashboard_component": "resizing on dashboard",
    "add_native_filter": "adding a filter to dashboard",
}

#: Arguments worth showing in the activity line. Ids and short keys only — a
#: free-text argument could be long or hostile, and this string is rendered.
_SUMMARY_ARGUMENT_KEYS = (
    "chart_name", "dashboard_title", "name", "dataset_id", "chart_id",
    "dashboard_id", "form_key", "form_id",
)


# --- Events --------------------------------------------------------------


@dataclass(frozen=True)
class AgentEvent:
    """One SSE-bound event. `name` is the SSE event name, `data` its JSON body."""

    name: str
    data: dict[str, Any] = field(default_factory=dict)


#: Transient-failure retries per turn. Small on purpose: a sustained provider
#: outage should surface as an error the user can act on, not as a long hang.
_MAX_PROVIDER_RETRIES = 4

#: Provider error types that are worth retrying, taken from the error body
#: rather than the HTTP status (see the note at the call site).
_TRANSIENT_ERROR_TYPES = frozenset({"overloaded_error", "rate_limit_error", "api_error"})

#: The stop reason that means "this turn ended on purpose, waiting for a person".
AWAITING_USER = "awaiting_user"


def _is_transient(exc: Exception) -> bool:
    """Whether a provider error is worth retrying, judged by its error type."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("type") in _TRANSIENT_ERROR_TYPES:
            return True
    # The body is not always parsed onto the exception; the rendered message
    # still names the type.
    text = str(exc).lower()
    return any(name in text for name in ("overloaded", "rate_limit", "429", "529"))


def _event(event_name: str, /, **data: Any) -> AgentEvent:
    """Positional-only first argument: several events carry a `name` field of
    their own (the tool's name), which would otherwise collide.

    Membership in `PANEL_EVENTS` is asserted under DEBUG. `panel.js` ignores an
    event name it does not know — deliberately, so a cached bundle survives an
    orchestrator upgrade — which means a typo here would produce an event the
    panel silently drops and nothing anywhere would report it.
    """
    if settings.DEBUG and event_name not in PANEL_EVENTS:
        raise AssertionError(
            f"{event_name!r} is not in PANEL_EVENTS. Declare it in agent/events.py "
            "and handle it in panel.js — an undeclared event is silently ignored "
            "by the panel."
        )
    return AgentEvent(name=event_name, data=data)


class ConversationNotFound(Exception):
    """The requested conversation does not exist *for this user*.

    Raised for someone else's conversation id as well as an unknown one — the
    caller turns both into a 404, so the endpoint cannot be used to probe
    whether a given id exists.
    """


class NothingToResume(Exception):
    """A resume request arrived with no pending question or proposal."""


# --- Conversation resolution ---------------------------------------------


def resolve_conversation(
    *,
    identity: SupersetIdentity,
    conversation_id: str | None,
    page_context: PageContext,
) -> Conversation:
    """Fetch the caller's conversation, or start a new one.

    Scoped to `identity.username` on the way in, not filtered afterwards: a
    conversation belonging to another user is indistinguishable from one that
    does not exist.
    """
    if conversation_id:
        conversation = Conversation.objects.filter(
            pk=conversation_id, superset_username=identity.username
        ).first()
        if conversation is None:
            raise ConversationNotFound(conversation_id)
        return conversation

    return Conversation.objects.create(
        superset_username=identity.username,
        page_scope=page_context.to_dict(),
    )


# --- History -------------------------------------------------------------

#: Stored role -> Anthropic wire role. Tool results are user-role content
#: blocks on the wire; the separate `tool` role exists only so the audit trail
#: distinguishes "the user typed this" from "a tool returned this".
_WIRE_ROLE = {
    Message.Role.USER: "user",
    Message.Role.ASSISTANT: "assistant",
    Message.Role.TOOL: "user",
}


def _blocks(content: Any) -> list[dict[str, Any]]:
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _tool_use_ids(content: Any) -> set[str]:
    return {
        block["id"]
        for block in _blocks(content)
        if block.get("type") == "tool_use" and block.get("id")
    }


def _tool_result_ids(content: Any) -> set[str]:
    return {
        block["tool_use_id"]
        for block in _blocks(content)
        if block.get("type") == "tool_result" and block.get("tool_use_id")
    }


def _is_tool_result(content: Any) -> bool:
    return bool(_tool_result_ids(content))


def _load_history(conversation: Conversation) -> list[dict[str, Any]]:
    """Rebuild the Anthropic `messages` list from stored blocks.

    The truncation is the subtle part, and getting it wrong loses the user's own
    words. Slicing the last N rows can leave a `tool_result` whose matching
    `tool_use` fell off the front, which the API rejects outright — so the naive
    fix was to pop leading rows until the list started on a genuine user turn.

    That fix silently deleted answers. Picture a pending `ask_user` straddling
    the window boundary: the assistant `tool_use` falls off the front, the
    orphaned `tool_result` — the executive's answer — is popped as "not a genuine
    user turn", and the model re-asks or guesses. Nothing reports it.

    So the window is made **pair-aware** instead: it is extended backwards to
    include the whole of any `tool_use`/`tool_result` pair it would otherwise
    split. Only if that still leaves an orphan (a pair older than the entire
    conversation window, which cannot happen) does the leading-row pop apply.
    """
    rows = list(conversation.messages.all())
    if len(rows) <= MAX_HISTORY_MESSAGES:
        start = 0
    else:
        start = _pair_aware_start(rows, len(rows) - MAX_HISTORY_MESSAGES)
    rows = rows[start:]

    # Belt and braces: an assistant turn cannot open a conversation, and a
    # tool_result with no tool_use above it is a 400.
    while rows and (rows[0].role != Message.Role.USER or _is_tool_result(rows[0].content)):
        rows.pop(0)

    messages = [{"role": _WIRE_ROLE[row.role], "content": row.content} for row in rows]
    # Keep recent visual inputs within the provider request-size budget. Stored
    # conversation content is unchanged; omitted older files are explicitly marked.
    remaining = 24 * 1024 * 1024
    for message in reversed(messages):
        content = []
        for block in reversed(message["content"]):
            source = block.get("source", {})
            size = len(source.get("data", "")) if source.get("type") == "base64" else 0
            if size > remaining:
                content.append({"type": "text", "text": "[An earlier attachment was omitted to fit the request size. Ask the user to attach it again if needed.]"})
            else:
                remaining -= size
                content.append(block)
        message["content"] = list(reversed(content))
    return messages


def _pair_aware_start(rows: Sequence[Message], preferred_start: int) -> int:
    """Move `preferred_start` back so the window never splits a tool-use pair.

    Walks forward from the preferred start collecting `tool_result` ids that have
    no `tool_use` above them inside the window, then walks backwards to find the
    assistant message that owns them. Returns the earliest index that keeps every
    pair whole.
    """
    window = rows[preferred_start:]
    satisfied: set[str] = set()
    orphans: set[str] = set()
    for row in window:
        satisfied |= _tool_use_ids(row.content)
        orphans |= _tool_result_ids(row.content) - satisfied
    if not orphans:
        return preferred_start

    start = preferred_start
    for index in range(preferred_start - 1, -1, -1):
        produced = _tool_use_ids(rows[index].content)
        if produced & orphans:
            orphans -= produced
            start = index
        if not orphans:
            break
    return start


def _serialise_blocks(blocks: Sequence[Any]) -> list[dict[str, Any]]:
    """Anthropic content blocks as plain JSON, faithful enough to replay.

    `exclude_none` is deliberate: the SDK's response models carry optional
    fields (a text block's `citations`, for example) that are absent rather
    than null in a request body. Keeping them as nulls has been tolerated, but
    dropping them keeps the replayed message byte-identical to a hand-built
    one, which is also what the prompt cache wants.
    """
    serialised: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, dict):
            serialised.append(block)
        else:
            serialised.append(block.model_dump(mode="json", exclude_none=True))
    return serialised


# --- Pending work: the deferred-resume path ------------------------------


def pending_clarification(conversation: Conversation) -> Clarification | None:
    """The outstanding question on this conversation, if any."""
    return conversation.clarifications.filter(
        status=Clarification.Status.PENDING
    ).order_by("-created_at").first()


def _dangling_tool_use(conversation: Conversation) -> tuple[Message, list[str]] | None:
    """The newest assistant message with `tool_use` blocks still unanswered.

    Returns EVERY unanswered id in that message, in block order, not just one —
    and that is the whole point. The Messages API does not merely require a
    result for each `tool_use`; it requires them **in the single user message
    immediately after**. Resolving one id per message satisfies "every id has a
    result somewhere" and still produces:

        messages.10: `tool_use` ids were found without `tool_result` blocks
        immediately after: toolu_...

    Observed exactly that way. The model asked for `resize_dashboard_component`
    and `add_native_filter` together; the resize auto-applied, the filter
    deferred for approval, and the two results were written as two separate TOOL
    rows. Every id had a result. The turn 400ed anyway.
    """
    rows = list(conversation.messages.all())
    answered: set[str] = set()
    for row in reversed(rows):
        if row.role == Message.Role.TOOL:
            answered |= _tool_result_ids(row.content)
            continue
        if row.role != Message.Role.ASSISTANT:
            continue
        used = [
            block["id"]
            for block in _blocks(row.content)
            if block.get("type") == "tool_use" and block.get("id")
        ]
        outstanding = [tool_use_id for tool_use_id in used if tool_use_id not in answered]
        if outstanding:
            return row, outstanding
        answered |= set(used)
    return None


def _write_tool_results(
    conversation: Conversation, payloads: list[tuple[str, Any]]
) -> Message:
    """Persist ONE `tool_result` message covering several `tool_use` blocks.

    One message, always. See `_dangling_tool_use` for why splitting them is a
    400 rather than a stylistic choice.
    """
    return Message.objects.create(
        conversation=conversation,
        role=Message.Role.TOOL,
        content=[
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": json.dumps(payload, default=str),
            }
            for tool_use_id, payload in payloads
        ],
    )


#: What an unresolved sibling of a deferred call is told. It genuinely did not
#: run: the loop stopped at the deferral, so anything after it was never
#: attempted.
_NOT_ATTEMPTED: dict[str, Any] = {
    "status": "not_attempted",
    "note": (
        "This was not carried out: the turn paused for the user before reaching it. "
        "Ask for it again if it is still wanted."
    ),
}


def _resolve_pending(
    conversation: Conversation, user_message: str | None
) -> dict[str, Any] | None:
    """Close out whatever the previous turn left outstanding.

    Called before any model request, and it is what upholds the invariant in the
    module docstring. Three exits, and the third is the one that actually breaks
    production if it is missing:

    * **Answered.** A pending `ask_user` is marked answered and the user's reply
      — a chip, "Something else…", or free text — becomes the tool result.
    * **Applied or cancelled.** A pending write proposal whose `ActionRecord` has
      since reached a settled status reports that status back.
    * **Superseded.** The executive reads the receipt card, applies nothing, and
      types "actually add malaria positivity too". That message arrives while the
      newest assistant message still ends in an unresolved `tool_use`, so the
      turn would 400 before reaching the model. Here the proposal is resolved as
      superseded, its record cancelled, and the typed message then appended as an
      ordinary user turn — so the model sees the superseded result followed by
      the new instruction and simply carries on.

    Returns a small dict describing what was resolved, for the caller's events,
    or None when there was nothing outstanding.
    """
    dangling = _dangling_tool_use(conversation)
    if dangling is None:
        return None
    _message, outstanding_ids = dangling
    tool_use_id = outstanding_ids[0]

    # Whatever the pending thing is, it resolves ONE of the outstanding ids. The
    # rest are siblings the loop never reached — it stopped at the deferral —
    # and they must be answered in the SAME message or the API rejects the turn
    # regardless of how correctly this one is resolved.
    def finish(
        payload: Any, extra: dict[str, Any], stashed: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        results = [(tool_use_id, payload)]
        for sibling in outstanding_ids:
            if sibling == tool_use_id:
                continue
            # A sibling that ran before the deferral has a REAL result, carried
            # on the record. Reporting it as "not attempted" would be a lie —
            # a chart that was auto-applied really was created — and the model
            # would build it again.
            results.append((sibling, (stashed or {}).get(sibling, _NOT_ATTEMPTED)))
        _write_tool_results(conversation, results)
        if len(results) > 1:
            logger.info(
                "Resolved %s alongside %s sibling call(s) in one message",
                tool_use_id, len(results) - 1,
            )
        return extra

    pending = pending_clarification(conversation)
    if pending is not None and pending.tool_use_id in outstanding_ids:
        tool_use_id = pending.tool_use_id
        answer = (user_message or "").strip()
        if not answer:
            # A resume request with no text cannot answer a question. Treat the
            # question as abandoned rather than feeding the model an empty
            # answer it would have to interpret.
            pending.status = Clarification.Status.ABANDONED
            pending.save(update_fields=["status", "updated_at"])
            return finish(
                {
                    "status": "abandoned",
                    "note": (
                        "The user did not answer. Choose the most defensible reading, "
                        "build it, and say which reading you used."
                    ),
                },
                {"kind": "clarification", "resolution": "abandoned"},
            )

        pending.answer = {"text": answer}
        pending.status = Clarification.Status.ANSWERED
        pending.save(update_fields=["answer", "status", "updated_at"])
        # `consumed` is what stops `run_turn` ALSO writing a USER row. The
        # answer becomes the tool result and nothing else: writing both would
        # show the model its own question answered twice, once as a tool result
        # and once as a fresh user turn — which is the shape this whole design
        # exists to avoid. The words are not lost; `conversations.py` reads them
        # back out of the tool result when replaying a thread.
        return finish(
            {"status": "answered", "answer": answer},
            {
                "kind": "clarification",
                "resolution": "answered",
                "answer": answer,
                "consumed": True,
            },
        )

    # Not a question, so it is a deferred write proposal. The primary id is the
    # one a record was STAMPED against, which is not necessarily the first in
    # block order: the model batches, and the deferred call is often the second.
    #
    # Two passes, and the order matters. Matching by stamp first is exact;
    # `_pending_action_for`'s newest-unsettled fallback is a guess, and letting
    # it answer for an earlier sibling made the loop resolve the wrong call —
    # reporting the deferred proposal as "not attempted" while treating an
    # auto-applied chart as the thing the user had approved.
    record = None
    for candidate in outstanding_ids:
        stamped = _stamped_action_for(conversation, candidate)
        if stamped is not None:
            record, tool_use_id = stamped, candidate
            break
    if record is None:
        record = _pending_action_for(conversation, tool_use_id)

    if record is None:
        # A dangling tool_use with nothing behind it should be impossible. It is
        # still resolved rather than left to 400 the turn: an honest "this did
        # not complete" is recoverable, an API rejection is not.
        logger.warning(
            "Conversation %s has a dangling tool_use %s with no pending action",
            conversation.pk, tool_use_id,
        )
        return finish(
            {"status": "unknown", "note": "This action did not complete. Do not rely on it."},
            {"kind": "unknown", "resolution": "unknown"},
        )

    stashed = _stashed_siblings(record)

    if record.status in _SETTLED_STATUSES:
        return finish(
            _outcome_payload(record),
            {
                "kind": "action",
                "resolution": record.status,
                "action_id": str(record.id),
            },
            stashed,
        )

    # Still PROPOSED or APPROVED, and the user has sent something else: they
    # replied instead of applying.
    record.transition_to(ActionRecord.Status.CANCELLED)
    return finish(
        {
            "status": "superseded",
            "note": (
                "The user replied instead of applying this. It was NOT executed and "
                "nothing changed in Superset. Read what they said next and act on that."
            ),
        },
        {"kind": "action", "resolution": "superseded", "action_id": str(record.id)},
        stashed,
    )


def _stashed_siblings(record: ActionRecord) -> dict[str, Any]:
    """Results for calls that ran before this proposal deferred the turn.

    Stored by `_stamp_tool_use` at deferral time, keyed by `tool_use_id`. They
    could not be written then: every result for one assistant message must
    arrive in one user message immediately after it, and that message is not
    written until the user acts.
    """
    blocks = (record.validation_result or {}).get("sibling_results") or []
    stashed: dict[str, Any] = {}
    for block in blocks:
        if not isinstance(block, dict) or not block.get("tool_use_id"):
            continue
        raw = block.get("content")
        try:
            stashed[block["tool_use_id"]] = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            stashed[block["tool_use_id"]] = raw
    return stashed


#: Statuses from which nothing more will happen, so the outcome can be reported.
_SETTLED_STATUSES = frozenset(
    {
        ActionRecord.Status.EXECUTED,
        ActionRecord.Status.FAILED,
        ActionRecord.Status.CANCELLED,
        ActionRecord.Status.REJECTED,
        ActionRecord.Status.UNDONE,
    }
)


def _stamped_action_for(conversation: Conversation, tool_use_id: str) -> ActionRecord | None:
    """The record explicitly stamped against this `tool_use`, or None.

    Exact, unlike `_pending_action_for`'s fallback. `run_turn` stamps
    `validation_result["tool_use_id"]` at the moment it defers, so this is the
    only reliable way to say which of several batched calls the user is being
    asked about.
    """
    for record in conversation.actions.order_by("-created_at"):
        if (record.validation_result or {}).get("tool_use_id") == tool_use_id:
            return record
    return None


def _pending_action_for(conversation: Conversation, tool_use_id: str) -> ActionRecord | None:
    """The action record a deferred `tool_use` is waiting on.

    Matched through the stamp first. Falling back to the newest unsettled record
    keeps a record written before that field existed resolvable rather than
    wedging the conversation — but it is a GUESS, so callers with several
    candidate ids must try `_stamped_action_for` against all of them before
    reaching for this.
    """
    stamped = _stamped_action_for(conversation, tool_use_id)
    if stamped is not None:
        return stamped
    return conversation.actions.order_by("-created_at").exclude(
        status__in=_SETTLED_STATUSES
    ).first()


def _outcome_payload(record: ActionRecord) -> dict[str, Any]:
    """What the model is told about a settled action."""
    if record.status == ActionRecord.Status.EXECUTED:
        return {
            "status": "executed",
            "superset_object_id": record.superset_object_id or None,
            "note": (
                "The user's browser applied this with their own Superset session. "
                "Use this id when referring to the object you just created."
            ),
        }
    detail = (record.execution_result or {}).get("response_excerpt", "")
    return {
        "status": record.status,
        "detail": str(detail)[:400],
        "note": "Tell the user plainly that this did not happen, and why.",
    }


# --- Awaiting the browser for an auto-applied write ----------------------


def _await_action_outcome(action_id: str | None) -> Iterator[Any]:
    """Poll until the browser reports the outcome, yielding progress meanwhile.

    A generator now, and for two reasons that both matter. It already slept in a
    loop that yielded nothing, so nine browser round trips read as a hang; and
    the caller needs to know how long it waited, because that time must not be
    charged to the model's own timeout.

    Yields `AgentEvent`s and finally a `("done", outcome, waited_seconds)` tuple.

    Only the **auto-applied** path comes here. Anything the user has to approve
    defers instead — that wait is unbounded by design and no timeout can make it
    otherwise. What is left is a browser answering in a couple of seconds, where
    chaining "create chart" into "put it on this dashboard" is a real win and
    30 seconds is generous.
    """
    started = time.monotonic()
    if not action_id:
        yield ("done", {"status": "unknown", "note": "No action id was issued."}, 0.0)
        return

    deadline = started + settings.LIFEDATA_AI_ACTION_WAIT_SECONDS
    last_progress = started
    while time.monotonic() < deadline:
        time.sleep(0.4)
        try:
            record = ActionRecord.objects.get(id=action_id)
        except ActionRecord.DoesNotExist:  # pragma: no cover - the row is created first
            break
        if record.status in _SETTLED_STATUSES:
            yield ("done", _outcome_payload(record), time.monotonic() - started)
            return
        now = time.monotonic()
        if now - last_progress >= _PROGRESS_INTERVAL_SECONDS:
            last_progress = now
            # Doubles as the SSE keep-alive: a proxy that sees nothing for long
            # enough will close the stream regardless of what nginx is told.
            yield _event(
                "progress",
                phase="applying",
                waited_seconds=round(now - started),
                message="applying the change in your browser",
            )

    yield (
        "done",
        {
            "status": "pending",
            "note": (
                "Still awaiting the user's browser. Do not claim it succeeded, and do not "
                "depend on an id from it."
            ),
        },
        time.monotonic() - started,
    )


# --- Tool activity summaries ---------------------------------------------


def _summarise_tool_call(name: str, arguments: dict[str, Any] | None) -> str:
    """A short, safe activity line: "inspecting dataset 2"."""
    summary = _TOOL_SUMMARIES.get(name, name.replace("_", " "))
    for key in _SUMMARY_ARGUMENT_KEYS:
        value = (arguments or {}).get(key)
        if value in (None, ""):
            continue
        return f"{summary} {clean_text(value, 60)}"
    return summary


# --- The loop ------------------------------------------------------------


def run_turn(
    *,
    conversation: Conversation,
    identity: SupersetIdentity,
    superset_session_cookie: str,
    page_context: PageContext,
    user_message: str | None,
    attachments: list[dict[str, Any]] | None = None,
) -> Iterator[AgentEvent]:
    """Run one chat turn, yielding events as they happen.

    `user_message` is None for a *resume*: the panel reporting an Apply outcome
    and asking the loop to carry on. There is genuinely no user message in that
    case, and inventing one would put a spurious user turn beside the model's own
    tool result.

    Never raises for an expected failure — a missing API key, an Anthropic
    error, a refusal, an exhausted round budget all arrive as an `error` event
    followed by `message_stop`, so the panel always sees a clean end. Genuine
    programming errors are left to propagate; the view wraps the generator and
    converts them, because a half-written SSE stream that simply stops looks
    identical to a network failure from the browser's side.
    """
    conversation_id = str(conversation.pk)
    yield _event("message_start", conversation_id=conversation_id)

    # THE INVARIANT. Whatever the previous turn left outstanding is closed out
    # here, before any model call — see `_resolve_pending`. A turn that begins
    # with an unresolved `tool_use` in history is rejected by the Messages API,
    # which surfaces as a 400 the user cannot act on.
    resolved = _resolve_pending(conversation, user_message)
    if resolved is None and user_message is None:
        raise NothingToResume(conversation_id)

    # A clarification answer BECOMES the tool result; it is not also a user
    # message. Everything else the user types is an ordinary turn, appended
    # after whatever was resolved above.
    if user_message is not None and (attachments or not (resolved or {}).get("consumed")):
        _record_user_message(conversation, user_message, page_context, attachments)
    else:
        # No user row this turn — but the page may well have changed while the
        # card sat there, and the conversation still needs a title if it somehow
        # lacks one (a thread that opened with a clarification answer would
        # otherwise stay untitled for ever in the history list).
        if not conversation.title and user_message:
            conversation.title = clean_text(user_message, TITLE_LENGTH)
        conversation.page_scope = page_context.to_dict()
        conversation.save(update_fields=["title", "page_scope", "updated_at"])

    provider = settings.LIFEDATA_AI_PROVIDER
    provider_label = {"openai": "OpenAI", "deepseek": "DeepSeek", "anthropic": "Anthropic"}[provider]
    key_name = {"openai": "OPENAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[provider]
    api_key = getattr(settings, key_name)
    if not api_key:
        # The rest of the stack stays usable without a key: read tools,
        # diagnostics and the panel all work, only the model call cannot.
        yield _event(
            "error",
            code=f"{provider}_key_missing",
            message=(
                f"The copilot has no {provider_label} API key configured, so it cannot answer. "
                f"Set {key_name} in the environment and restart the orchestrator."
            ),
        )
        yield _event("message_stop", stop_reason="error", conversation_id=conversation_id)
        return

    import anthropic  # Imported late so a missing key path costs nothing.

    registry = get_registry()
    # Read AND write tools. A write tool only ever proposes: it returns a signed
    # envelope that the panel executes with the user's own session (spec §1).
    tool_specs = _cacheable_tool_specs(registry.to_anthropic_specs())
    system_blocks = build_system_blocks(page_context, identity)

    # Built from the database, not from the in-memory blocks: the rows for this
    # turn were written above, so replaying history is the same code path
    # whether the turn is the first, the fiftieth, or a resume.
    messages = _load_history(conversation)

    tool_context = ToolContext(
        superset_session_cookie=superset_session_cookie,
        identity=identity,
        conversation=conversation,
        page_context=page_context.to_dict(),
    )

    if provider == "openai":
        client = OpenAIClient(api_key)
    elif provider == "deepseek":
        client = DeepSeekClient(api_key, base_url=settings.LIFEDATA_AI_DEEPSEEK_BASE_URL)
    else:
        client = anthropic.Anthropic(api_key=api_key)
    deadline = time.monotonic() + settings.LIFEDATA_AI_TURN_TIMEOUT_SECONDS
    max_rounds = settings.LIFEDATA_AI_MAX_TOOL_ROUNDS
    stop_reason = "error"
    #: Spec §5 caps write actions per turn, so one request cannot rewrite a
    #: whole workspace before the user has seen any of it.
    writes_proposed = 0
    #: One `ask_user` per turn, and never after a write. Both guards exist so the
    #: deferral has at most one thing to resolve.
    asked_this_turn = False
    #: Transient provider failures (429/529) retried across the whole turn, not
    #: per round — a busy period tends to affect several rounds in a row.
    provider_retries = 0

    for round_number in range(max_rounds + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            yield _event(
                "error",
                code="turn_timeout",
                message=(
                    "This turn took longer than "
                    f"{settings.LIFEDATA_AI_TURN_TIMEOUT_SECONDS} seconds and was stopped. "
                    "Try a narrower question."
                ),
            )
            break

        try:
            response = None
            request_id = ""
            with client.messages.stream(
                model=settings.LIFEDATA_AI_MODEL,
                max_tokens=MAX_TOKENS,
                system=system_blocks,
                messages=messages,
                tools=tool_specs,
                # Effort belongs inside output_config; it is not a top-level
                # parameter. Thinking is omitted entirely — it is on by default
                # on claude-opus-5, and `budget_tokens` is rejected outright.
                output_config={"effort": settings.LIFEDATA_AI_EFFORT},
                timeout=remaining,
            ) as stream:
                for chunk in stream:
                    if (
                        chunk.type == "content_block_delta"
                        and getattr(chunk.delta, "type", None) == "text_delta"
                    ):
                        yield _event("text_delta", text=chunk.delta.text)
                response = stream.get_final_message()
                # Read from the stream, not from `response._request_id`: the
                # latter is only populated on non-streaming calls, so taking it
                # from the accumulated message silently records an empty id and
                # the §9 traceability requirement quietly stops holding.
                request_id = stream.request_id or ""
        except (anthropic.APIStatusError, OpenAIStatusError) as exc:
            logger.warning("%s returned %s: %s", provider_label, exc.status_code, exc)
            # Failing the whole turn on an "Overloaded" makes a multi-step build
            # unusable: a dashboard takes several model rounds and any one of
            # them can hit it. Retry with backoff, bounded so a sustained outage
            # still surfaces.
            #
            # Do NOT key this on the status code alone. When an overload arrives
            # part-way through a STREAM, the HTTP response has already been sent
            # as 200 and the error is delivered in the body — the SDK then raises
            # APIStatusError with status_code == 200. Keying on the code alone
            # silently never retried, which is exactly the bug this comment
            # exists to stop someone reintroducing.
            retryable = exc.status_code in (429, 500, 502, 503, 529) or _is_transient(exc)
            if provider == "openai" and _openai_quota_exhausted(exc):
                retryable = False
            if retryable and provider_retries < _MAX_PROVIDER_RETRIES:
                provider_retries += 1
                backoff = min(2 ** provider_retries, 8)
                logger.info(
                    "Retrying after %s (attempt %s/%s) in %ss",
                    exc.status_code, provider_retries, _MAX_PROVIDER_RETRIES, backoff,
                )
                retry_id = f"retry-{provider_retries}"
                yield _event(
                    "tool_use",
                    id=retry_id,
                    name="_retry",
                    summary=f"the model service is busy — retrying ({provider_retries})",
                )
                time.sleep(backoff)
                # The matching result, which used to be omitted: the panel force-
                # failed every unclosed tool line at the end of the turn, so a
                # successful retry was drawn in red as if it had failed.
                yield _event(
                    "tool_result", tool_use_id=retry_id, name="_retry", ok=True, is_error=False
                )
                # The backoff is not model work and must not be charged to the
                # turn budget; otherwise a busy provider looks like a slow one.
                deadline += backoff
                continue
            yield _event(
                "error",
                code=f"{provider}_error",
                message=_provider_message(exc, provider),
            )
            break
        except (anthropic.APIConnectionError, OpenAIConnectionError) as exc:
            logger.warning("Could not reach %s: %s", provider_label, exc)
            yield _event(
                "error",
                code=f"{provider}_unreachable",
                message=f"Could not reach the {provider_label} API. Please try again.",
            )
            break

        # Checked before `response.content` is touched: on a refusal the
        # content list can be empty, and indexing it would raise.
        if response.stop_reason == "refusal":
            stop_reason = "refusal"
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            logger.info(
                "Turn refused by the model (category=%s, request_id=%s)", category, request_id
            )
            Message.objects.create(
                conversation=conversation,
                role=Message.Role.ASSISTANT,
                content=_serialise_blocks(response.content or []),
                anthropic_request_id=request_id,
            )
            yield _event(
                "error",
                code="refusal",
                message=(
                    "The model declined to answer this request. Rephrasing it, or asking a "
                    "narrower analytical question, usually helps."
                ),
            )
            break

        assistant_blocks = _serialise_blocks(response.content)
        Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content=assistant_blocks,
            anthropic_request_id=request_id,
        )
        messages.append({"role": "assistant", "content": assistant_blocks})

        if response.stop_reason == "pause_turn":
            # A server-side tool hit its own iteration limit. Re-sending the
            # conversation unchanged resumes it; no tool results are owed.
            continue

        if response.stop_reason == "tool_use":
            if round_number >= max_rounds:
                yield _event(
                    "error",
                    code="tool_round_limit",
                    message=(
                        f"The copilot reached its limit of {max_rounds} tool rounds for one "
                        "turn and stopped. Ask a more specific question to narrow the work."
                    ),
                )
                stop_reason = "tool_round_limit"
                break

            tool_uses = [block for block in response.content if block.type == "tool_use"]
            result_blocks: list[dict[str, Any]] = []
            deferred = False

            for block in tool_uses:
                arguments = dict(block.input or {})
                yield _event(
                    "tool_use",
                    id=block.id,
                    name=block.name,
                    summary=_summarise_tool_call(block.name, arguments),
                )
                tool = registry.get(block.name)
                is_write = tool is not None and tool.mode is ToolMode.WRITE

                if block.name == "ask_user":
                    refusal = _ask_user_refusal(
                        len(tool_uses), writes_proposed, asked_this_turn
                    )
                    if refusal:
                        audit_refusal(tool_context, block.name, arguments, refusal)
                        yield _event("tool_result", tool_use_id=block.id, name=block.name,
                                     ok=False, is_error=True)
                        result_blocks.append({
                            "type": "tool_result", "tool_use_id": block.id,
                            "content": refusal, "is_error": True,
                        })
                        continue

                if is_write and writes_proposed >= settings.LIFEDATA_AI_MAX_WRITES_PER_TURN:
                    # Refuse the extra rather than silently dropping it, so the
                    # model can tell the user what it did not do.
                    refusal = (
                        f"Refused: this turn already proposed "
                        f"{settings.LIFEDATA_AI_MAX_WRITES_PER_TURN} changes, which is the "
                        "per-turn limit. Ask the user to confirm these before proposing more."
                    )
                    audit_refusal(tool_context, block.name, arguments, refusal)
                    yield _event("tool_result", tool_use_id=block.id, name=block.name,
                                 ok=False, is_error=True)
                    result_blocks.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": refusal, "is_error": True,
                    })
                    continue

                result = execute(block.name, arguments, tool_context, registry=registry)
                yield _event(
                    "tool_result",
                    tool_use_id=block.id,
                    name=block.name,
                    ok=result.ok,
                    is_error=not result.ok,
                )

                if block.name in {"publish_dashboard", "edit_dashboard_chart_style"} and result.ok and isinstance(result.content, dict) and result.content.get("type") == "published_dashboard":
                    yield _event("published_dashboard", **result.content)

                if (
                    block.name == "author_canvas"
                    and result.ok
                    and isinstance(result.content, dict)
                    and result.content.get("type") == "sandboxed_canvas"
                ):
                    # Phase 7/10 prototype: the one deliberate exception to
                    # "tool_result carries no payload" -- the panel needs this
                    # content to mount the sandboxed iframe, it cannot render
                    # from a bare ok/is_error flag the way every other tool
                    # result does.
                    yield _event(
                        "canvas",
                        tool_use_id=block.id,
                        title=result.content.get("title"),
                        html_snippet=result.content.get("html_snippet"),
                        rows=result.content.get("rows"),
                        columns=result.content.get("columns"),
                    )

                if block.name == "ask_user" and result.ok and isinstance(result.content, dict):
                    asked_this_turn = True
                    clarification = Clarification.objects.create(
                        conversation=conversation,
                        tool_use_id=block.id,
                        question=result.content,
                    )
                    yield _event(
                        "clarification",
                        clarification_id=str(clarification.id),
                        tool_use_id=block.id,
                        questions=result.content.get("questions", []),
                    )
                    # DEFER. No tool_result is written: the user's answer becomes
                    # it on the next turn, so the model resumes its own call.
                    deferred = True
                    break

                if is_write and result.ok and isinstance(result.content, dict):
                    writes_proposed += 1
                    envelope = result.content
                    action_id = envelope.get("action_id")
                    is_low_risk = envelope.get("risk") == "low"
                    payload = {
                        "action_id": action_id,
                        "action_type": envelope.get("action_type"),
                        "risk": envelope.get("risk"),
                        "summary": envelope.get("summary"),
                        "envelope": envelope.get("envelope"),
                        "calls": envelope.get("calls"),
                        "warnings": envelope.get("warnings"),
                    }

                    if not is_low_risk:
                        # Waiting for a person. DEFER, exactly as `ask_user` does
                        # — see the module docstring. Blocking here would be a
                        # race against a human's attention span, and losing it
                        # leaves the model holding `pending`, unable to lay out
                        # the dashboard it just proposed, ending the turn with
                        # "press Apply and then ask me to arrange it".
                        _stamp_tool_use(action_id, block.id, result_blocks)
                        yield _event("action_preview", **payload)
                        deferred = True
                        break

                    yield _event("action_envelope", **payload)
                    # Auto-applied: the browser answers in a couple of seconds,
                    # and chaining "create chart" into "add it to the dashboard"
                    # genuinely needs the real id back.
                    outcome: dict[str, Any] = {}
                    for item in _await_action_outcome(action_id):
                        if isinstance(item, AgentEvent):
                            yield item
                            continue
                        _marker, outcome, waited = item
                        # Awaited browser time is not model work. Charging it to
                        # the turn budget meant nine round trips could hand the
                        # SDK a three-second timeout mid-build, surfacing as a
                        # provider timeout that looked like an Anthropic fault —
                        # so turn timeouts tracked network quality, which is the
                        # worst possible failure distribution for a pilot on a
                        # Ugandan facility connection.
                        deadline += waited
                    result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(outcome),
                    })
                    continue

                result_blocks.append(result.to_tool_result_block(block.id))

            if deferred:
                # The results computed BEFORE the deferral are deliberately not
                # persisted here. They belong in the same single `tool_result`
                # message as the deferred one — the API requires every result
                # for an assistant message to arrive together, immediately after
                # it — and that message is written on resume by
                # `_resolve_pending`, which covers every outstanding id at once.
                #
                # Writing them now would produce two TOOL rows for one assistant
                # message, which is a 400 even though every id has a result.
                # Observed exactly that way: a resize auto-applied and a filter
                # deferred, in one message, and the turn died on the next round.
                stop_reason = AWAITING_USER
                break

            # One user message carrying *all* results. Splitting them across
            # several messages trains the model out of parallel tool calls.
            Message.objects.create(
                conversation=conversation, role=Message.Role.TOOL, content=result_blocks
            )
            messages.append({"role": "user", "content": result_blocks})
            continue

        stop_reason = response.stop_reason or "end_turn"
        if stop_reason == "max_tokens":
            yield _event(
                "error",
                code="max_tokens",
                message=(
                    "The answer was cut short at the output limit, so it may be incomplete. "
                    "Asking for one part at a time will fit."
                ),
            )
        break
    else:
        # Reachable only through repeated `pause_turn`, which continues without
        # consuming a tool round of its own. Rare, but it must not end the
        # stream silently.
        stop_reason = "round_limit"
        yield _event(
            "error",
            code="round_limit",
            message=(
                f"The copilot reached its limit of {max_rounds} model rounds for one turn "
                "and stopped before finishing."
            ),
        )

    yield _event("message_stop", stop_reason=stop_reason, conversation_id=conversation_id)


def _record_user_message(
    conversation: Conversation, user_message: str, page_context: PageContext,
    attachments: list[dict[str, Any]] | None = None,
) -> None:
    """Persist the user's turn before any model call.

    Written first so the turn is auditable even if the call never happens (no
    key, provider down).
    """
    Message.objects.create(
        conversation=conversation,
        role=Message.Role.USER,
        content=[*(attachments or []), {"type": "text", "text": user_message}],
    )
    if not conversation.title:
        conversation.title = clean_text(user_message, TITLE_LENGTH)
    conversation.page_scope = page_context.to_dict()
    conversation.save(update_fields=["title", "page_scope", "updated_at"])


def _ask_user_refusal(
    calls_in_message: int, writes_proposed: int, asked_this_turn: bool
) -> str | None:
    """Why this `ask_user` call cannot be honoured, or None if it can.

    Each guard prevents a state that would otherwise have to be managed:

    * **Alone.** The Messages API requires every `tool_use` in a message to
      receive a `tool_result`, and deferring writes none. Deferring alongside
      another call would leave that other call permanently unanswered.
    * **Before writing, not after.** Asking after a change has been proposed is
      the double-confirmation the design exists to avoid, and it would leave two
      things pending at once.
    * **Once.** Two questions in one turn is an interrogation, and the second
      would be asked before the first was answered.
    """
    if calls_in_message > 1:
        return (
            "Refused: call ask_user on its own, not alongside other tools. Ask first, "
            "then act on the answer."
        )
    if writes_proposed:
        return (
            "Refused: ask before you build, not after. You have already proposed a change "
            "this turn, so make your best interpretation and say which one you used."
        )
    if asked_this_turn:
        return (
            "Refused: you have already asked this turn. Put every open decision into ONE "
            "ask_user call, or choose the most defensible reading and build it."
        )
    return None


def _stamp_tool_use(
    action_id: Any, tool_use_id: str, siblings: list[dict[str, Any]] | None = None
) -> None:
    """Record what a deferred proposal owes, and what its siblings already did.

    Written onto `validation_result` rather than a new column: it is diagnostic
    metadata about one proposal, it is already a JSON field, and
    `_resolve_pending` is the only reader.

    `siblings` are the `tool_result` blocks for calls in the SAME assistant
    message that ran before the deferral. They cannot be persisted at the time —
    the API wants every result for one assistant message in one user message
    immediately after it, and that message is not written until the user acts —
    so they are carried here and emitted alongside the resolution.

    Without this the siblings would be reported as "not attempted", which is a
    lie: a chart that was auto-applied really was created, and telling the model
    otherwise makes it build the thing twice.
    """
    if not action_id:
        return
    record = ActionRecord.objects.filter(id=action_id).first()
    if record is None:  # pragma: no cover - the row was just created
        return
    verdict = {**(record.validation_result or {}), "tool_use_id": tool_use_id}
    if siblings:
        verdict["sibling_results"] = siblings
    record.validation_result = verdict
    record.save(update_fields=["validation_result", "updated_at"])


def _cacheable_tool_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Put a cache breakpoint on the last tool spec.

    The tool array is the largest stable block in every request — 27 schemas,
    several of them the full governed action schemas — and it was re-billed at
    full price on every round of every turn. `ToolRegistry` already documents
    that insertion order is preserved "to keep the cache warm"; this is the line
    that made that promise mean something.

    A breakpoint marks the end of a prefix, so one on the final entry covers
    every spec before it.
    """
    if not specs:
        return specs
    marked = list(specs)
    marked[-1] = {**marked[-1], "cache_control": {"type": "ephemeral"}}
    return marked


def _openai_quota_exhausted(exc: Any) -> bool:
    body = getattr(exc, "body", {}) or {}
    error = body.get("error", body)
    return error.get("code") in {"insufficient_quota", "credit_balance_exhausted"}


def _provider_message(exc: Any, provider: str = "anthropic") -> str:
    """Turn an Anthropic HTTP error into something worth showing a user.

    A 400 is relayed with the provider's own wording, because in practice it is
    an operator problem the user cannot act on blindly — an exhausted credit
    balance and a rejected parameter both land here, and "the model request
    failed" sends whoever sees it looking in the wrong place. The body is
    Anthropic's own text, so there is nothing of ours to leak.
    """
    status_code = getattr(exc, "status_code", None)
    label = {"openai": "OpenAI", "deepseek": "DeepSeek", "anthropic": "Anthropic"}[provider]
    key_name = {"openai": "OPENAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[provider]
    if provider == "openai" and _openai_quota_exhausted(exc):
        return "The OpenAI API account has no credits remaining. Add credits in the OpenAI billing dashboard."
    if provider == "deepseek" and status_code == 402:
        return "The DeepSeek API account has insufficient balance. Add credits in the DeepSeek billing dashboard."
    if status_code == 401:
        return f"The configured {label} API key was rejected. Check {key_name}."
    if status_code == 429:
        return "The copilot is rate limited right now. Please try again shortly."
    if status_code is not None and status_code >= 500:
        return f"The {label} API is temporarily unavailable. Please try again."
    if status_code == 400:
        detail = clean_text(getattr(exc, "message", "") or str(exc), 240)
        return f"The {label} API rejected the request: {detail}"
    return "The copilot could not complete this turn because the model request failed."
