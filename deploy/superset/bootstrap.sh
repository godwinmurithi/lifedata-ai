#!/usr/bin/env bash
# First-run bootstrap for the unmodified Superset image.
#
# Runs migrations, creates the demo accounts, initialises roles, then hands off
# to Superset's normal server process. Safe to re-run: every step is idempotent.
set -euo pipefail

echo "[lifedata] superset db upgrade"
superset db upgrade

echo "[lifedata] creating admin user (${SUPERSET_ADMIN_USERNAME})"
superset fab create-admin \
  --username "${SUPERSET_ADMIN_USERNAME}" \
  --firstname Life \
  --lastname Admin \
  --email "${SUPERSET_ADMIN_USERNAME}@lifedata.invalid" \
  --password "${SUPERSET_ADMIN_PASSWORD}" || echo "[lifedata] admin already exists"

echo "[lifedata] superset init (roles + permissions)"
superset init

# A non-admin account so we can prove the spec's identity invariant: the copilot
# can never exceed the logged-in user's Superset permissions (spec §1).
echo "[lifedata] creating Alpha-role analyst user (${SUPERSET_ANALYST_USERNAME})"
superset fab create-user \
  --role Alpha \
  --username "${SUPERSET_ANALYST_USERNAME}" \
  --firstname Life \
  --lastname Analyst \
  --email "${SUPERSET_ANALYST_USERNAME}@lifedata.invalid" \
  --password "${SUPERSET_ANALYST_PASSWORD}" || echo "[lifedata] analyst already exists"

echo "[lifedata] starting Superset"
# Verified against apache/superset:6.1.0 — this is the image's own CMD
# (docker image inspect → Config.Cmd). Re-check if the pin is bumped.
exec /app/docker/entrypoints/run-server.sh
