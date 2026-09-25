# Superset 6.1.0 integration findings

Spec §10 says: *pin the deployed version, then verify (and note in code comments)*
the integration points, *reading the Superset source rather than guessing* where
docs are ambiguous. This file is that record.

**Pinned version: `apache/superset:6.1.0`** (latest 6.x release at build time;
`6.0.0` is the only other 6.x tag published).

All checks were run through nginx at `http://localhost:8080` — the same origin
the browser and the panel see — not against the container directly.

---

## Verified working

| Integration point | Result |
| --- | --- |
| Superset reachable through nginx | `GET /health` → `200 OK` |
| **`sub_filter` panel injection** | `panel.js` script tag present in `/login/` HTML |
| `Accept-Encoding ""` precondition | Response `Content-Encoding: None` — upstream gzip disabled, so `sub_filter` can operate |
| Database (Flask-AppBuilder) auth | Login form → `session` cookie set |
| `GET /api/v1/me/` | `200`, returns the logged-in username |
| Anonymous `GET /api/v1/me/` | `401` — the middleware's reject path works |
| `GET /api/v1/database/`, `/dataset/`, `/chart/`, `/dashboard/` | all `200` — read-tool surface confirmed |
| **Superset 6 theme objects API** (`/api/v1/theme/`) | `200` — theme objects available, so `apply_dashboard_theme` uses them and never emits CSS |
| CSS template API (`/api/v1/css_template/`) | `200` — fallback path exists but is not needed |
| Dashboard `changed_on` | Present on `GET /api/v1/dashboard/{id}` — the undo staleness check (§4) is implementable |
| Dashboard `owners` | Present — ownership-based risk classification (§6.6) is implementable |

---

## Corrections to the specification

These are places where the spec's stated assumption does not match Superset
6.1.0. Each one would have caused a bug if implemented as written.

### 1. `/api/v1/security/csrf_token/` requires authentication

Spec §7 describes the panel fetching a CSRF token, implying it is freely
available. It is not:

- Unauthenticated → **`401`**
- Authenticated (session cookie present) → **`200`** with `{"result": "<token>"}`

**Impact:** none in practice — the panel only ever runs inside an already
authenticated Superset page. But the panel's token fetch must handle `401` as
"session expired, re-authenticate", not as a transient error to retry.

### 2. `GET /api/v1/me/` does **not** return roles

Spec §1 leans on `/api/v1/me/` for identity, and §6.6 needs role/ownership
information for risk classification. The `me` payload contains only:

```
email, first_name, id, is_active, is_anonymous, last_name, login_count, username
```

There is no `roles` key. Roles live at a **separate endpoint**:

```
GET /api/v1/me/roles/   →   {"result": {"Admin": [[perm, view], ...]}}
```

**Impact:** the identity middleware must call *both* endpoints (and cache both
for the 60 s window §1 specifies) if role-aware policy is wanted. Ownership for
the un-owned-dashboard preview rule comes from `get_dashboard(...).owners`, not
from the user object.

### 3. `POST /api/v1/chart/data` does not validate an empty body gracefully

An empty `{}` body returns **`500`**, not `400`. With a shaped body naming a
non-existent datasource it correctly returns `404`.

**Impact:** the `get_chart_data` read tool must never forward a malformed or
empty query context — validate before calling, and treat a `500` from this
endpoint as "our request was malformed", not "Superset is broken".

### 4. `position_json` and `json_metadata` are `None` on a fresh dashboard

Not `{}`, not `"{}"` — SQL `NULL`, surfacing as JSON `null`.

**Impact:** the layout compiler (§6.7) must treat "no layout yet" as a first-class
case and synthesise a root `GRID`/`ROOT` structure rather than assuming a
parseable object. A naive `json.loads(position_json)` crashes on the very first
dashboard the copilot creates.

### 5. The image ships **no Postgres driver at all**

`psycopg2`, `psycopg`, `pg8000` and `asyncpg` are all absent from
`apache/superset:6.1.0`. Superset cannot reach its own metadata database, let
alone the LifeHealth analytics database.

**Impact:** spec §0's "no patched images" rule cannot be met literally. See
`deploy/superset/Dockerfile` — the derived image adds the driver and nothing
else. Two further traps found while doing it:

- The system `pip` at `/usr/local/bin/pip` installs into a site-packages
  directory that is **not on the venv's `sys.path`**. The install "succeeds" and
  Superset still fails with `ModuleNotFoundError: No module named 'psycopg2'`.
- The venv at `/app/.venv` is **uv-managed and has no `pip`**, so
  `/app/.venv/bin/pip` is exit-127.
- The working form is `uv pip install --python /app/.venv/bin/python ...`.

### 6. Superset refuses to start with a weak `SECRET_KEY`

6.1.0 hard-fails at boot with *"Refusing to start due to insecure SECRET_KEY"*
rather than warning. A generated key is mandatory, not advisory.

### 7. The image's CMD is `/app/docker/entrypoints/run-server.sh`

Not `/usr/bin/run-server.sh`. The bootstrap script must `exec` the real path or
the container exits immediately after creating users.

---

## Confirmed: the identity and CSRF model works as designed

This is the spec's critical invariant (§1), so it was tested directly:

| Probe | Result |
| --- | --- |
| `POST /api/v1/dashboard/` **without** `X-CSRFToken` | **`400`** — *"The CSRF token is missing."* |
| `POST /api/v1/dashboard/` **with** `X-CSRFToken` | **`201 Created`** |

Superset enforces CSRF on state-changing API calls. The panel genuinely must
hold the user's session cookie *and* a matching CSRF token to write anything,
which is exactly the property that makes "write actions execute as the user,
never as a service account" enforceable rather than merely intended.

---

## Incidental observations

- Superset 6.1.0 logs `fastmcp is not installed, skipping MCP initialization`
  at boot. The official MCP effort the spec mentions in §0 is present but
  optional (`pip install 'apache-superset[fastmcp]'`). We call the REST API
  directly as planned; the tool interface in `tools/base.py` keeps the MCP
  swap open.
- The dashboard object exposes a `theme` field, consistent with theme objects
  being the right target for `apply_dashboard_theme`.
- SQLAlchemy in the image is 1.4.54, Python 3.10.
