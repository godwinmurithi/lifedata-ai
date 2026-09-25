"""Undo the last executed change (spec §4).

Undo is not a special privileged path. It builds an ordinary signed envelope
that restores the snapshot, and the panel executes it exactly like any other
write — so undo is audited, permission-checked, and subject to the same
identity model as the change it reverses.

It refuses when the object has moved on since the snapshot: silently
overwriting someone else's later edit would be worse than declining.
"""

from __future__ import annotations

import json
import logging

from django.core.exceptions import ValidationError
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from lifedata_ai.actions.envelopes import build_envelope, sign_envelope
from lifedata_ai.models import ActionRecord, Snapshot
from lifedata_ai.superset_client import SupersetClient, SupersetError
from lifedata_ai.tools.superset_write import owns

logger = logging.getLogger(__name__)


@csrf_exempt  # See the note in action_result.py — identity is the Superset cookie.
def undo(request: HttpRequest) -> JsonResponse:
    if request.method != "POST":
        return JsonResponse({"error": "method_not_allowed"}, status=405)

    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    identity = request.superset_identity
    actions = ActionRecord.objects.filter(
        conversation__superset_username=identity.username,
        status=ActionRecord.Status.EXECUTED,
    ).order_by("-updated_at")

    try:
        if body.get("action_id"):
            actions = actions.filter(id=body["action_id"])
        if body.get("conversation_id"):
            actions = actions.filter(conversation_id=body["conversation_id"])
        action = actions.first()
    except (ValidationError, ValueError, TypeError):
        # Django's UUID field raises from `to_python` BEFORE the query runs, so a
        # malformed id was a 500 rather than a 404 on an endpoint anything with a
        # Superset session can reach.
        return JsonResponse(
            {"error": "nothing_to_undo", "message": "There is no such change to undo."},
            status=404,
        )
    if action is None:
        return JsonResponse(
            {"error": "nothing_to_undo", "message": "There is no executed change to undo."},
            status=404,
        )

    snapshot = action.snapshots.order_by("-captured_at").first()
    mode = _resolve_mode(body.get("mode"), action, snapshot)
    if mode is None:
        return JsonResponse(
            {
                "error": "invalid_mode",
                "message": "mode must be 'restore' or 'delete'.",
            },
            status=400,
        )

    if mode == MODE_DELETE:
        return _delete_response(request, action, identity)

    if snapshot is None:
        return JsonResponse(
            {
                "error": "not_undoable",
                "message": (
                    f"“{action.action_type}” cannot be undone: no prior state was captured. "
                    "Creating an object has nothing to restore — delete it instead."
                ),
            },
            status=409,
        )

    # Staleness check: refuse if the object changed after we snapshotted it.
    client = SupersetClient(session_cookie=request.superset_session_cookie)
    try:
        if snapshot.object_type == Snapshot.ObjectType.CHART:
            current = client.get_chart(int(snapshot.object_id))
        else:
            current = client.get_dashboard(snapshot.object_id)
    except SupersetError as exc:
        return JsonResponse(
            {"error": "superset_error", "message": str(exc)},
            status=502 if not exc.is_permission_denied else 403,
        )

    current_changed_on = str(current.get("changed_on") or "")
    if snapshot.changed_on and current_changed_on and current_changed_on != snapshot.changed_on:
        return JsonResponse(
            {
                "error": "stale_snapshot",
                "message": (
                    f"This {snapshot.object_type} has changed since the copilot edited it, so "
                    "undoing would discard that newer change. Review it manually instead."
                ),
            },
            status=409,
        )

    calls = _restore_calls(snapshot)

    undo_record = ActionRecord.objects.create(
        conversation=action.conversation,
        action_type=f"undo_{action.action_type}",
        # `undoes` is what `action_result` reads to flip the ORIGINAL record to
        # UNDONE — and only once this undo action reports `executed`. Marking it
        # here, before the browser has restored anything, is the bug this whole
        # rewrite exists to remove.
        payload={"undoes": str(action.id), "snapshot_id": snapshot.id},
        validation_result={"undo": True, "of": str(action.id)},
        risk=ActionRecord.Risk.LOW,
        status=ActionRecord.Status.PROPOSED,
        superset_object_type=snapshot.object_type,
        superset_object_id=snapshot.object_id,
    )

    envelope = build_envelope(
        action_id=str(undo_record.id),
        action_type=undo_record.action_type,
        calls=calls,
    )
    logger.info("Prepared undo of %s for %s", action.id, identity.username)
    return JsonResponse(
        {
            "action_id": str(undo_record.id),
            "action_type": undo_record.action_type,
            "mode": MODE_RESTORE,
            "undoes": str(action.id),
            "summary": f"restore {snapshot.object_type} {snapshot.object_id}",
            "envelope": sign_envelope(envelope),
            # `calls`, not `preview`. The panel's `executeAction` reads
            # `action.calls` and returns immediately when it is empty, so the
            # old `preview` key meant undo silently did nothing even once the
            # TypeError above it was fixed.
            "calls": calls,
        }
    )


