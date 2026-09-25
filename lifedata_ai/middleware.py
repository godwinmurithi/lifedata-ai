"""Superset session identity verification (spec §1).

This is how database auth is bridged without SSO: the browser sends its Superset
`session` cookie to /lifedata-ai/* (same origin, so it is attached
automatically), and we verify it by asking Superset who that is.

The result is cached per session for 60 seconds so a chat turn doesn't make an
identity round-trip per tool call.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field

from django.conf import settings
from django.http import JsonResponse

from lifedata_ai.superset_client import SupersetClient, SupersetError

logger = logging.getLogger(__name__)

#: Paths under /lifedata-ai/ that do NOT require a Superset session.
#: Deliberately tiny: the health check (so the container can report readiness)
#: and static assets (the panel bundle itself, which is fetched before login on
#: the Superset login page).
PUBLIC_PATH_PREFIXES = (
    "/lifedata-ai/api/health/",
    "/lifedata-ai/static/",
)


@dataclass
class SupersetIdentity:
    """Who the caller is, according to Superset."""

    username: str
    user_id: int | None = None
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    roles: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        full = f"{self.first_name} {self.last_name}".strip()
        return full or self.username

    @property
    def is_admin(self) -> bool:
        return "Admin" in self.roles


class _IdentityCache:
    """In-process TTL cache keyed by a hash of the session cookie.

    The cookie value itself is never used as a key or logged — only its digest,
    so a memory dump or log line can't yield a usable session.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, SupersetIdentity]] = {}

    @staticmethod
    def _key(cookie: str) -> str:
        return hashlib.sha256(cookie.encode()).hexdigest()

    def get(self, cookie: str) -> SupersetIdentity | None:
        entry = self._entries.get(self._key(cookie))
        if not entry:
            return None
        expires_at, identity = entry
        if time.monotonic() > expires_at:
            self._entries.pop(self._key(cookie), None)
            return None
        return identity

    def set(self, cookie: str, identity: SupersetIdentity) -> None:
        ttl = settings.LIFEDATA_AI_IDENTITY_CACHE_SECONDS
        self._entries[self._key(cookie)] = (time.monotonic() + ttl, identity)
        # Opportunistic pruning so a long-lived process doesn't grow unbounded.
        if len(self._entries) > 512:
            now = time.monotonic()
            self._entries = {k: v for k, v in self._entries.items() if v[0] > now}


_cache = _IdentityCache()


