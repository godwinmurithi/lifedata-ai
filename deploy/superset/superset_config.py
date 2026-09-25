"""Superset configuration for the LifeData AI Copilot POC.

This is a *config file only* — the Superset image is unmodified (spec §0).
Everything here is either required to run Superset against our Postgres/Redis,
or is one of the §10 integration points the spec says to verify and record
against the pinned version (6.1.0).
"""

import os

# --- Core -----------------------------------------------------------------

SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]

_PG_USER = os.environ.get("POSTGRES_USER", "lifedata")
_PG_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "lifedata-dev-password")

# Superset's own Flask-AppBuilder metadata database.
SQLALCHEMY_DATABASE_URI = f"postgresql+psycopg2://{_PG_USER}:{_PG_PASSWORD}@postgres:5432/superset"
SQLALCHEMY_TRACK_MODIFICATIONS = False

# Running behind nginx: trust X-Forwarded-* so redirects and cookies use the
# browser-visible scheme/host rather than the container's.
ENABLE_PROXY_FIX = True


# --- Authentication (spec §0: database auth, no SSO) ----------------------

from flask_appbuilder.security.manager import AUTH_DB  # noqa: E402

AUTH_TYPE = AUTH_DB
AUTH_USER_REGISTRATION = False

# The orchestrator bridges identity by calling GET /api/v1/me/ with the
# forwarded session cookie (spec §1). Nothing extra is needed here — but the
# session cookie must be readable across the whole origin, including
# /lifedata-ai/*, which SameSite=Lax on path "/" already gives us.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
# Local development is plain HTTP on localhost. Set to True behind real TLS.
SESSION_COOKIE_SECURE = False


# --- CSRF (spec §10: verify CSRF endpoint behaviour) ----------------------

WTF_CSRF_ENABLED = True
# The panel fetches a token from GET /api/v1/security/csrf_token/ and sends it
# as X-CSRFToken. That endpoint must stay CSRF-exempt or the panel can never
# bootstrap a token.
WTF_CSRF_EXEMPT_LIST = [
    "superset.views.core.log",
    "superset.views.core.explore_json",
    "superset.charts.data.api.data",
]
# Long-lived so a panel session doesn't need to re-fetch mid-conversation.
WTF_CSRF_TIME_LIMIT = 60 * 60 * 24


# --- Content Security Policy (spec §3: Talisman may block the panel) ------

# Superset 6 ships Talisman with a restrictive CSP. The panel is served from the
# SAME origin (/lifedata-ai/static/panel/panel.js) via nginx, so 'self' covers
# the script — no external host needs allowing, which is exactly why the
# nginx-injection design keeps the CSP story simple.
TALISMAN_ENABLED = True
TALISMAN_CONFIG = {
    "content_security_policy": {
        "base-uri": ["'self'"],
        "default-src": ["'self'"],
        "img-src": ["'self'", "blob:", "data:"],
        "worker-src": ["'self'", "blob:"],
        "connect-src": [
            "'self'",
            # Superset's own telemetry/geo endpoints.
            "https://api.mapbox.com",
            "https://events.mapbox.com",
        ],
        "object-src": "'none'",
        "style-src": ["'self'", "'unsafe-inline'"],
        "script-src": ["'self'", "'unsafe-inline'", "'unsafe-eval'"],
        # The panel opens an SSE stream to /lifedata-ai/api/chat/ — same origin,
        # already covered by connect-src 'self'.
    },
    "content_security_policy_nonce_in": ["script-src"],
    "force_https": False,  # local HTTP development
    "session_cookie_secure": False,
}
CONTENT_SECURITY_POLICY_WARNING = False


# --- Caching / async ------------------------------------------------------

_REDIS = {"host": "redis", "port": 6379}

CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 300,
    "CACHE_KEY_PREFIX": "superset_",
    "CACHE_REDIS_HOST": _REDIS["host"],
    "CACHE_REDIS_PORT": _REDIS["port"],
    "CACHE_REDIS_DB": 1,
}
DATA_CACHE_CONFIG = {**CACHE_CONFIG, "CACHE_REDIS_DB": 2, "CACHE_DEFAULT_TIMEOUT": 600}
FILTER_STATE_CACHE_CONFIG = {**CACHE_CONFIG, "CACHE_REDIS_DB": 3}
EXPLORE_FORM_DATA_CACHE_CONFIG = {**CACHE_CONFIG, "CACHE_REDIS_DB": 4}


# --- Feature flags --------------------------------------------------------

FEATURE_FLAGS = {
    # Native dashboard filters — the copilot's add_native_filter action writes
    # json_metadata.native_filter_configuration (spec §5).
    "DASHBOARD_NATIVE_FILTERS": True,
    "DASHBOARD_CROSS_FILTERS": True,
    # Permalink state, used by the panel's page-context collector (spec §7).
    "ENABLE_EXPLORE_DRAG_AND_DROP": True,
    "HORIZONTAL_FILTER_BAR": True,
    # Superset 6 theming: the copilot's apply_dashboard_theme uses theme objects
    # rather than raw CSS (spec §0, §6.5).
    "ALERT_REPORTS": False,
    "EMBEDDED_SUPERSET": False,
}

# Row limits — the orchestrator additionally caps get_chart_data at 200 rows
# server-side (spec §5), but a global ceiling is defence in depth.
ROW_LIMIT = 5000
SAMPLES_ROW_LIMIT = 1000
SQL_MAX_ROW = 10000

# SQL Lab is not needed for the POC and widens the surface area the copilot
# could theoretically be pointed at.
SQLLAB_CTAS_NO_LIMIT = False
