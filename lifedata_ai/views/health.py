"""Health and identity endpoints."""

from __future__ import annotations

from django.conf import settings
from django.db import connection
from django.http import HttpRequest, JsonResponse

from lifedata_ai.superset_client import SupersetClient, SupersetError


def health(request: HttpRequest) -> JsonResponse:
    """Liveness check. Public — the container's healthcheck calls it.

    **Liveness is `SELECT 1`, and PostGIS is reported as an optional detail.**
    This probe used to run `SELECT postgis_version()` and return 503 with
    `status: "degraded"` on any database without the extension — including the
    plain `postgres:16` that CI deliberately uses, because the orchestrator's own
    models declare no geometry columns and the spatial work lives in the
    reporting views.

    So the container reported itself unhealthy while being entirely healthy, and
    the first person to run it against plain Postgres burns an hour on a red
    herring. What this endpoint must answer is "can this process serve
    requests?", and the answer does not depend on PostGIS.
    """
    database_ok, database_detail = True, "ok"
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:  # pragma: no cover - only on a broken environment
        database_ok, database_detail = False, str(exc)[:200]

    payload = {
        "status": "ok" if database_ok else "degraded",
        "service": "lifedata-ai-orchestrator",
        "database": database_detail,
        # A detail, not a verdict. Absent on plain Postgres, which is fine.
        "postgis": _postgis_version() if database_ok else None,
        "superset_url": settings.LIFEDATA_AI_SUPERSET_INTERNAL_URL,
        "model": settings.LIFEDATA_AI_MODEL,
        "provider": settings.LIFEDATA_AI_PROVIDER,
        "model_key_configured": bool(
            settings.OPENAI_API_KEY
            if settings.LIFEDATA_AI_PROVIDER == "openai"
            else settings.ANTHROPIC_API_KEY
        ),
        "openai_key_configured": bool(settings.OPENAI_API_KEY),
        "anthropic_key_configured": bool(settings.ANTHROPIC_API_KEY),
    }
    return JsonResponse(payload, status=200 if database_ok else 503)


def _postgis_version() -> str | None:
    """The PostGIS version, or None where the extension is not installed.

    Wrapped in `atomic()` because PostgreSQL aborts the whole transaction on a
    failed statement: without a savepoint, probing for a missing extension would
    leave the connection refusing every subsequent command in the request.
    """
    from django.db import transaction

    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT postgis_version()")
            return str(cursor.fetchone()[0])
    except Exception:
        return None


def whoami(request: HttpRequest) -> JsonResponse:
    """Echo the verified Superset identity.

    Requires a valid session — the middleware rejects anonymous callers before
    this runs, which is the Phase 1 acceptance criterion.
    """
    identity = request.superset_identity
    return JsonResponse(
        {
            "username": identity.username,
            "display_name": identity.display_name,
            "email": identity.email,
            "roles": identity.roles,
            "is_admin": identity.is_admin,
        }
    )


def superset_diagnostics(request: HttpRequest) -> JsonResponse:
    """Prove the orchestrator can reach Superset *as the calling user*.

    Useful during setup: if this returns counts, cookie passthrough works.
    """
    client = SupersetClient(session_cookie=request.superset_session_cookie)
    try:
        return JsonResponse(
            {
                "as_user": request.superset_identity.username,
                "databases": len(client.list_databases()),
                "datasets": len(client.list_datasets()),
                "charts": len(client.list_charts()),
                "dashboards": len(client.list_dashboards()),
                "csrf_token_obtainable": bool(client.csrf_token()),
            }
        )
    except SupersetError as exc:
        return JsonResponse(
            {"error": "superset_error", "message": str(exc), "detail": exc.body},
            status=502,
        )