# --- Deletion as the inverse of creation ---------------------------------
#
# Executives create things, so "I didn't want that" has to be answerable. But it
# must be a DISTINCT affordance, never the Undo button quietly redefined:
#
#   * Undo restores state. Deleting is destructive and is *itself* unundoable.
#   * The chart may already be on another dashboard, or a colleague may have
#     edited it since.
#   * Silently redefining "Undo" as "Delete" is the sort of thing that ends a
#     pilot.
#
# **`delete_chart` and `delete_dashboard` stay out of the tool registry.** The
# model can never propose a deletion — strictly better than adding two
# destructive tools it might reach for. The human gets the affordance; the model
# does not get the capability.

MODE_RESTORE = "restore"
MODE_DELETE = "delete"

#: Which Superset object a creating action produced, and where it lives.
_DELETABLE = {
    Snapshot.ObjectType.CHART: "/api/v1/chart/",
    Snapshot.ObjectType.DASHBOARD: "/api/v1/dashboard/",
}


def _resolve_mode(requested: object, action: ActionRecord, snapshot: Snapshot | None) -> str | None:
    """Which inverse this is, defaulting to whichever the action admits of.

    A change with a snapshot is restorable, so `restore`. A creation has no
    prior state, so the only meaningful inverse is `delete`. Returning None
    means the caller asked for something that is not a mode at all.
    """
    if requested is None:
        return MODE_RESTORE if snapshot is not None else MODE_DELETE
    if requested in (MODE_RESTORE, MODE_DELETE):
        return str(requested)
    return None


def _delete_response(
    request: HttpRequest, action: ActionRecord, identity: object
) -> JsonResponse:
    """Sign a DELETE for an object this action created.

    Preconditions are re-checked here rather than trusted from proposal time,
    because deletion is unundoable and the record may be hours old:

    * the action must have created something, and we must know what and which;
    * the object must still exist;
    * the caller must still own it.

    The response shape is byte-identical to a write tool's return value, so the
    panel's `executeAction` needs no special case — which was clearly the
    original intent for undo and had been lost.
    """
    object_type = action.superset_object_type
    object_id = action.superset_object_id
    path = _DELETABLE.get(object_type)

    if not path or not object_id:
        return JsonResponse(
            {
                "error": "not_deletable",
                "message": (
                    f"“{action.action_type}” did not create an object this copilot can "
                    "delete, so there is nothing to remove."
                ),
            },
            status=409,
        )

    client = SupersetClient(session_cookie=request.superset_session_cookie)
    try:
        current = (
            client.get_chart(int(object_id))
            if object_type == Snapshot.ObjectType.CHART
            else client.get_dashboard(object_id)
        )
    except SupersetError as exc:
        return JsonResponse(
            {"error": "superset_error", "message": str(exc)},
            status=403 if exc.is_permission_denied else 502,
        )

    # `owns` rather than a username comparison, because Superset's owner objects
    # carry an `id` and NO `username` — see its docstring. Matching on the name
    # found nothing, every time, so this refused every deletion including the
    # caller's own.
    owners = current.get("owners") or []
    if owners and not owns(owners, identity):
        return JsonResponse(
            {
                "error": "not_owner",
                "message": (
                    f"This {object_type} is no longer yours to delete. Ask its owner to "
                    "remove it."
                ),
            },
            status=403,
        )

    name = current.get("slice_name") or current.get("dashboard_title") or object_id
    calls = [{"method": "DELETE", "path": f"{path}{object_id}", "body": {}}]

    delete_record = ActionRecord.objects.create(
        conversation=action.conversation,
        action_type=f"delete_{object_type}",
        payload={"deletes": str(action.id), "superset_object_id": object_id},
        validation_result={"delete": True, "of": str(action.id), "calls": calls},
        # Deletion is destructive and unundoable, so it is never auto-applied
        # however small it looks.
        risk=ActionRecord.Risk.PREVIEW,
        status=ActionRecord.Status.PROPOSED,
        superset_object_type=object_type,
        superset_object_id=str(object_id),
    )

    envelope = build_envelope(
        action_id=str(delete_record.id),
        action_type=delete_record.action_type,
        calls=calls,
    )
    logger.info(
        "Prepared deletion of %s %s (from action %s) for %s",
        object_type, object_id, action.id, identity.username,
    )
    return JsonResponse(
        {
            "action_id": str(delete_record.id),
            "action_type": delete_record.action_type,
            "mode": MODE_DELETE,
            "deletes": str(action.id),
            "summary": (
                f"delete {object_type} “{name}”"
                + (" — the charts on it are kept" if object_type == "dashboard" else "")
            ),
            "destructive": True,
            "envelope": sign_envelope(envelope),
            "calls": calls,
        }
    )


def _restore_calls(snapshot: Snapshot) -> list[dict]:
    """Build the calls that put the object back the way it was.

    A list, because `build_envelope` takes a sequence: one user-visible change is
    not always one HTTP request, and undo goes down the same path as any other
    write so that it is audited, permission-checked and executed by the user's
    own session.
    """
    state = snapshot.state or {}
    if snapshot.object_type == Snapshot.ObjectType.CHART:
        restore = {
            key: state[key]
            for key in ("slice_name", "params", "query_context")
            if state.get(key) is not None
        }
        return [{"method": "PUT", "path": f"/api/v1/chart/{snapshot.object_id}", "body": restore}]

    restore = {
        key: state[key]
        for key in ("dashboard_title", "position_json", "json_metadata")
        if state.get(key) is not None
    }
    return [
        {"method": "PUT", "path": f"/api/v1/dashboard/{snapshot.object_id}", "body": restore}
    ]
