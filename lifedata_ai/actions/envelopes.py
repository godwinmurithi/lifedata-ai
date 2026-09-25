"""Signed action envelopes (spec §5).

The orchestrator never performs a Superset write. It decides *what* should
happen, signs that decision, and hands it to the panel; the panel executes it
with the user's own session cookie and CSRF token.

The signature is what makes that safe. Without it the panel — or anything that
can reach it, including a prompt-injected page — could rewrite the method, path
or body before executing. With it, the panel can only execute exactly what the
orchestrator approved, or nothing at all.

Envelope shape:
    {action_id, action_type, method, path, body, headers_required, expires_at}
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.conf import settings
from django.core import signing

#: A dedicated salt so an envelope can never be confused with — or substituted
#: for — any other value this project signs.
_SALT = "lifedata_ai.actions.envelope.v1"


class EnvelopeError(Exception):
    """An envelope was tampered with, malformed, or has expired."""


def build_envelope(
    *,
    action_id: str,
    action_type: str,
    calls: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble an unsigned envelope carrying an ordered sequence of calls.

    One user-visible change is not always one HTTP request. Putting a chart on a
    dashboard needs both the layout node *and* the chart-to-dashboard relation —
    Superset renders "There is no chart definition associated with this
    component" if only the layout is written. So an envelope carries a list, the
    panel executes it in order, and the whole sequence is one auditable action.

    Each call is `{method, path, body}`, optionally with `json_encode`. Paths
    must be relative Superset API paths: an absolute URL would let the panel be
    pointed at another host with the user's credentials attached, so it is
    refused here rather than trusted to the panel.

    `json_encode` names body keys the panel must serialise to a JSON *string*
    immediately before sending. Superset stores `position_json`, `params` and
    `json_metadata` as strings, but a body that arrives here already serialised
    cannot carry a `__NEW_ID__` placeholder as a *value* — substituting one would
    mean string surgery on signed content, which is not acceptable. So the value
    travels as a nested object, the panel substitutes ids structurally, and the
    named keys are stringified last. The key list is inside the signature, so a
    tampered frame cannot add or drop one.
    """
    if not calls:
        raise EnvelopeError("An action envelope must carry at least one call.")

    normalised: list[dict[str, Any]] = []
    for call in calls:
        method = str(call.get("method", "")).upper()
        path = str(call.get("path", ""))
        if not path.startswith("/api/"):
            raise EnvelopeError(
                f"Envelope path must be a relative Superset API path, got {path!r}."
            )
        if method not in {"POST", "PUT", "DELETE", "GET"}:
            raise EnvelopeError(f"Unsupported envelope method {method!r}.")
        entry: dict[str, Any] = {
            "method": method,
            "path": path,
            "body": call.get("body") or {},
        }
        json_encode = call.get("json_encode")
        if json_encode:
            if not isinstance(json_encode, list) or not all(
                isinstance(key, str) for key in json_encode
            ):
                raise EnvelopeError("json_encode must be a list of body key names.")
            missing = [key for key in json_encode if key not in entry["body"]]
            if missing:
                raise EnvelopeError(
                    f"json_encode names {missing} which the body does not contain."
                )
            entry["json_encode"] = list(json_encode)
        normalised.append(entry)

    ttl = settings.LIFEDATA_AI_ENVELOPE_TTL_SECONDS
    expires_at = dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=ttl)
    return {
        "action_id": str(action_id),
        "action_type": action_type,
        "calls": normalised,
        # The panel must fetch a CSRF token before executing. Naming it here
        # keeps the requirement in the contract rather than in panel folklore.
        "headers_required": ["csrf"],
        "expires_at": expires_at.isoformat(),
    }


def sign_envelope(envelope: dict[str, Any]) -> str:
    """Sign an envelope for transport to the panel."""
    return signing.dumps(envelope, salt=_SALT)


def verify_envelope(token: str) -> dict[str, Any]:
    """Recover an envelope, refusing anything tampered with or expired.

    Expiry is enforced twice: by the signer's own max_age and by the
    `expires_at` field. The first stops a replay of an old token; the second
    keeps the envelope self-describing, so the panel can refuse a stale action
    without a round trip.
    """
    ttl = settings.LIFEDATA_AI_ENVELOPE_TTL_SECONDS
    try:
        envelope = signing.loads(token, salt=_SALT, max_age=ttl)
    except signing.SignatureExpired as exc:
        raise EnvelopeError("This action expired before it was executed.") from exc
    except signing.BadSignature as exc:
        raise EnvelopeError("This action envelope was altered and cannot be trusted.") from exc

    expires_at = dt.datetime.fromisoformat(envelope["expires_at"])
    if dt.datetime.now(tz=dt.UTC) > expires_at:
        raise EnvelopeError("This action expired before it was executed.")
    return envelope
