# Runbook

Operational facts that are not enforceable in code, and that will cost someone a
confusing afternoon if they are not written down. Everything here has either
happened or is one obvious change away from happening.

---

## 1. Re-run `--sync-metadata` after "Sync columns from source"

**Symptom.** The copilot starts refusing columns it charted yesterday, or charts
render with headers like `facility_level` instead of "Facility level".

**Cause.** Someone pressed **Sync columns from source** on a dataset in the
Superset UI. That button rebuilds the column collection from the database schema
and **wipes every curated `verbose_name` and `description`** — everything
`registry.yaml` projected onto the dataset.

**Fix.**

```sh
docker compose -f deploy/docker-compose.yml exec django \
    uv run python manage.py register_superset_datasets
```

`--sync-metadata` runs by default. The command is idempotent: it reads each
dataset, merges the curated fields onto the columns Superset already has, and
writes back. Saved metrics are restored in the same pass.

**Why CI cannot catch this.** It happens in a browser, against a running
instance, with no commit and no deploy. There is nothing to hook.

**Two warnings the command prints, and what they mean:**

- *"N column(s) in the view are not in registry.yaml and so cannot be charted"* —
  someone added a column to a reporting view. The validator's allowlist refuses
  anything the registry does not declare, so the copilot cannot use it. Declare
  it in `registry.yaml` with a `role`.
- *"registry.yaml declares N column(s) the view does not have"* — the reverse,
  and worse: the catalogue in the system prompt is offering the model a column
  that will fail at query time. Fix the registry or the view.

---

## 2. Do not swap `runserver` for gunicorn without changing the timeout

`deploy/django/Dockerfile` says *"Swap for gunicorn when this stops being a
POC."* Whoever does that must read the comment above the `CMD` line first.

**Gunicorn's default sync-worker timeout is 30 seconds.** A chat turn that builds
a dashboard takes several model rounds and a browser round trip per write, and
`LIFEDATA_AI_TURN_TIMEOUT_SECONDS` is 300. A default gunicorn command would kill
**every build turn**, not only slow ones, and the user would see a truncated
stream with no error — indistinguishable from a dropped connection.

```sh
gunicorn lifedata.wsgi:application \
    --bind 0.0.0.0:8000 \
    --timeout 300 \            # at least LIFEDATA_AI_TURN_TIMEOUT_SECONDS
    --graceful-timeout 30 \
    --worker-class gthread --threads 8
```

The worker class matters as much as the timeout: a sync worker is occupied for
the whole of a streaming turn, so N workers would cap the deployment at N
concurrent conversations.

**nginx is already correct.** `deploy/nginx/lifedata.conf` sets
`proxy_read_timeout 300s` explicitly on `/lifedata-ai/` and on `/`. Gunicorn is
the only piece of the chain that would need changing — and the panel's `fetch()`
has no timeout of its own, only an `AbortController` for Stop.

---

## 3. Dataset ids in `registry.yaml` are live ids

`superset_dataset_id` in `registry.yaml` must match the ids in the running
Superset. After a database reset, or a fresh `bootstrap.sh`, re-run:

```sh
docker compose -f deploy/docker-compose.yml exec django \
    uv run python manage.py register_superset_datasets
```

It prints the ids. Put them in `registry.yaml` and restart the orchestrator.

**Why this fails loudly rather than quietly:** the validator resolves the dataset
for every chart through `dataset_by_superset_id`, and fails **closed** when it
cannot — an unregistered dataset id is refused with "its privacy classification
is unknown", not waved through. A wrong id is a refusal, never a policy bypass.

---

## 4. Reading the audit trail

Everything the copilot did is in Postgres, in the orchestrator's own tables.

