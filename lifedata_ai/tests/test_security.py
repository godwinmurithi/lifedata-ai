"""The controls that protect the deployment rather than the data (plan §9.5).

Three small things, each deferred through several planning cycles because each
is only a handful of lines and none of them shows up in a demo. They are tested
here so the decision to have them is explicit and cannot quietly lapse.
"""

from __future__ import annotations

import json

import pytest
from django.test import Client

from lifedata_ai import middleware
from lifedata_ai.middleware import SupersetIdentity
from lifedata_ai.superset_client import SupersetError

pytestmark = pytest.mark.django_db

SESSION_PREFIX = "superset-session-for-"


@pytest.fixture(autouse=True)
def stub_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    def _verify(cookie: str) -> SupersetIdentity:
        if not cookie.startswith(SESSION_PREFIX):
            raise SupersetError("Superset rejected this session", status_code=401)
        # `user_id` matters: Superset identifies an owner by numeric id and
        # sends no `username`, so an identity without one can never be
        # recognised as an owner. 7 matches the `identity` fixture and the
        # owner ids in the Superset fixtures below.
        return SupersetIdentity(
            username=cookie[len(SESSION_PREFIX):],
            user_id=7,
            email=f"{cookie[len(SESSION_PREFIX):]}@example.test",
            roles=["Alpha"],
        )

    monkeypatch.setattr(middleware, "_cache", middleware._IdentityCache())
    monkeypatch.setattr(middleware, "_verify_with_superset", _verify)


@pytest.fixture
def signed_in() -> Client:
    client = Client()
    client.cookies["session"] = f"{SESSION_PREFIX}analyst"
    return client


# --- The signing key ------------------------------------------------------


class TestTheSigningKey:
    """A production-shaped deployment must not sign with the repo's own key.

    `SECRET_KEY` defaults to `"dev-insecure-orchestrator-key"` and `DEBUG`
    defaults to True, so forgetting one environment variable would produce a
    deployment that looks completely healthy while every action envelope is
    signed with a value published in this repository — and forging one would be
    trivial. Nothing in the running system would report it.
    """

    @pytest.mark.parametrize(
        ("secret_key", "why"),
        [
            (None, "the variable is absent"),
            # Present and EMPTY is what a half-filled `.env` produces, and it
            # used to sail past this check *and* Django's own — leaving every
            # envelope signed with "".
            ("", "the variable is present but empty"),
            ("dev-insecure-orchestrator-key", "the repo's own default was copied"),
        ],
    )
    def test_an_insecure_key_is_refused_when_debug_is_off(
        self, secret_key: str | None, why: str
    ) -> None:
        from django.core.exceptions import ImproperlyConfigured

        with pytest.raises(ImproperlyConfigured, match="DJANGO_SECRET_KEY") as caught:
            _exec_settings(debug="false", secret_key=secret_key)

        # The message must name the fix, not merely the fault: whoever hits this
        # is mid-deploy and needs the command, not a diagnosis.
        assert "token_urlsafe" in str(caught.value), why

    def test_a_real_key_boots_fine_with_debug_off(self) -> None:
        namespace = _exec_settings(debug="false", secret_key="a-real-secret-key")

        assert namespace["DEBUG"] is False
        assert namespace["SECRET_KEY"] == "a-real-secret-key"

    def test_development_is_unaffected(self) -> None:
        """The default key is exactly right when DEBUG is on."""
        namespace = _exec_settings(debug="true", secret_key=None)

        assert namespace["SECRET_KEY"] == "dev-insecure-orchestrator-key"


def _exec_settings(*, debug: str, secret_key: str | None) -> dict:
    """Execute settings.py in a throwaway namespace with a patched environment.

    Re-executed rather than reimported: Django caches the settings module and the
    check under test runs at import time, so a reimport would be a no-op. The
    real `.env` is neutralised too, or the developer's own key would satisfy the
    check and the test would pass for the wrong reason.
    """
    import os
    from pathlib import Path
    from unittest import mock

    import lifedata.settings as settings_module

    source = Path(settings_module.__file__).read_text(encoding="utf-8")
    namespace = {
        "__name__": "lifedata.settings_probe",
        "__file__": settings_module.__file__,
    }

    env = {"DJANGO_DEBUG": debug}
    if secret_key is not None:
        env["DJANGO_SECRET_KEY"] = secret_key

    with mock.patch.dict(os.environ, env, clear=False):
        if secret_key is None:
            os.environ.pop("DJANGO_SECRET_KEY", None)
        # `load_dotenv` would otherwise put the developer's own key back.
        with mock.patch("dotenv.load_dotenv"):
            exec(compile(source, "<settings-probe>", "exec"), namespace)
    return namespace


# --- Cross-origin protection ---------------------------------------------


