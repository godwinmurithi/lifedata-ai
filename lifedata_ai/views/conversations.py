"""Conversation history endpoints (spec §4, §7).

The panel uses these to show a conversation list and to reopen a thread after a
page reload — the acceptance criterion that conversation history survives a
refresh.

**Every query is scoped to `request.superset_identity.username` on the way in.**
That is not a convenience: conversation ids are UUIDs, but they still appear in
panel state and in logs, so a leaked id must not become a readable transcript.
Another user's conversation returns **404, never 403** — a 403 would confirm
that the id exists, which is exactly the fact being protected.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from django.db.models import Count
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from lifedata_ai.actions.envelopes import build_envelope, sign_envelope
from lifedata_ai.models import ActionRecord, Clarification, Conversation, Message

#: The panel shows a short recent list, not an archive browser.
LIST_LIMIT = 50

#: Transcripts are replayed into the panel, so a very long thread would be a
#: large synchronous response. The most recent turns are what a user reopens
#: for; older ones stay in the database and in the audit trail.
DETAIL_MESSAGE_LIMIT = 200


def _summarise(conversation: Conversation, message_count: int | None = None) -> dict[str, Any]:
    return {
        "id": str(conversation.pk),
        "title": conversation.title,
        "page_scope": conversation.page_scope,
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
        "message_count": message_count,
    }


def _visible_text(content: Any) -> str:
    """The assistant/user text of a stored message, for display.

    Thinking blocks are deliberately excluded: they are kept for faithful replay
    (see `agent/loop.py`), not for reading back to the user.

    Tool results are *mostly* excluded for the same reason — but not entirely.
    A clarification answer is stored as a `tool_result`, because the whole point
    of the deferred-resume design is that the user's reply resumes the model's
    own tool call. Returning "" for every TOOL-role message meant a replayed
    thread lost the executive's answer: they would scroll back and see the
    question with nothing under it.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    if parts:
        return "".join(part for part in parts if part)

    return _answer_text(content)


def _answer_text(content: Any) -> str:
    """The user's own words out of a `tool_result`, or "" for anything else.

    Only an `answered` clarification is surfaced. A tool result carrying a
    dataset listing or a Superset error is machinery, and replaying it as though
    the user had said it would be worse than losing it.
    """
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        raw = block.get("content")
        if not isinstance(raw, str):
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("status") == "answered":
            return str(payload.get("answer") or "")
    return ""


def _not_found() -> JsonResponse:
    return JsonResponse({"error": "not_found", "message": "No such conversation."}, status=404)


def list_conversations(request: HttpRequest) -> JsonResponse:
    """The caller's most recently updated conversations."""
    if request.method != "GET":
        return JsonResponse(
            {"error": "method_not_allowed", "message": "Use GET to list conversations."},
            status=405,
        )

    rows = (
        Conversation.objects.filter(superset_username=request.superset_identity.username)
        .annotate(message_total=Count("messages"))
        .order_by("-updated_at")[:LIST_LIMIT]
    )
    return JsonResponse(
        {"conversations": [_summarise(row, row.message_total) for row in rows]}
    )


@csrf_exempt
def conversation_detail(request: HttpRequest, pk: UUID) -> JsonResponse:
    """Retrieve or delete one conversation.

    CSRF-exempt for the same reason as the chat endpoint: there is no Django
    session for Django's CSRF token to protect. The Superset session cookie is
    the credential, and the middleware has already verified it.
    """
    if request.method not in {"GET", "DELETE"}:
        return JsonResponse(
            {"error": "method_not_allowed", "message": "Use GET or DELETE."}, status=405
        )

    # `<uuid:pk>` in urls.py rejects a malformed id at the router, so this
    # query only ever sees a well-formed UUID.
    username = request.superset_identity.username
    conversation = Conversation.objects.filter(pk=pk, superset_username=username).first()
    if conversation is None:
        return _not_found()

    if request.method == "DELETE":
        # Messages, tool-call logs, actions and snapshots cascade with it.
        conversation.delete()
        return JsonResponse({"deleted": str(pk)}, status=200)

    messages = list(conversation.messages.all())
    truncated = len(messages) > DETAIL_MESSAGE_LIMIT
    if truncated:
        messages = messages[-DETAIL_MESSAGE_LIMIT:]

    payload = _summarise(conversation, conversation.messages.count())
    payload["truncated"] = truncated
    payload["publications"] = _publication_receipts(messages)
    payload["messages"] = [
        {
            "id": message.id,
            "role": message.role,
            "text": _visible_text(message.content),
            "created_at": message.created_at.isoformat(),
            "anthropic_request_id": message.anthropic_request_id,
        }
        for message in messages
    ]
    # A mechanism that waits indefinitely must survive a reload. Both kinds of
    # pending work are surfaced so the panel can put the question — or the Apply
    # button — back on screen; a card that survives on the server but not on
    # screen is worse than one that never persisted at all.
    payload["pending_clarification"] = _pending_clarification(conversation)
    payload["pending_action"] = _pending_action(conversation)
    return JsonResponse(payload)


def _pending_clarification(conversation: Conversation) -> dict[str, Any] | None:
    row = (
        conversation.clarifications.filter(status=Clarification.Status.PENDING)
        .order_by("-created_at")
        .first()
    )
    if row is None:
        return None
    return {
        "id": str(row.id),
        "tool_use_id": row.tool_use_id,
        "questions": (row.question or {}).get("questions", []),
        "created_at": row.created_at.isoformat(),
    }


def _pending_action(conversation: Conversation) -> dict[str, Any] | None:
    """A proposal still awaiting Apply, re-signed for the panel.

    The envelope is rebuilt rather than stored, because envelopes expire — a
    proposal the user comes back to tomorrow needs a fresh signature, and one
    they abandoned should not stay executable in a stale browser tab.
    """
    row = (
        conversation.actions.filter(
            status__in=[ActionRecord.Status.PROPOSED, ActionRecord.Status.APPROVED]
        )
        .order_by("-created_at")
        .first()
    )
    if row is None:
        return None

    calls = (row.validation_result or {}).get("calls")
    if not calls:
        # Nothing executable to re-offer. Surfacing it anyway would render an
        # Apply button that does nothing, which is worse than no card.
        return None

    envelope = build_envelope(
        action_id=str(row.id), action_type=row.action_type, calls=calls
    )
    return {
        "action_id": str(row.id),
        "action_type": row.action_type,
        "risk": row.risk,
        "summary": (row.validation_result or {}).get("summary") or row.action_type,
        "envelope": sign_envelope(envelope),
        "calls": calls,
        "created_at": row.created_at.isoformat(),
    }


def _publication_receipts(messages) -> list[dict]:
    """Rehydrate dashboard links without exposing multimodal file bytes."""
    receipts = {}
    for message in messages:
        if message.role != Message.Role.TOOL:
            continue
        for block in message.content:
            if block.get("type") != "tool_result" or not isinstance(block.get("content"), str):
                continue
            try:
                result = json.loads(block["content"])
            except ValueError:
                continue
            if isinstance(result, dict) and result.get("type") == "published_dashboard":
                receipts[result.get("publication_id")] = result
    return list(receipts.values())