```sql
-- Every tool call, read or write, with the user who caused it.
SELECT created_at, superset_username, tool_name, mode, ok, duration_ms
FROM lifedata_ai_toolcalllog
ORDER BY created_at DESC LIMIT 50;

-- Every proposed change and what became of it.
SELECT created_at, action_type, risk, status, executed_by_username,
       superset_object_type, superset_object_id
FROM lifedata_ai_actionrecord
ORDER BY created_at DESC LIMIT 50;

-- Why a specific action was refused, or classified as needing approval.
SELECT validation_result FROM lifedata_ai_actionrecord WHERE id = '<uuid>';
```

`status` follows a declared state machine (`ActionRecord.ALLOWED_TRANSITIONS`).
Statuses worth recognising:

| status | means |
| --- | --- |
| `proposed` | signed and sent to the browser; nothing has happened yet |
| `executed` | the browser applied it with the user's own session |
| `failed` | Superset refused it — the `execution_result` says why |
| `cancelled` | the user pressed Cancel, **or** replied instead of applying |
| `undone` | a later undo action reported success |

A record is never moved backwards out of a settled status. A late report from the
browser for a cancelled action returns **409**, which is expected rather than an
error: it means the user superseded the proposal while the request was in
flight.

---

## 5. "This dashboard isn't owned by your account"

The copilot only changes things you own. Two ways to hit this legitimately:

**The object genuinely belongs to someone else.** Working as intended. The
refusal names them.

**The object has no owner at all.** Seeded and hand-built objects often do, and
so does anything created before the copilot started naming owners on creation.
The message is different for this case — it tells you to claim it rather than to
find a colleague:

> Open it in Superset and add yourself as an owner (Edit properties → Access)

To claim a backlog of unowned objects at once:

```sh
docker compose -f deploy/docker-compose.yml exec django uv run python - <<'PY'
import httpx, os
c = httpx.Client(base_url="http://superset:8088", timeout=30)
t = c.post("/api/v1/security/login", json={
    "username": "admin", "password": os.environ["SUPERSET_ADMIN_PASSWORD"],
    "provider": "db", "refresh": True}).json()["access_token"]
h = {"Authorization": f"Bearer {t}"}
csrf = c.get("/api/v1/security/csrf_token/", headers=h)
h["X-CSRFToken"] = csrf.json()["result"]
uid = c.get("/api/v1/me/", headers=h).json()["result"]["id"]
for kind in ("chart", "dashboard"):
    for row in c.get(f"/api/v1/{kind}/?q=(page_size:100)", headers=h).json()["result"]:
        if not (row.get("owners") or []):
            c.put(f"/api/v1/{kind}/{row['id']}", headers=h,
                  cookies=dict(csrf.cookies), json={"owners": [uid]})
PY
```

**A note for anyone changing the ownership code.** Superset's owner objects carry
a numeric `id`, `first_name` and `last_name` — and **no `username`**. Code that
matches on a username finds nothing and refuses everything, which is exactly what
happened and took a browser run to see. `owns()` in `tools/superset_write.py` is
the one place that comparison lives; keep it there.

---

## 6. When the copilot says it cannot chart something

Three refusals look similar and have different fixes.

**"`x` is not a column of `rpt_…`"** — the column is not declared in
`registry.yaml`. Either it genuinely does not exist in the view, or it was added
to the view and nobody told the registry. See §1.

**"`x` is an internal key… group by `y` instead"** — declared, but with
`role: key`. Working as intended: grouping by a key produces one chart row per
record. If it really is a dimension, change its `role`.

**"Dataset id N is not in the LifeData semantics registry"** — the ids have
drifted. See §3.

---

## 7. The copilot describes charts instead of building them

This has happened once and the cause was not obvious: the capability block in the
system prompt said the copilot had "no write access whatsoever" while the write
tools were registered. The model believed the prompt.

Check, in order:

1. `GET /lifedata-ai/api/whoami/` — is the session resolving to a real Superset
   user?
2. Is the orchestrator running with write tools? `build_registry(read_only=True)`
   is a supported deployment mode and produces exactly this behaviour, correctly.
3. Does the system prompt still contain "CREATE and UPDATE charts" and "Do not
   describe how one could be built"? `test_prompts.py` asserts both, so a
   regression here should have been a red test.

