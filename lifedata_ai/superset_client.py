"""httpx wrapper around the Superset REST API (spec §2).

Every call forwards the end user's own `session` cookie, so reads are
permission-scoped to that user exactly as if they had made the request in the
browser. The orchestrator holds no Superset credentials of its own.

Timeouts are mandatory on every call (spec §9): connect 5s, read 30s.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


class SupersetError(Exception):
    """A Superset call failed. Carries the status so callers can surface 403s."""

    def __init__(self, message: str, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body

    @property
    def is_permission_denied(self) -> bool:
        return self.status_code in (401, 403)


class SupersetUnauthenticated(SupersetError):
    """No valid Superset session — the caller is not logged in."""


class SupersetClient:
    """Thin, synchronous Superset API client bound to one user's session."""

    def __init__(self, session_cookie: str | None, base_url: str | None = None):
        self.base_url = (base_url or settings.LIFEDATA_AI_SUPERSET_INTERNAL_URL).rstrip("/")
        self.session_cookie = session_cookie
        self._timeout = httpx.Timeout(
            connect=settings.LIFEDATA_AI_SUPERSET_CONNECT_TIMEOUT,
            read=settings.LIFEDATA_AI_SUPERSET_READ_TIMEOUT,
            write=settings.LIFEDATA_AI_SUPERSET_READ_TIMEOUT,
            pool=settings.LIFEDATA_AI_SUPERSET_CONNECT_TIMEOUT,
        )

    # -- plumbing ---------------------------------------------------------

    def _cookies(self) -> dict[str, str]:
        return {"session": self.session_cookie} if self.session_cookie else {}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=self._timeout, cookies=self._cookies()) as client:
                response = client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise SupersetError(f"Superset timed out: {method} {path}") from exc
        except httpx.HTTPError as exc:
            raise SupersetError(f"Superset unreachable: {exc}") from exc

        if response.status_code == 401:
            raise SupersetUnauthenticated(
                "Not authenticated to Superset", status_code=401
            )
        if response.status_code >= 400:
            # Surface Superset's own error rather than swallowing it — a viewer
            # hitting a 403 on save must be told exactly that (spec Phase 2).
            body: Any
            try:
                body = response.json()
            except ValueError:
                body = response.text[:500]
            raise SupersetError(
                f"Superset {method} {path} failed with {response.status_code}",
                status_code=response.status_code,
                body=body,
            )

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def get(self, path: str, params: dict | None = None) -> Any:
        return self._request("GET", path, params=params)

    # -- identity ---------------------------------------------------------

    def me(self) -> dict:
        """The logged-in user.

        NOTE (verified against Superset 6.1.0): this payload contains only
        `id, username, email, first_name, last_name, is_active, is_anonymous,
        login_count`. It does NOT include roles, despite what the spec implies —
        use `my_roles()` for those.
        """
        return (self.get("/api/v1/me/") or {}).get("result", {})

    def my_roles(self) -> dict:
        """Roles and permissions for the logged-in user.

        A separate endpoint from `/api/v1/me/` — see the note in `me()`.
        """
        result = (self.get("/api/v1/me/roles/") or {}).get("result", {})
        # Superset returns {"Admin": [[perm, view], ...]}; the role names are
        # what policy cares about, the permission pairs are noise for our use.
        if isinstance(result, dict) and "roles" in result:
            return result["roles"]
        return result

    def csrf_token(self) -> str:
        """Fetch a CSRF token for this session.

        Requires authentication (verified: 401 when anonymous, 200 when logged
        in). Only used for diagnostics server-side — the panel fetches its own.
        """
        return (self.get("/api/v1/security/csrf_token/") or {}).get("result", "")

    # -- read tools -------------------------------------------------------

    def list_databases(self, q: str | None = None) -> list[dict]:
        return self._listing("/api/v1/database/", q, ["database_name", "backend"])

    def list_datasets(self, q: str | None = None) -> list[dict]:
        return self._listing("/api/v1/dataset/", q, ["table_name", "schema", "kind"])

    def list_charts(self, q: str | None = None) -> list[dict]:
        return self._listing("/api/v1/chart/", q, ["slice_name", "viz_type"])

    def list_dashboards(self, q: str | None = None) -> list[dict]:
        return self._listing("/api/v1/dashboard/", q, ["dashboard_title", "status"])

    def _listing(self, path: str, q: str | None, columns: list[str]) -> list[dict]:
        rison = {"columns": ["id", *columns], "page_size": 100}
        if q:
            rison["filters"] = [
                {"col": columns[0], "opr": "ct", "value": q}
            ]
        payload = self.get(path, params={"q": _to_rison(rison)}) or {}
        return payload.get("result", [])

    def get_dataset(self, dataset_id: int) -> dict:
        return (self.get(f"/api/v1/dataset/{dataset_id}") or {}).get("result", {})

    def get_chart(self, chart_id: int) -> dict:
        return (self.get(f"/api/v1/chart/{chart_id}") or {}).get("result", {})

    def get_dashboard(self, dashboard_id: int | str) -> dict:
        """Full dashboard record.

        Includes `owners` (needed for the un-owned-dashboard preview rule) and
        `changed_on` (needed for the undo staleness check). NOTE: on a freshly
        created dashboard, `position_json` and `json_metadata` are NULL — not
        `{}` — so callers must handle None.
        """
        return (self.get(f"/api/v1/dashboard/{dashboard_id}") or {}).get("result", {})

    def get_dashboard_charts(self, dashboard_id: int | str) -> list[dict]:
        return (self.get(f"/api/v1/dashboard/{dashboard_id}/charts") or {}).get("result", [])

    def chart_data(self, query_context: dict) -> dict:
        """Run a query context through `POST /api/v1/chart/data`.

        This is a *read*: it executes an already-saved chart's stored query
        context. It is a POST only because Superset takes the query context in
        the body, so no CSRF token is involved and the spec §1 rule that writes
        never execute server-side is untouched.

        NOTE (verified against Superset 6.1.0): a malformed or empty body comes
        back as **500**, not 400. Callers must validate the query context before
        calling and read a 500 here as "we sent rubbish", not "Superset is down"
        — see docs/superset-integration-findings.md §3.
        """
        return self._request("POST", "/api/v1/chart/data", json=query_context) or {}


def _to_rison(value: Any) -> str:
    """Minimal Rison encoder for Superset's `q=` query parameter.

    Superset accepts JSON here in practice, but Rison is the documented format
    and avoids ambiguity with nested filters.
    """
    import json

    return json.dumps(value, separators=(",", ":"))
