"""The panel reports back what happened when it executed an action (spec §5).

This closes the audit loop. The orchestrator proposed a change and signed it;
the browser executed it; this endpoint records the outcome against the
ActionRecord so the change feed, undo, and the audit trail all reflect reality
rather than intent.

Two things here are load-bearing and easy to get wrong:

* **Status changes go through `ActionRecord.transition_to`.** Assigning
  `action.status` directly is how undo came to mark a change UNDONE and then
  crash before reversing anything. An illegal transition is refused with 409, not
  applied — the browser may genuinely send a late report for an action the user
  has already superseded, and overwriting a settled record would make the audit
  trail lie.
* **`undoes` closes the loop on the original action.** The original change
  becomes UNDONE only when the *undo* action reports `executed`, because only
  the browser knows whether the restore actually happened.
"""

from __future__ import annotations

import json
import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from lifedata_ai.models import ActionRecord

logger = logging.getLogger(__name__)

#: What the panel is allowed to report. Anything else is a bug in the panel.
_REPORTABLE = {
    "executed": ActionRecord.Status.EXECUTED,
    "failed": ActionRecord.Status.FAILED,
    "cancelled": ActionRecord.Status.CANCELLED,
}


@csrf_exempt  # Django CSRF does not apply: this app has no Django session, and
# identity is proven by the Superset session cookie that
# SupersetIdentityMiddleware verified before this view ran.
def action_result(request: HttpRequest) -> JsonResponse:
    if request.method != "POST":
        return JsonResponse({"error": "method_not_allowed"}, status=405)

    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    action_id = body.get("action_id")
    reported = str(body.get("status", "")).lower()
    if not action_id or reported not in _REPORTABLE:
        return JsonResponse(
            {
                "error": "invalid_request",
                "message": f"action_id and status are required; status must be one of "
                           f"{sorted(_REPORTABLE)}.",
            },
            status=400,
        )

    identity = request.superset_identity
    try:
        # Scoped to the caller: a user must not be able to close out someone
        # else's action. 404 rather than 403 — a 403 would confirm the id exists.
        action = ActionRecord.objects.select_related("conversation").get(
            id=action_id, conversation__superset_username=identity.username
        )
    except (ActionRecord.DoesNotExist, ValidationError, ValueError, TypeError):
        # `ValidationError` is the one that is easy to miss: Django raises it
        # from the UUID field's `to_python` BEFORE the query runs, so a
        # malformed id was a 500 rather than a 404 — and this endpoint is
        # reachable by anything holding a Superset session.
        return JsonResponse({"error": "not_found"}, status=404)

    new_status = _REPORTABLE[reported]

    with transaction.atomic():
        moved = action.transition_to(
            new_status,
            executed_by_username=identity.username,
            execution_result={
                "http_status": body.get("http_status"),
                "response_excerpt": str(body.get("response_excerpt", ""))[:2000],
                "superset_object_id": body.get("superset_object_id"),
            },
            **(
                {"superset_object_id": str(body["superset_object_id"])}
                if body.get("superset_object_id")
                else {}
            ),
        )
        if not moved:
            # The common real case: the user typed a new message instead of
            # pressing Apply, the loop superseded the proposal and cancelled the
            # record, and the browser's report arrives afterwards anyway. Say so
            # plainly rather than resurrecting a settled action.
            logger.info(
                "Refused %s -> %s on action %s (already %s)",
                action.status, new_status, action.id, action.status,
            )
            return JsonResponse(
                {
                    "error": "invalid_transition",
                    "message": (
                        f"This action is already “{action.get_status_display()}” and "
                        f"cannot become “{new_status}”. It was most likely superseded "
                        "or cancelled while the browser was executing it."
                    ),
                    "action_id": str(action.id),
                    "status": action.status,
                },
                status=409,
            )

        undone = _close_out_undo(action)

    logger.info(
        "Action %s reported %s by %s (superset object %s)",
        action.id, action.status, identity.username, action.superset_object_id or "-",
    )
    return JsonResponse(
        {
            "action_id": str(action.id),
            "status": action.status,
            "undoable": action.status == ActionRecord.Status.EXECUTED
            and action.snapshots.exists()
            # An undo of an undo is not something this build reasons about, and
            # offering the button would produce a confusing loop.
            and not action.action_type.startswith("undo_"),
            "undone_action_id": str(undone.id) if undone else None,
        }
    )


def _close_out_undo(action: ActionRecord) -> ActionRecord | None:
    """If this was an undo that succeeded, mark what it undid as UNDONE.

    The transition lives here, not in `views/undo.py`, and that placement is the
    entire fix for a verified bug: undo used to set `status = UNDONE` at
    *proposal* time and then raise a `TypeError` on the next line, so every
    `POST /api/undo/` recorded a change as reversed and then 500ed without
    reversing it. Only the browser knows whether the restore happened, and this
    is where the browser tells us.
    """
    if action.status != ActionRecord.Status.EXECUTED:
        return None
    undoes = (action.payload or {}).get("undoes")
    if not undoes:
        return None

    original = ActionRecord.objects.filter(
        id=undoes, conversation_id=action.conversation_id
    ).first()
    if original is None:
        return None
    if not original.transition_to(ActionRecord.Status.UNDONE):
        logger.warning(
            "Undo %s executed but original %s is %s and cannot become undone",
            action.id, original.id, original.status,
        )
        return None
    return original