---

## 8. Superset session expiry mid-build

The panel fetches a **fresh** CSRF token before every write, because Superset
ties the token to the session and briefly 401s immediately after sign-in while
the session settles. A cached failure produced a confusing "CSRF token is
invalid" from Superset rather than a clear message from the panel.

If a user reports a write failing with a session error mid-conversation: their
Superset session expired. The conversation itself survives — it is in the
orchestrator's database, keyed to their username — so signing in again and
reloading restores the thread, including any pending question or unapplied
proposal.

---

## 9. Before the pilot: give people their own accounts

**The create-and-modify-your-own boundary is not real on a shared admin login.**

The copilot refuses to change an object the caller does not own, and the
validator classes a non-owner action as needing confirmation. Both are no-ops
when everyone signs in as `admin`: everyone owns everything, and Admin bypasses
Superset's own object permissions on top of that.

```sh
docker compose -f deploy/docker-compose.yml exec django \
    uv run python manage.py create_pilot_users --dry-run \
    --user "Ada Nakato:ada@example.test" \
    --user "Bwire Okello:bwire@example.test"
```

`--dry-run` prints the role it would create and the usernames it would derive.
Drop it to create them. Passwords are generated, shown **once**, and stored
nowhere — send them individually, not to a group.

The role is `LifeData Executive`: write on Chart and Dashboard, read on the
catalogue, datasource access to the five `rpt_*` views. **Not Admin, and no SQL
Lab.** If Superset's role API refuses (it differs across versions), the command
prints the exact permission list to grant by hand rather than half-creating a
role — a role with a subset of these permissions produces a participant who can
open a dashboard and not save one, which reads as the copilot being broken.

Ship the enforcement regardless of whether you get to this. It still stops the
copilot editing a colleague's chart the moment two people share a role.

---

## 10. "I didn't want that" — undo versus delete

Two different affordances, deliberately:

| The user says | The panel offers | What happens |
| --- | --- | --- |
| "undo that change" | **Undo** | Restores the snapshot. Refused if the object changed since. |
| "I didn't want that chart" | **Delete this chart** | Removes it. Two clicks. Unundoable. |

Undo appears only where a snapshot exists; a creation has no prior state, so it
gets Delete instead. `POST /api/undo/` takes `mode: "restore" | "delete"` and
defaults sensibly, so the panel rarely needs to say which.

**The model can never propose a deletion.** `delete_chart` and
`delete_dashboard` are not in the tool registry and have no action schema. The
human gets the affordance; the copilot does not get the capability. If someone
asks for that to change, the reason it is this way is that "get rid of that" is
exactly the phrase a model over-reads.

Ownership and existence are re-checked at deletion time, not trusted from when
the action was proposed — the record may be hours old and the chart may have
changed hands.

---

## 11. Anthropic capacity

`LIFEDATA_AI_EFFORT` is `medium`, and that is a measured choice rather than a
cost one. Bisected: no tools + `high` is fine; 13 tools + `high` returned
`overloaded_error` on essentially every request; 25 tools + `medium` at 32k
succeeded. A dashboard build needs several consecutive rounds, so one overload
anywhere in the chain aborted the whole thing — `high` made multi-step work
effectively impossible.

Worth re-measuring now that prompt caching is on and the catalogue removes one to
two `get_dataset` round trips per build, because the token profile has changed.

Transient overloads are retried four times with backoff, and the backoff is added
back to the turn deadline so a busy provider does not also look like a slow one.

**Do not put real users on `high` on the strength of a hunch.** Three things have
changed since that bisect — cache breakpoints cut input volume per round, the
catalogue removes one to two tool rounds per build, and the retry loop postdates
the measurement — so `high` may now be survivable. Re-measure with a 20-request
run across (medium, high) × (32k, 16k `max_tokens`); the untested cell is `high`
at a lower `max_tokens`. If it clears, make it a per-conversation opt-in rather
than the default. A half-built dashboard is worse than a slightly shallower one.

