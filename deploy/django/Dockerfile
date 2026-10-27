# LifeData AI Copilot — orchestrator image.
#
# Runs the Django app that hosts the agent loop, tool registry, validator and
# audit tables. Includes GDAL/GEOS/PROJ so GeoDjango can talk to PostGIS: the
# spec's geography policy (§6.4) is about real spatial data — district-level
# aggregation for patient geography, exact coordinates for facilities — and
# enforcing that in the database beats enforcing it only in Python.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# GeoDjango's native dependencies, plus curl for the healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        binutils \
        gdal-bin \
        libgdal-dev \
        libgeos-dev \
        libproj-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer first so application edits don't invalidate it.
# `[tool.uv] package = false` means this installs dependencies only.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Authenticated dashboard review is part of the production build lifecycle.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN .venv/bin/python -m playwright install --with-deps chromium

COPY . .

EXPOSE 8000

# Dev server: autoreload on, so editing files on the host is picked up live
# through the bind mount.
#
# SWAPPING FOR GUNICORN — read this first.
#
# A chat turn is a long-lived streaming response. Building a dashboard takes
# several model rounds and a browser round trip per write, and
# LIFEDATA_AI_TURN_TIMEOUT_SECONDS is 300. `runserver` has no worker timeout, so
# that works today.
#
# Gunicorn's default sync-worker timeout is 30 SECONDS. Swapping to a default
# gunicorn command would kill EVERY build turn — not only the slow ones — and
# the failure surfaces to the user as a truncated stream with no error, which is
# indistinguishable from a dropped connection. Whoever makes that change must:
#
#     gunicorn lifedata.wsgi:application \
#         --bind 0.0.0.0:8000 \
#         --timeout 300 \            # at least LIFEDATA_AI_TURN_TIMEOUT_SECONDS
#         --graceful-timeout 30 \
#         --worker-class gthread --threads 8
#
# A threaded or async worker class matters as much as the timeout: a sync worker
# is occupied for the whole of a streaming turn, so N workers cap the deployment
# at N concurrent conversations.
#
# nginx is already correct for this — deploy/nginx/lifedata.conf sets
# `proxy_read_timeout 300s` explicitly on /lifedata-ai/ and on / — so gunicorn
# is the only piece of the chain that would need changing.
CMD ["uv", "run", "--no-sync", "python", "manage.py", "runserver", "0.0.0.0:8000"]
