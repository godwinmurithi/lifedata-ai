"""Django settings for the LifeData AI Copilot orchestrator.

The orchestrator is a thin Django project whose only job is to host the
`lifedata_ai` app: the agent loop, the tool registry, the validator, and the
audit tables. It shares the `lifedata` Postgres database with the LifeHealth
operational tables and the flattened reporting views Superset reads.

It deliberately has no user-facing UI of its own — identity comes from the
Superset session cookie (spec §1), and the interface is the injected panel.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env", override=False)


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else value.strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "t", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = _env(name)
    return [p.strip() for p in raw.split(",") if p.strip()] if raw else list(default)


# --- Core -----------------------------------------------------------------

#: The default is deliberately named so it is obvious in a log or a diff.
_INSECURE_SECRET_KEY = "dev-insecure-orchestrator-key"

SECRET_KEY = _env("DJANGO_SECRET_KEY") or _INSECURE_SECRET_KEY
DEBUG = _env_bool("DJANGO_DEBUG", True)

# `or` rather than a default argument, because `_env` returns the default only
# when the variable is ABSENT. `DJANGO_SECRET_KEY=` — present and empty, which is
# exactly what a half-filled `.env` produces — would otherwise sail past both
# this check and Django's own, leaving every envelope signed with "".
if not DEBUG and SECRET_KEY == _INSECURE_SECRET_KEY:
    # REFUSE TO BOOT. Action envelopes are signed with this key
    # (`actions/envelopes.py`), and the default is published in this repository
    # — so a production-shaped deployment that forgot the environment variable
    # would sign every Superset write with a value anyone can read, and forging
    # an envelope would be trivial.
    #
    # A crash at startup is the only safe failure here. The alternative is a
    # deployment that looks completely healthy while its central integrity
    # control is worthless, and nothing in the running system would report it.
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY is still the development default while DEBUG is off. "
        "Action envelopes are signed with it, so this deployment would sign every "
        "write with a key published in the repository. Generate one with "
        "`python -c \"import secrets; print(secrets.token_urlsafe(64))\"` and set "
        "DJANGO_SECRET_KEY."
    )
ALLOWED_HOSTS = _env_list(
    "DJANGO_ALLOWED_HOSTS", ["localhost", "127.0.0.1", "host.docker.internal"]
)

#: Trust the reverse proxy's protocol header, when there is a reverse proxy
#: terminating TLS in front of us.
#:
#: OFF by default, because a deployment that trusts this header without a proxy
#: guaranteeing it lets a client claim its own request was HTTPS.
#:
#: It has to be ON wherever nginx terminates TLS, and the reason is not
#: cosmetic. `middleware._check_origin` compares the browser's `Origin` against
#: `request.build_absolute_uri("/")`. Behind TLS termination Django sees plain
#: HTTP and computes `http://host/`, while the browser correctly sends
#: `https://host` — the schemes differ, `_same_origin` requires them to match,
#: and EVERY state-changing request is refused with "This request came from
#: another site". Same class of failure as the `$host`/`$http_host` note in
#: deploy/nginx/lifedata.conf, one layer up.
if _env_bool("DJANGO_TRUST_PROXY_PROTO", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

ROOT_URLCONF = "lifedata.urls"
WSGI_APPLICATION = "lifedata.wsgi.application"
ASGI_APPLICATION = "lifedata.asgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "django.contrib.gis",
    "lifedata_ai",
]

# No AuthenticationMiddleware and no django.contrib.auth: identity is the
# Superset session, verified by our own middleware (spec §1). Adding Django auth
# here would create a second, conflicting notion of "who the user is".
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.gzip.GZipMiddleware",
    "django.middleware.common.CommonMiddleware",
    "lifedata_ai.middleware.SupersetIdentityMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    },
]


# --- Database -------------------------------------------------------------
# The same Postgres database that holds LifeHealth data and the reporting
# views. Superset reads those views through a separate read-only role.
#
# `DJANGO_DB_SCHEMA` exists for deployments that ADOPT an existing database
# rather than creating one — the sandbox.lifehealth.app install writes its
# orchestrator tables into a `lifedata_ai` schema alongside Superset's own
# `superset` schema in a database that was already there. Setting a search_path
# per connection is the right lever for that: `ALTER ROLE ... SET search_path`
# would be invisible in this repository and would follow the role into psql
# sessions nobody expected it to change.
#
# The role named here must be the schema's owner and must NOT be Superset's own
# database user. The orchestrator never writes to Superset — the user's browser
# does — and a role that could rewrite `superset.dashboards` directly would
# dissolve that property without a single line of code changing.

_DB_SCHEMA = _env("DJANGO_DB_SCHEMA", "")

DATABASES = {
    "default": {
        # PostGIS backend so geometry columns and spatial queries are available
        # to the ORM. Override with DJANGO_DB_ENGINE=django.db.backends.postgresql
        # to run outside the container, where GDAL/GEOS may not be installed.
        "ENGINE": _env("DJANGO_DB_ENGINE", "django.contrib.gis.db.backends.postgis"),
        "NAME": _env("POSTGRES_DB", "lifedata"),
        "USER": _env("POSTGRES_USER", "lifedata"),
        "PASSWORD": _env("POSTGRES_PASSWORD", ""),
        "HOST": _env("POSTGRES_HOST", "127.0.0.1"),
        "PORT": _env("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
        # The schema ALONE — `public` is deliberately not on the path.
        #
        # It was, briefly, on the theory that extensions and types installed
        # there should still resolve. In a database we share with other
        # applications that is actively dangerous: the staging host's `public`
        # already holds a `django_migrations` table belonging to GeoNode, so
        # Django resolved its own migration bookkeeping to somebody else's table
        # and died with `permission denied for table django_migrations` — which
        # is the *good* outcome. Had our role been able to read it, migrate
        # would have compared our migrations against GeoNode's history.
        #
        # Nothing is lost by leaving it off: built-in types live in pg_catalog,
        # which is always implicitly first, and these models declare no geometry
        # so no extension type is ever named. The result is that the ORM cannot
        # address another application's tables at all, whatever it is asked to
        # do — a stronger property than the grants alone provide.
        **({"OPTIONS": {"options": f"-c search_path={_DB_SCHEMA}"}} if _DB_SCHEMA else {}),
    }
}


# --- Static ---------------------------------------------------------------
# Serves the panel bundle at /lifedata-ai/static/panel/panel.js, which nginx
# injects into every Superset HTML response.

STATIC_URL = "/lifedata-ai/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

LANGUAGE_CODE = "en-gb"
TIME_ZONE = _env("DJANGO_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True


# --- LifeData AI Copilot --------------------------------------------------

#: Where the orchestrator reaches Superset for server-side read tools.
#: Every call forwards the end user's own session cookie, so reads are
#: permission-scoped to that user (spec §1 step 3).
LIFEDATA_AI_SUPERSET_INTERNAL_URL = _env(
    "LIFEDATA_AI_SUPERSET_INTERNAL_URL", "http://localhost:8080"
).rstrip("/")

#: Native Superset MCP service (superset/mcp_service/, Superset 6.1.0+),
#: reached container-to-container -- never exposed on the host or publicly.
#: Read tools call through here instead of raw REST when
#: LIFEDATA_AI_USE_MCP_READS is on. See lifedata_ai/superset_mcp_client.py.
LIFEDATA_AI_MCP_URL = _env(
    "LIFEDATA_AI_MCP_URL", "http://superset-staging-mcp4cti:5008/mcp/"
)
#: Shared HS256 secret with Superset's MCP_JWT_SECRET (superset-staging/.env).
#: Mints a short-lived per-user token per tool call -- see
#: superset-staging/mcp_auth_bridge.py for how Superset resolves it back to
#: a real Superset user (g.user), never a shared service account.
MCP_JWT_SECRET = _env("MCP_JWT_SECRET")
MCP_JWT_ISSUER = "lifedata-ai"
MCP_JWT_AUDIENCE = "superset-mcp"
#: Feature flag (hard rule 4): read tools use native MCP when true, the
#: original REST client (superset_client.py) when false. No redeploy needed
#: to flip -- just restart the django container with the new env value.
LIFEDATA_AI_USE_MCP_READS = _env_bool("LIFEDATA_AI_USE_MCP_READS", True)
#: Same idea, for the write path (create_chart, create_dashboard,
#: add_chart_to_dashboard only -- see tools/mcp_write_dispatch.py for why the
#: others aren't covered yet). When true, WriteTool.run() marks a proposal
#: execution_mode="mcp" and the panel confirms it through
#: /api/execute-mcp-action/ (server executes via MCP) instead of the panel
#: performing raw REST calls itself with the user's cookie. Full MCP
#: authorization for staging, per explicit instruction (2026-09-25) -- see
#: AI_NATIVE_STATUS.md.
LIFEDATA_AI_USE_MCP_WRITES = _env_bool("LIFEDATA_AI_USE_MCP_WRITES", True)

#: Phase 15 (plan's own spec): guardrails off for this sandbox, per explicit,
#: repeated instruction (2026-09-25) -- every dataset on this box is a fixture
#: the user created for this purpose, not real patient/production data. Two
#: things stay true regardless of this flag, matching the plan's own text
#: verbatim, not a reinterpretation of it:
#:   1. Refuses to turn on if the deployment doesn't look like this sandbox
#:      (DJANGO_ALLOWED_HOSTS must be exactly sandbox.lifehealth.app-shaped,
#:      never a production hostname) -- the environment gate, not an AI
#:      restriction.
#:   2. Saving a skill (Phase 18, not built yet) always needs explicit human
#:      approval even with this on -- the plan's one stated exception.
#: What this flag actually removes when true: the propose-then-click-confirm
#: step for MCP-executable writes (WriteTool.run() auto-executes instead of
#: waiting for the panel to POST a confirm). The semantics registry's own
#: dataset gate (Phase 4) has since been removed entirely, so there is no
#: longer a separate registry-based layer for this flag to additionally skip.
#: It does NOT introduce a service account -- every action still runs under
#: the calling user's own minted MCP identity, same invariant as always.
_PRODUCTION_HOST_MARKERS = ("lifedata.lifehealth.app",)


def _looks_like_production(allowed_hosts: list[str]) -> bool:
    return any(marker in host for host in allowed_hosts for marker in _PRODUCTION_HOST_MARKERS)


LIFEDATA_AI_DEV_MODE = _env_bool("LIFEDATA_AI_DEV_MODE", False) and not _looks_like_production(
    ALLOWED_HOSTS
)

#: Secrets come from the environment only, never from code (spec §9).
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
OPENAI_API_KEY = _env("OPENAI_API_KEY")
DEEPSEEK_API_KEY = _env("DEEPSEEK_API_KEY")
LIFEDATA_AI_DEEPSEEK_BASE_URL = _env("LIFEDATA_AI_DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")
LIFEDATA_AI_PROVIDER = _env("LIFEDATA_AI_PROVIDER", "anthropic").lower()
if LIFEDATA_AI_PROVIDER not in {"anthropic", "openai", "deepseek"}:
    raise ValueError("LIFEDATA_AI_PROVIDER must be anthropic, openai or deepseek")

#: The independent visual-review sub-call (quality.py:independent_visual_review)
#: defaults to a different provider than the main loop when unset — explicit
#: here so that choice is visible and overridable rather than a silent fallback.
LIFEDATA_AI_VISUAL_REVIEW_PROVIDER = _env("LIFEDATA_AI_VISUAL_REVIEW_PROVIDER", "").lower()
LIFEDATA_AI_VISUAL_REVIEW_MODEL = _env("LIFEDATA_AI_VISUAL_REVIEW_MODEL", "")

#: Model catalogue verified rather than assumed — the spec's suggested
#: `claude-sonnet-4-6` predates the current line-up.
LIFEDATA_AI_MODEL = _env(
    "LIFEDATA_AI_MODEL",
    {"openai": "gpt-5.3-codex", "deepseek": "deepseek-flash", "anthropic": "claude-opus-5"}[LIFEDATA_AI_PROVIDER],
)

#: Reasoning depth: low | medium | high | xhigh | max.
#:
#: `medium`, not `high`, and that is a measured choice rather than a cost one.
#: With tools attached, `high` was returning `overloaded_error` on essentially
#: every request while `medium` succeeded at every max_tokens we tried
#: (bisected: no tools + high is fine; 13 tools + high fails; 25 tools + medium
#: at 32k is fine). Since a dashboard build needs several consecutive rounds,
#: one overload anywhere in the chain aborts the whole thing — so `high` made
#: multi-step work effectively impossible. Revisit if capacity changes.
LIFEDATA_AI_EFFORT = _env("LIFEDATA_AI_EFFORT", "medium")

#: Hard stops on the agent loop (spec §5).
LIFEDATA_AI_MAX_TOOL_ROUNDS = _env_int("LIFEDATA_AI_MAX_TOOL_ROUNDS", 12)
#: The spec suggested 60s, which is fine for a question but not for work.
#: Building a dashboard is inspect -> create chart -> create chart -> create
#: dashboard, with a browser round trip per write; 60s cut that off mid-way
#: every time. Raised deliberately, with the tool-round cap still bounding it.
#:
#: This budget bounds MODEL work. Time the loop spends awaiting the browser is
#: measured and added back to the deadline (see `_await_action_outcome`) —
#: without that, nine round trips could hand the SDK a three-second timeout
#: mid-build, which surfaced as a provider timeout that looked like an Anthropic
#: fault. Turn timeouts would then track network quality, which is the worst
#: possible failure distribution for a pilot on a Ugandan facility connection.
LIFEDATA_AI_TURN_TIMEOUT_SECONDS = _env_int("LIFEDATA_AI_TURN_TIMEOUT_SECONDS", 300)

#: How long the loop waits for the browser to execute an AUTO-APPLIED action and
#: report back. This is what lets the model chain writes — it learns the new
#: chart's real id and can put it on a dashboard.
#:
#: Stays at 30. The only wait that ever depended on human reaction time was the
#: preview card, and that no longer blocks: anything the user must approve ends
#: the turn and resumes when they act, which no timeout could have made safe
#: anyway. What is left behind this setting is a browser answering in a couple of
#: seconds, where 30s is already generous.
LIFEDATA_AI_ACTION_WAIT_SECONDS = _env_int("LIFEDATA_AI_ACTION_WAIT_SECONDS", 30)
#: The spec suggested 3. That is below the cost of the product's headline
#: request: without layout recipes a dashboard is ~5 create_chart + 1
#: create_dashboard + 1 filter + 1-2 layout operations, so about nine write
#: actions. Nine against a cap of ten is uncomfortably close to a limit whose
#: overshoot behaviour is a half-built dashboard.
#:
#: Note this bounds a TURN, not a dashboard. A previewed build spans two turns —
#: the proposal ends one and the Apply resumes the next — so `writes_proposed`
#: resets across the Apply. `review_dashboard_design` is what catches a partial
#: result, not this number.
LIFEDATA_AI_MAX_WRITES_PER_TURN = _env_int("LIFEDATA_AI_MAX_WRITES_PER_TURN", 14)

#: Signed action envelopes expire quickly so a captured envelope is not
#: replayable (spec §5).
LIFEDATA_AI_ENVELOPE_TTL_SECONDS = _env_int("LIFEDATA_AI_ENVELOPE_TTL_SECONDS", 300)

#: Whether to offer `list_forms` / `get_form_schema` at all.
#:
#: They read the dynamic-form metadata tables that `manage.py seed_lifehealth`
#: creates. A deployment that adopts an existing Superset has neither, so the
#: tools fail at call time telling the operator to run a seed command that would
#: be wrong to run there. Turn them off and the model never offers form analysis
#: it cannot deliver.
LIFEDATA_AI_ENABLE_FORM_TOOLS = _env_bool("LIFEDATA_AI_ENABLE_FORM_TOOLS", True)

#: Row caps applied before any query result is shown to the model (spec §5).
LIFEDATA_AI_MAX_QUERY_ROW_LIMIT = 200
LIFEDATA_AI_MAX_ROWS_TO_MODEL = 50
LIFEDATA_AI_MAX_COLUMNS_TO_MODEL = 20

#: httpx timeouts on every Superset call (spec §9 cross-phase rules).
LIFEDATA_AI_SUPERSET_CONNECT_TIMEOUT = 5.0
LIFEDATA_AI_SUPERSET_READ_TIMEOUT = 30.0

#: Identity verification result is cached per session for this long (spec §1).
LIFEDATA_AI_IDENTITY_CACHE_SECONDS = 60

#: Extra origins permitted to make state-changing requests, beyond the one
#: Django computes for itself.
#:
#: Every write endpoint is `@csrf_exempt` — correctly, because there is no Django
#: session for Django's CSRF token to protect — which leaves a SameSite cookie as
#: the only thing between a foreign page and a POST. `SupersetIdentityMiddleware`
#: therefore checks `Origin`/`Referer` on POST/PUT/PATCH/DELETE.
#:
#: Normally empty: the panel is served same-origin through nginx, which is the
#: whole point of the injection approach. Set this only where nginx terminates on
#: a different host from the one Django believes it is serving.
LIFEDATA_AI_ALLOWED_ORIGINS = _env_list("LIFEDATA_AI_ALLOWED_ORIGINS", [])

# Browser review runs against the same configured public application origin.
# It forwards only the requesting viewer's session; never use a service account.
LIFEDATA_AI_BROWSER_BASE_URL = _env(
    "LIFEDATA_AI_BROWSER_BASE_URL",
    LIFEDATA_AI_ALLOWED_ORIGINS[0] if LIFEDATA_AI_ALLOWED_ORIGINS else "http://localhost:8080",
)
LIFEDATA_AI_QUALITY_REPAIR_LIMIT = _env_int("LIFEDATA_AI_QUALITY_REPAIR_LIMIT", 2)


# --- Logging --------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "{levelname} {name}: {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"lifedata_ai": {"level": "DEBUG" if DEBUG else "INFO", "propagate": True}},
}