---

## 12. Verifying against the real model

```sh
uv run pytest -m live_model      # billed, manual, ~1 minute
```

Nothing else in CI touches a real model: the end-to-end job drives
`scripts/mock_anthropic_server.py` through `ANTHROPIC_BASE_URL`, which is what
makes it deterministic, free and runnable on fork PRs. That leaves the model
string and every request parameter unverified by anything automatic — and the
parameters have moved. `temperature`, `top_p` and a fixed `thinking` budget are
outright 400s on current models, and reasoning depth travels inside
`output_config.effort`.

Four checks, cheapest first:

1. the configured model resolves and the request shape is accepted;
2. `temperature` is *still* rejected, so the comments explaining its absence stay
   true;
3. all 25 tool schemas are accepted — a keyword the API dislikes (`minimum` on an
   integer was the one that bit) 400s the entire turn, not just that tool;
4. the cache breakpoints are accepted **and the API reports caching something**.
   Without that last clause the test would pass on a request that cached nothing,
   which is exactly the state the code was in before: the prompt module argued
   for prefix ordering at length and then sent `system=` as a plain string, which
   cannot carry a breakpoint.

Run it after any change to `agent/loop.py`'s request shape, to
`LIFEDATA_AI_MODEL`, or to a tool's `input_schema`.

---

## 13. The panel "did not initialise" and nothing says why

Two causes, and they look identical from the browser.

**You just edited a Python file.** `runserver` autoreloads, and Django serves
`panel.js` — so for a second or two the panel script 404s or hangs and the panel
never boots. Wait and reload. This bites hardest when running the browser tests
straight after a code change; the fix is to wait for the container to settle:

```sh
curl -s -o /dev/null -w "%{http_code}\n" \
    http://localhost:8080/lifedata-ai/static/panel/panel.js   # want 200
```

**`start()` threw.** `panel.js` wraps its bootstrap in log-and-render-nothing, so
a copilot defect never presents as a Superset defect — which means a genuine
bootstrap failure is *silent in the page*. Open the browser console: the message
is `LifeData copilot failed to start; Superset is unaffected.` followed by the
error.

That trade is deliberate. The panel runs inside HTML injected into a page the
user came to use for something else, and a throw there can abort the rest of the
page's scripts. Quiet-and-contained is right; the console line is how you find it
anyway.

---

## 14. "The copilot is still working on the previous message"

The in-flight guard, doing its job. One conversation runs one turn at a time —
two concurrent turns would interleave writes to the same history and could each
answer half a batch of tool calls — the 400 that the module docstring of
`lifedata_ai/agent/loop.py` describes under "Waiting for a human". Refusing is
right.

**If it appears while someone is just using the panel normally, that is a bug, not
a busy server.** The guard should be unreachable: the composer knows when a turn
is running. Reaching it means the panel looked idle while the orchestrator was
still holding the conversation. Two windows have done that:

* **Pressing Apply.** Executing an envelope is several writes against Superset,
  and the panel used to set busy only when the turn *resumed* afterwards. Anyone
  typing during the writes raced the resume. Fixed by setting busy in the same
  tick as the click; `_check_busy_on_apply` in the acceptance script asserts it
  with no wait, because a `wait_for_function` would pass on the broken code by
  sleeping through the window.
* **A turn abandoned mid-stream** — laptop closed, tab killed. The claim is
  reclaimable once it goes stale (turn timeout plus a 60s margin), so this clears
  itself. Pressing Stop clears it immediately.

To see whether a claim is genuinely held:

```sh
docker compose exec orchestrator python manage.py shell -c \
  "from lifedata_ai.models import Conversation as C; \
   print([(str(c.id), c.turn_started_at) for c in C.objects.exclude(turn_started_at=None)])"
```

A `turn_started_at` older than a few minutes is a stale claim, not live work.

---

## 15. The acceptance test that matters

Not a command — a script to type, from a clean database, as an executive would.
**No schema words, no dataset ids, no column names anywhere.** If it needs
technical vocabulary, ease of use is not fixed.