class TestCrossOriginProtection:
    """Every write endpoint is `@csrf_exempt`, and correctly so.

    This application has no Django session for Django's CSRF token to protect —
    `django.contrib.sessions` is not even installed. That leaves a SameSite
    cookie as the only thing between a foreign page and a POST, and the surface
    keeps growing: `/api/action-result/` now *resumes agent turns*, so a forged
    POST does not merely corrupt an audit row, it makes the copilot continue
    building on an outcome nobody reported.
    """

    def test_a_cross_origin_post_is_refused(self, signed_in: Client) -> None:
        response = signed_in.post(
            "/lifedata-ai/api/action-result/",
            data=json.dumps({"action_id": "x", "status": "executed"}),
            content_type="application/json",
            HTTP_ORIGIN="https://evil.example",
        )

        assert response.status_code == 403
        assert response.json()["error"] == "cross_origin"

    def test_a_same_origin_post_passes_through(self, signed_in: Client) -> None:
        response = signed_in.post(
            "/lifedata-ai/api/action-result/",
            data=json.dumps({"action_id": "x", "status": "executed"}),
            content_type="application/json",
            HTTP_ORIGIN="http://testserver",
        )

        # 404 because the action does not exist — which is the point: the
        # request reached the view.
        assert response.status_code == 404

    def test_a_cross_origin_referer_is_refused_when_origin_is_absent(
        self, signed_in: Client
    ) -> None:
        response = signed_in.post(
            "/lifedata-ai/api/action-result/",
            data=json.dumps({"action_id": "x", "status": "executed"}),
            content_type="application/json",
            HTTP_REFERER="https://evil.example/page",
        )

        assert response.status_code == 403

    def test_no_headers_at_all_is_allowed(self, signed_in: Client) -> None:
        """The honest bound on this control, asserted rather than left implicit.

        A same-origin `fetch` from an older browser and every non-browser client
        — curl, this test suite, the container healthcheck — send neither header.
        A header the browser controls can be *checked* when offered; it cannot be
        *required*. The session cookie is what authenticates.
        """
        response = signed_in.post(
            "/lifedata-ai/api/action-result/",
            data=json.dumps({"action_id": "x", "status": "executed"}),
            content_type="application/json",
        )

        assert response.status_code == 404

    def test_a_cross_origin_GET_is_untouched(self, signed_in: Client) -> None:
        """Reads change nothing, and blocking them would break nothing useful."""
        response = signed_in.get(
            "/lifedata-ai/api/conversations/", HTTP_ORIGIN="https://evil.example"
        )

        assert response.status_code == 200

    def test_an_explicitly_allowed_origin_passes(
        self, signed_in: Client, settings
    ) -> None:
        """For deployments where nginx terminates on another host."""
        settings.LIFEDATA_AI_ALLOWED_ORIGINS = ["https://superset.example"]

        response = signed_in.post(
            "/lifedata-ai/api/action-result/",
            data=json.dumps({"action_id": "x", "status": "executed"}),
            content_type="application/json",
            HTTP_ORIGIN="https://superset.example",
        )

        assert response.status_code == 404

    def test_the_chat_endpoint_is_protected_too(self, signed_in: Client) -> None:
        """Not only action-result: a forged turn spends money and writes rows."""
        response = signed_in.post(
            "/lifedata-ai/api/chat/",
            data=json.dumps({"message": "hello"}),
            content_type="application/json",
            HTTP_ORIGIN="https://evil.example",
        )

        assert response.status_code == 403


# --- Liveness -------------------------------------------------------------


class TestHealth:
    def test_liveness_does_not_depend_on_postgis(self, client: Client) -> None:
        """This probe used to run `SELECT postgis_version()` and 503 without it.

        The orchestrator's own models declare no geometry columns — the spatial
        work lives in the reporting views — so the container reported itself
        unhealthy while being entirely healthy, including against the plain
        `postgres:16` that CI deliberately uses. The first person to hit that
        burns an hour on a red herring.
        """
        response = client.get("/lifedata-ai/api/health/")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["database"] == "ok"
        # PostGIS is reported as a detail, and its absence is not a verdict.
        assert "postgis" in body

    def test_health_is_public(self, client: Client) -> None:
        """The container healthcheck has no Superset session."""
        assert client.get("/lifedata-ai/api/health/").status_code == 200


class TestTheProxyPortHazard:
    """The bug that only a real browser found.

    nginx's `$host` drops the port, so Django computed its own origin as
    `http://localhost` while the browser correctly sent
    `Origin: http://localhost:8080` — and the check refused every request the
    panel made. Every chat turn returned "This request came from another site".

    Nothing in the unit suite could have caught it: there is no nginx in it, and
    Django's test client makes `build_absolute_uri` and the Origin header agree
    by construction. These tests encode the asymmetry directly.
    """

    def test_a_port_the_proxy_dropped_is_tolerated(self, signed_in: Client) -> None:
        response = signed_in.post(
            "/lifedata-ai/api/action-result/",
            data=json.dumps({"action_id": "x", "status": "executed"}),
            content_type="application/json",
            # Django sees `testserver` (no port); the browser states one.
            HTTP_ORIGIN="http://testserver:8080",
        )

        assert response.status_code == 404, "the panel's own request was refused"

    def test_a_different_hostname_is_still_refused(self, signed_in: Client) -> None:
        """The concession is about ports, not hosts."""
        response = signed_in.post(
            "/lifedata-ai/api/action-result/",
            data=json.dumps({"action_id": "x", "status": "executed"}),
            content_type="application/json",
            HTTP_ORIGIN="http://evil.example:8080",
        )

        assert response.status_code == 403

    def test_a_different_scheme_is_still_refused(self, signed_in: Client) -> None:
        response = signed_in.post(
            "/lifedata-ai/api/action-result/",
            data=json.dumps({"action_id": "x", "status": "executed"}),
            content_type="application/json",
            HTTP_ORIGIN="https://testserver",
        )

        assert response.status_code == 403

    def test_the_nginx_config_preserves_the_port(self) -> None:
        """Belt and braces: fix the proxy too, not only the tolerance.

        `$http_host` keeps the port, so `build_absolute_uri` is correct for
        everything else Django generates — not just this check.
        """
        from pathlib import Path

        conf = Path(__file__).resolve().parents[2] / "deploy" / "nginx" / "lifedata.conf"
        orchestrator = conf.read_text(encoding="utf-8").split("location /lifedata-ai/")[1]
        orchestrator = orchestrator.split("location /")[0]

        assert "proxy_set_header Host $http_host;" in orchestrator
        assert "proxy_set_header Host $host;" not in orchestrator
