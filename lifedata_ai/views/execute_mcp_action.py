"""Executes a write action via native MCP when the panel reports the user confirmed it.

Counterpart to action_result.py, for actions WriteTool.run() marked
`execution_mode="mcp"` (tools/mcp_write_dispatch.py). Unlike action_result.py
-- which only RECORDS what the browser already did by calling Superset
itself -- this endpoint PERFORMS the write, server-side, using the calling
user's own minted MCP identity (superset_mcp_client.py).

Nothing user-supplied travels in the request body except `action_id`: every
detail of the write comes from the ActionRecord this server already created
and validated in WriteTool.run(), so there is nothing here for a tampered
request to smuggle in -- if anything, a smaller trust surface than
action_result.py's `superset_object_id`/`response_excerpt` fields, which are
just recorded for audit rather than acted on.
"""

from __future__ import annotations

import json
import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from lifedata_ai.models import ActionRecord
from lifedata_ai.superset_client import SupersetError
from lifedata_ai.superset_mcp_client import SupersetMCPClient
from lifedata_ai.tools.mcp_write_dispatch import execute_via_mcp

logger = logging.getLogger(__name__)


@csrf_exempt  # Same rationale as action_result.py: no Django session exists here,
# identity is proven by the Superset session cookie SupersetIdentityMiddleware
# already verified before this view ran.
def execute_mcp_action(request: HttpRequest) -> JsonResponse:
    if request.method != "POST":
        return JsonResponse({"error": "method_not_allowed"}, status=405)

    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    action_id = body.get("action_id")
    if not action_id:
        return JsonResponse(
            {"error": "invalid_request", "message": "action_id is required"}, status=400
        )

    identity = request.superset_identity
    try:
        # Scoped to the caller, same as action_result.py -- a user must not be
        # able to trigger someone else's proposed action.
        action = ActionRecord.objects.select_related("conversation").get(
            id=action_id, conversation__superset_username=identity.username
        )
    except (ActionRecord.DoesNotExist, ValidationError, ValueError, TypeError):
        return JsonResponse({"error": "not_found"}, status=404)

    if (action.validation_result or {}).get("execution_mode") != "mcp":
        # Refuse rather than silently falling back -- a payload that wasn't
        # translated for MCP at proposal time (tools/mcp_write_dispatch.py)
        # must not be guessed at here.
        return JsonResponse(
            {
                "error": "wrong_execution_mode",
                "message": "This action was not proposed for MCP execution.",
            },
            status=409,
        )

    if action.status != ActionRecord.Status.PROPOSED:
        return JsonResponse(
            {
                "error": "invalid_transition",
                "message": (
                    f"This action is already “{action.get_status_display()}” and "
                    "cannot be executed again."
                ),
                "action_id": str(action.id),
                "status": action.status,
            },
            status=409,
        )

    mcp_client = SupersetMCPClient(username=identity.username)
    try:
        outcome = execute_via_mcp(mcp_client, action.action_type, action.payload or {})
    except (SupersetError, ValueError) as exc:
        logger.warning("MCP execution failed for action %s: %s", action.id, exc)
        with transaction.atomic():
            action.transition_to(
                ActionRecord.Status.FAILED,
                executed_by_username=identity.username,
                execution_result={"error": str(exc)},
            )
        return JsonResponse(
            {"ok": False, "action_id": str(action.id), "status": action.status, "error": str(exc)}
        )

    with transaction.atomic():
        extra_fields = (
            {"superset_object_id": str(outcome["superset_object_id"])}
            if outcome.get("superset_object_id")
            else {}
        )
        moved = action.transition_to(
            ActionRecord.Status.EXECUTED,
            executed_by_username=identity.username,
            execution_result=outcome,
            **extra_fields,
        )

    if not moved:
        # Same real-world case action_result.py guards against: superseded or
        # cancelled between proposal and confirm.
        return JsonResponse(
            {
                "error": "invalid_transition",
                "message": f"This action is already “{action.get_status_display()}”.",
                "action_id": str(action.id),
                "status": action.status,
            },
            status=409,
        )

    logger.info(
        "Action %s executed via MCP by %s (superset object %s)",
        action.id, identity.username, action.superset_object_id or "-",
    )
    return JsonResponse(
        {
            "ok": True,
            "action_id": str(action.id),
            "status": action.status,
            "superset_object_id": outcome.get("superset_object_id"),
            "url": outcome.get("url"),
            "undoable": action.snapshots.exists(),
        }
    )