1. *"Build me a dashboard showing how our labs are performing"* → a clarifying
   question with plain-English choices, then a dashboard with a row of headline
   numbers, a monthly trend, a breakdown and a date filter.
2. *"Show me where our patients come from"* → district-level output, never
   points.
3. *"Actually make that first chart bigger"* → the layout compiler runs.
4. *"Undo that"* → it works.

**Then look at the dashboard.** The lesson from this build is that the Superset
API accepts almost anything and only the browser reveals the truth: four
separate defects — a missing chart-to-dashboard relation, an unregistered `bar`
viz type, duplicate column labels, and KPI tiles saved with the wrong metric key
— all returned `2xx` and appeared only in a screenshot. Check for *"Data
error"*, *"There is no chart definition"*, *"is not registered"* and **an empty
widget**.

The last one is the one to take seriously. The script went 13/13 green over a
dashboard whose three headline tiles all read *"No data"*: no error marker was
present, the layout was right, the filter was there, cross-filtering was on.
Nothing was malformed. `big_number_total` was reading a control name nobody had
set. See §16.

---

## 16. Adding a viz type to the allowlist

`viz_allowlist` in `registry.yaml` is short and every entry is verified. Adding
one is not a config change — **build a chart of that type and look at it**, then
add a case to `tests_browser/test_viz_renders.py`. That file already fails if an
allowlisted type has no case, so the reminder is automatic.

The reason is that Superset's `form_data` control names differ per plugin and a
name nobody set is ignored in silence:

| viz type | reads |
|---|---|
| `big_number_total`, `pie` | `metric` — **singular** |
| `echarts_timeseries_line`, `echarts_timeseries_bar`, `table` | `metrics` — a list |

`_apply_single_metric_alias` in `superset_write.py` derives `metric` from
`metrics` for the singular ones. `_apply_number_format` does the same job for
`y_axis_format` versus `number_format`. Expect the next viz type to have its own
version of this, and expect it to be invisible until someone looks:

```sh
LIFEDATA_LIVE=1 uv run pytest -m superset_live tests_browser/test_viz_renders.py
```

A chart that renders "No data" and one that renders "Add required control
values to preview chart" are the same defect wearing different clothes — the
second is what a `pie` says, and it never appears on a dashboard because a
dashboard has no control panel to complain about.

---

## 17. Charts built before July 2026 ignore their own time period

**Any chart the copilot created before this was fixed is showing more data than
its caption claims**, and nothing about it looks wrong.

`time_range` on its own does nothing. It is the legacy control; the modern viz
plugins take the period from an `adhoc_filters` entry whose operator is
`TEMPORAL_RANGE`. Superset saves the chart either way and reports no problem.
Measured on the same tile, twice:

| form_data | rendered |
| --- | --- |
| `time_range: "Last 12 months"` | **14,355** — every order ever recorded |
| the same, plus a TEMPORAL_RANGE filter on `ordered_at` | **5,922** — correct |

`_apply_time_range` in `superset_write.py` now writes the filter on every chart,
so anything built from here on is right. Older charts are not migrated
automatically — rewriting a colleague's saved chart without being asked is
exactly what the ownership rules exist to prevent.

To find them — note this is Superset's own metadata database, `superset`, not
`lifedata`:

```sh
docker compose --env-file .env -f deploy/docker-compose.yml exec postgres \
  psql -U "$POSTGRES_USER" -d superset -c \
  "SELECT id, slice_name FROM slices
    WHERE params LIKE '%time_range%' AND params NOT LIKE '%TEMPORAL_RANGE%'
    ORDER BY changed_on DESC;"
```

Ask the copilot to rebuild the ones that matter, or open each in Explore and set
the time range in the UI — which writes the filter correctly.

**How to spot it by eye:** a trend whose x-axis runs back further than the
period claims. A dashboard captioned "last 12 months" whose monthly line starts
two years ago is this, every time.