class SupersetIdentityMiddleware:
    """Rejects any /lifedata-ai/ request that isn't a logged-in Superset user.

    On success, attaches `request.superset_identity` and
    `request.superset_session_cookie` for downstream views and tools.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if not path.startswith("/lifedata-ai/"):
            return self.get_response(request)
        if path.startswith(PUBLIC_PATH_PREFIXES):
            return self.get_response(request)

        if request.method in _STATE_CHANGING_METHODS:
            refused = _check_origin(request)
            if refused is not None:
                return refused

        cookie = request.COOKIES.get("session")
        if not cookie:
            return _unauthenticated("No Superset session cookie was sent.")

        identity = _cache.get(cookie)
        if identity is None:
            try:
                identity = _verify_with_superset(cookie)
            except SupersetError as exc:
                if exc.is_permission_denied:
                    return _unauthenticated("Superset rejected this session.")
                logger.warning("Identity verification failed: %s", exc)
                return JsonResponse(
                    {
                        "error": "superset_unavailable",
                        "message": "Could not reach Superset to verify your session.",
                    },
                    status=503,
                )
            _cache.set(cookie, identity)

        request.superset_identity = identity
        request.superset_session_cookie = cookie
        return self.get_response(request)


def _verify_with_superset(cookie: str) -> SupersetIdentity:
    """Ask Superset who this session belongs to.

    Two calls, not one: `/api/v1/me/` carries no roles in Superset 6.1.0, and
    role information is needed for risk classification (spec §6.6).
    """
    client = SupersetClient(session_cookie=cookie)
    me = client.me()
    username = me.get("username")
    if not username or me.get("is_anonymous"):
        raise SupersetError("Anonymous session", status_code=401)

    try:
        raw_roles = client.my_roles()
        roles = list(raw_roles.keys()) if isinstance(raw_roles, dict) else []
    except SupersetError as exc:
        # Roles are policy input, not identity. A failure here shouldn't lock a
        # legitimate user out — degrade to "no known roles", which is the most
        # restrictive interpretation for risk classification.
        logger.warning("Could not fetch roles for %s: %s", username, exc)
        roles = []

    return SupersetIdentity(
        username=username,
        user_id=me.get("id"),
        first_name=me.get("first_name", ""),
        last_name=me.get("last_name", ""),
        email=me.get("email", ""),
        roles=roles,
    )


# --- Cross-origin protection ---------------------------------------------
#
# Every state-changing endpoint here is `@csrf_exempt`, and correctly so: this
# application has no Django session for Django's CSRF token to protect, and
# `django.contrib.sessions` is not even installed. Identity is the Superset
# session cookie, verified above.
#
# But that leaves a SameSite cookie as the only thing standing between a foreign
# page and a POST — and the surface keeps growing. `/api/action-result/` now
# *resumes agent turns*, so a forged POST to it does not merely corrupt an audit
# row: it makes the copilot continue building on an outcome nobody reported.
#
# Checking the origin here, where the identity is already being resolved, is a
# handful of lines and closes it for every endpoint at once.

_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _check_origin(request) -> JsonResponse | None:
    """Refuse a state-changing request that came from another origin.

    `Origin` is sent by every browser on a cross-origin request and on every
    same-origin POST in current browsers; `Referer` is the fallback for the
    handful of cases that omit it. **Neither header being present is allowed**,
    because that is what a same-origin `fetch` from an older browser and every
    non-browser client (curl, the test suite, the container healthcheck) look
    like — a header a browser controls cannot be *required*, only checked when
    offered.

    That is the honest bound on this control: it stops a cross-origin POST from
    a page the user is visiting, which is the actual threat. It is not a
    substitute for the session cookie, which is what authenticates.
    """
    stated = request.META.get("HTTP_ORIGIN") or ""
    if not stated:
        referer = request.META.get("HTTP_REFERER") or ""
        if not referer:
            return None
        stated = referer

    permitted = [request.build_absolute_uri("/")]
    # An allowlist for deployments where nginx terminates on a different host
    # from the one Django believes it is serving.
    permitted.extend(getattr(settings, "LIFEDATA_AI_ALLOWED_ORIGINS", []) or [])

    if any(_same_origin(stated, entry) for entry in permitted if entry):
        return None

    logger.warning(
        "Refused a cross-origin %s to %s from %s (expected one of %s)",
        request.method, request.path, stated, permitted,
    )
    return JsonResponse(
        {
            "error": "cross_origin",
            "message": (
                "This request came from another site and was refused. Use the copilot "
                "from within Superset."
            ),
        },
        status=403,
    )


def _same_origin(stated: str, expected: str) -> bool:
    """Whether two URLs name the same origin, tolerating a proxy-dropped port.

    Scheme and hostname must match exactly. **The port must match only when both
    sides state one**, and that concession is not laziness — it is the deployment
    reality this check was written against.

    nginx's `$host` variable drops the port. A config using it (which ours did,
    and which is the more common of the two spellings) makes Django compute its
    own origin as `http://localhost` while the browser correctly sends
    `Origin: http://localhost:8080` — so a strict comparison refuses the panel's
    own requests, which is exactly what happened the first time this ran in a
    real browser.

    What is given up is small and bounded: another service on a *different port
    of the same hostname* would be treated as same-origin. What is gained is that
    the control cannot be silently broken by a proxy header nobody thought about,
    which is the failure mode that turns a security check into a 403 someone
    disables in a hurry.
    """
    from urllib.parse import urlsplit

    a, b = urlsplit(stated), urlsplit(expected)
    if not (a.scheme and a.netloc and b.scheme and b.netloc):
        return False
    if a.scheme != b.scheme or a.hostname != b.hostname:
        return False
    if a.port is not None and b.port is not None:
        return a.port == b.port
    return True


def _unauthenticated(message: str) -> JsonResponse:
    return JsonResponse({"error": "unauthenticated", "message": message}, status=401)
