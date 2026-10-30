"""The streaming chat endpoint: `POST /lifedata-ai/api/chat/`.

The view is thin on purpose — it validates the request, resolves the
conversation, and relays `agent.loop` events as Server-Sent Events. All the
judgement lives in the loop.

Three things here are worth explaining rather than reading past:

* **`@csrf_exempt` is correct, not an oversight.** This application has no
  Django sessions and no Django login; the caller's identity is the Superset
  `session` cookie, verified by `SupersetIdentityMiddleware` before this view
  runs. Django's CSRF machinery protects a Django session cookie that does not
  exist here, and `django.contrib.sessions` is not even installed. The panel
  still sends Superset's `X-CSRFToken` — obtaining it proves a live Superset
  session and it is the same cached token the write executor needs — but it is
  not what authenticates this request.
* **`{"resume": true}` is a first-class request shape.** After the user presses
  Apply, the panel reports the outcome and then asks the loop to carry on. There
  is genuinely no user message in that case: the loop writes a `tool_result`
  instead, resuming the model's own tool call. Requiring a message here would
  have forced the panel to invent one, and a fabricated user turn beside the
  model's tool result is exactly what the deferred-resume design avoids.
* **The generator is wrapped.** An exception escaping mid-stream would close
  the connection with a partially written answer, which the browser cannot
  distinguish from a dropped network. The wrapper converts it into an `error`
  frame followed by `message_stop`, so the panel always ends cleanly.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt

from lifedata_ai.agent import inflight
from lifedata_ai.agent.context import PageContext
from lifedata_ai.agent.loop import (
    AgentEvent,
    ConversationNotFound,
    NothingToResume,
    resolve_conversation,
    run_turn,
)
from lifedata_ai.attachments import AttachmentError, attachment_blocks, extract_tabular_attachments
from lifedata_ai.middleware import SupersetIdentity
from lifedata_ai.models import ChatAttachment, Conversation

logger = logging.getLogger(__name__)

#: A single user message is a prompt, not a document upload. Anything longer is
#: either a mistake or an attempt to blow out the context budget.
MAX_MESSAGE_LENGTH = 8000


def _persist_tabular_attachments(
    *, conversation: Conversation, identity: SupersetIdentity, tabular_attachments: list[dict],
) -> dict:
    """Save raw bytes of this turn's CSV/TSV/XLSX uploads, and tell the model their ids.

    A plain text block, not a tool result — there is no tool call for the model
    to have made yet. The model reads this the same way it reads any other
    attachment note, and quotes an id back as `import_dataset_from_attachment`'s
    `attachment_id` argument instead of retyping the file.
    """
    rows = [
        ChatAttachment.objects.create(
            conversation=conversation,
            filename=item["filename"],
            file_type=item["file_type"],
            delimiter=item["delimiter"],
            data=item["data"],
            uploaded_by_username=identity.username,
        )
        for item in tabular_attachments
    ]
    references = "; ".join(f"{row.filename} -> attachment_id {row.pk}" for row in rows)
    return {
        "type": "text",
        "text": (
            "Attachment references for import_dataset_from_attachment (quote the id "
            f"back exactly, never retype the file's rows): {references}"
        ),
    }


def _frame(event: AgentEvent) -> str:
    """Render one SSE frame.

    `json.dumps` is what makes this safe: it escapes newlines, so no value in
    the payload can terminate the frame early and forge a second event.
    """
    return f"event: {event.name}\ndata: {json.dumps(event.data, default=str)}\n\n"


@csrf_exempt
def chat(request: HttpRequest) -> HttpResponse:
    """Run one chat turn and stream the result."""
    if request.method != "POST":
        return JsonResponse(
            {"error": "method_not_allowed", "message": "Use POST to send a chat message."},
            status=405,
        )

    try:
        payload = json.loads(request.POST.get("payload", "{}")) if request.content_type == "multipart/form-data" else json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse(
            {"error": "invalid_json", "message": "The request body was not valid JSON."},
            status=400,
        )
    if not isinstance(payload, dict):
        return JsonResponse(
            {"error": "invalid_json", "message": "The request body must be a JSON object."},
            status=400,
        )

    try:
        attachments = attachment_blocks(request.FILES.getlist("files"))
    except AttachmentError as exc:
        return JsonResponse({"error": "invalid_attachment", "message": str(exc)}, status=400)
    # Re-reads the same uploads for the subset `import_dataset_from_attachment`
    # can use — cheap relative to the request, and failing attachment_blocks
    # above already screened out oversized/unreadable files.
    tabular_attachments = extract_tabular_attachments(request.FILES.getlist("files"))

    is_resume = payload.get("resume") is True
    raw_message = payload.get("message")
    message = raw_message.strip() if isinstance(raw_message, str) else ""

    # The empty-message check moves AFTER the resume branch, and stays exactly as
    # strict for a real message. A resume carries no message by design.
    if is_resume and attachments:
        return JsonResponse({"error": "invalid_attachment", "message": "Send attachments with a new message."}, status=400)
    if attachments and not message:
        message = "Please inspect the attached files."
    if not is_resume and not message:
        return JsonResponse(
            {"error": "empty_message", "message": "A non-empty message is required."},
            status=400,
        )
    if len(message) > MAX_MESSAGE_LENGTH:
        return JsonResponse(
            {
                "error": "message_too_long",
                "message": f"Messages are limited to {MAX_MESSAGE_LENGTH} characters.",
            },
            status=400,
        )

    page_context = PageContext.from_payload(payload.get("page_context"))
    conversation_id = payload.get("conversation_id")
    if conversation_id is not None and not isinstance(conversation_id, str):
        return JsonResponse(
            {"error": "invalid_conversation_id", "message": "conversation_id must be a string."},
            status=400,
        )
    if is_resume and not conversation_id:
        return JsonResponse(
            {
                "error": "invalid_request",
                "message": "A resume request must name the conversation to continue.",
            },
            status=400,
        )

    identity = request.superset_identity
    try:
        conversation = resolve_conversation(
            identity=identity,
            conversation_id=conversation_id,
            page_context=page_context,
        )
    except ConversationNotFound:
        # 404 rather than 403: another user's conversation must look exactly
        # like one that does not exist.
        return JsonResponse(
            {"error": "not_found", "message": "No such conversation."}, status=404
        )
    except (ValueError, ValidationError):
        # A malformed UUID never reaches the database as a lookup — Django
        # raises before the query. Same verdict as a missing row.
        return JsonResponse(
            {"error": "not_found", "message": "No such conversation."}, status=404
        )

    # One turn at a time per conversation. The panel's post-Apply continuation
    # and a message the user typed a moment earlier can otherwise both try to
    # resolve the same pending tool_use, and whichever loses produces a history
    # the Messages API rejects. See agent/inflight.py.
    if not inflight.claim(conversation):
        return JsonResponse(
            {
                "error": "turn_in_progress",
                "message": (
                    "The copilot is still working on the previous message in this "
                    "conversation. Wait for it to finish, or press Stop."
                ),
            },
            status=409,
        )

    if tabular_attachments:
        attachments.append(_persist_tabular_attachments(
            conversation=conversation, identity=identity, tabular_attachments=tabular_attachments,
        ))

    stream = _safe_stream(
        conversation=conversation,
        identity=identity,
        superset_session_cookie=request.superset_session_cookie,
        page_context=page_context,
        user_message=None if is_resume else message,
        attachments=attachments,
    )

    response = StreamingHttpResponse(stream, content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    # nginx already has proxy_buffering off for this location, but a buffering
    # proxy anywhere in front would hold the whole turn and defeat streaming.
    response["X-Accel-Buffering"] = "no"
    return response


def _safe_stream(
    *,
    conversation: Conversation,
    identity: SupersetIdentity,
    superset_session_cookie: str,
    page_context: PageContext,
    user_message: str | None,
    attachments: list[dict] | None = None,
) -> Iterator[str]:
    """Relay loop events, guaranteeing a terminating frame either way.

    The `finally` releases the conversation's turn slot. It must run even when
    the client disconnects mid-stream — a browser that navigates away during a
    two-minute build would otherwise leave the conversation claimed until the
    stale-claim margin expired, and the user's next message would 409.
    """
    try:
        for event in run_turn(
            conversation=conversation,
            identity=identity,
            superset_session_cookie=superset_session_cookie,
            page_context=page_context,
            user_message=user_message,
            **({"attachments": attachments} if attachments else {}),
        ):
            yield _frame(event)
    except NothingToResume:
        logger.info("Resume requested on %s with nothing pending", conversation.pk)
        yield _frame(
            AgentEvent(
                "error",
                {
                    "code": "nothing_to_resume",
                    "message": (
                        "There is nothing waiting on this conversation. "
                        "Send a message to continue."
                    ),
                },
            )
        )
        yield _frame(AgentEvent("message_stop", {"stop_reason": "error"}))
    except Exception:  # noqa: BLE001 - the browser must not be left hanging
        logger.exception("Chat turn failed unexpectedly")
        yield _frame(
            AgentEvent(
                "error",
                {
                    "code": "internal_error",
                    "message": (
                        "The copilot hit an unexpected error and stopped. "
                        "The problem has been logged."
                    ),
                },
            )
        )
        yield _frame(AgentEvent("message_stop", {"stop_reason": "error"}))
    finally:
        inflight.release(conversation)
