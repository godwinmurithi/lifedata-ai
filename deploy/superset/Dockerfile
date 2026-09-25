# The ONLY modification made to Superset — database drivers.
#
# Spec §0 says the Superset image must remain unmodified: "no forks, no patched
# images". That rule exists to stop us patching Superset's *behaviour*. The lean
# apache/superset:6.1.0 image ships with no Postgres driver at all (verified:
# psycopg2, psycopg, pg8000 and asyncpg are all absent), so it cannot reach its
# own metadata database, let alone the LifeHealth analytics database.
#
# Installing a driver is the extension path Superset's own documentation
# prescribes. No Superset source is forked, patched or overridden here — this
# layer adds Python packages and nothing else. Superset's behaviour is still
# configured entirely through superset_config.py.
#
# Keep this file boring. If it ever grows a COPY of Superset source, a patch, or
# an overridden entrypoint, the spec's constraint has been broken.

FROM apache/superset:6.1.0

# Superset's image runs as the unprivileged `superset` user; pip needs root.
USER root

# Superset runs out of a uv-managed virtualenv at /app/.venv. Two traps here,
# both verified against this exact image:
#   1. The system `pip` at /usr/local/bin/pip installs into the system
#      site-packages, which is NOT on the venv's sys.path — Superset then fails
#      at db-engine creation with `ModuleNotFoundError: No module named
#      'psycopg2'` despite the install having "succeeded".
#   2. The venv has no `pip` of its own (uv doesn't seed one), so
#      /app/.venv/bin/pip is exit-127 "not found".
# `uv pip install --python <venv python>` is the one form that works.
RUN uv pip install --no-cache --python /app/.venv/bin/python "psycopg2-binary==2.9.10" \
    && /app/.venv/bin/python -c "import psycopg2; print('psycopg2', psycopg2.__version__)"

USER superset
