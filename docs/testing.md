# Testing the LifeData copilot

Ordered so each step only depends on the ones above it. Steps 1–4 need nothing
but the repo. Step 5 onwards needs the stack running. Step 8 spends money.

If you are short of time and want one command that tells you whether the thing
works: **step 7**, the executive script. It is the acceptance test the whole
build is measured against.

---

## 1. Install

```sh
uv sync --all-extras --dev
uv run playwright install chromium
```

Chromium is needed even for the default test run — the markdown renderer is
tested by injecting it into a blank page, because a renderer that is only
unit-tested is a renderer nobody has watched draw anything.

## 2. Configure

```sh
cp .env.example .env
```

Then edit `.env`. Only two entries have no usable default:

| key | what to put |
|---|---|
| `ANTHROPIC_API_KEY` | your key — needed for steps 5 to 8, not for 3 and 4 |
| `SUPERSET_SECRET_KEY` | any long random string; Superset refuses to start on a weak one |

`openssl rand -base64 42` generates a suitable secret. **Never commit `.env`** —
it is gitignored, and `DJANGO_SECRET_KEY` must not stay at the dev default if
you ever set `DJANGO_DEBUG=False` (the app refuses to boot if you do).

## 3. The unit suite

```sh
uv run pytest -q
```

Expect **812 passed**, a few seconds. No stack, no API key, no network. If this
fails, nothing below is worth trying.

To run one area while working on it:

```sh
uv run pytest lifedata_ai/tests/test_write_pipeline.py -q   # what gets written to Superset
uv run pytest lifedata_ai/tests/test_review.py -q           # the design review
uv run pytest lifedata_ai/tests/test_security.py -q         # the refusals
uv run pytest tests_browser/test_markdown.py -q             # the streaming renderer
```

## 4. The invariants

Two properties no test can cover, because they are about what the source does
*not* contain. Seconds to run, and each has been violated at least once:

**No write tool may perform an HTTP write against Superset.** The user's own
browser executes every write; one `client.post(...)` here would dismantle that.

```sh
python3 - <<'PY'
import ast, sys
path = "lifedata_ai/tools/superset_write.py"
bad = sorted({
    f"{n.lineno}: .{n.func.attr}("
    for n in ast.walk(ast.parse(open(path, encoding="utf-8").read()))
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    and n.func.attr in {"post", "put", "delete", "patch"}
})
print("\n".join(bad) or f"{path}: no HTTP writes")
sys.exit(1 if bad else 0)
PY
```

Parsed rather than grepped, and the reason is instructive: `superset_write.py`'s
docstring contains the sentence *"if you reach for `client.post(...)`, stop"*, so
a substring search reports the warning as a violation. CI did exactly that until
this was written, and a check that cries wolf is worse than no check.

**No HTML sink in panel code.** Model output is untrusted and the panel runs in a
page holding the user's Superset session; everything is built with
`createElement` and `textContent`.

```sh
grep -nE '(innerHTML|outerHTML|insertAdjacentHTML|document\.write|srcdoc)' \
     lifedata_ai/static/panel/*.js | grep -vE '^\S+: *(\*|//)' \
  && echo "FOUND A SINK" || echo "ok: no HTML sinks"
```

The trailing `grep -v` is not optional: several panel comments say `innerHTML`
while explaining that it is never used.

Both are the `invariants` job in `.github/workflows/ci.yml`, which runs on every
push. The unit suite asserts the same two properties:

```sh
uv run pytest lifedata_ai/tests/test_security.py -q
```

## 5. Bring the stack up

```sh
docker compose --env-file .env -f deploy/docker-compose.yml up -d --wait
docker compose --env-file .env -f deploy/docker-compose.yml exec -T django \
    uv run --no-sync python manage.py seed_lifehealth
docker compose --env-file .env -f deploy/docker-compose.yml exec -T django \
    uv run --no-sync python manage.py register_superset_datasets
```

`seed_lifehealth` generates the synthetic health data. `register_superset_datasets`
projects `registry.yaml` onto Superset — the datasets, the column labels and the
saved metrics. **Re-run it after anyone clicks "Sync columns from source" in
Superset**, which wipes the curated labels; see runbook §1.

Check it is up:

```sh
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/lifedata-ai/api/health/
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/lifedata-ai/static/panel/panel.js
```

Both must be `200`. The second one matters more than it looks: Django serves
`panel.js` and `runserver` autoreloads on any Python edit, so for a second or
two after a code change the panel never boots and every browser test fails with
"panel did not initialise". This cost two full acceptance runs before it was
written down.

## 6. The live stack tests

```sh
LIFEDATA_LIVE=1 uv run pytest -m superset_live -q
```

Roughly four minutes. Three suites:

* **panel smoke** — the panel boots inside Superset's page, on the same origin a
  real user sees, and answers.
* **write actions** — a chart is really created, by the *user's* session rather
  than by a service account.
* **viz rendering** — one chart per allowlisted viz type, built from exactly the
  `form_data` the pipeline writes, opened in Explore and read. This is the test
  that catches a chart Superset accepts and then declines to draw. Its
  screenshots land in `tests_browser/artefacts/viz-*.png`.

Run just one:

```sh
LIFEDATA_LIVE=1 uv run pytest -m superset_live tests_browser/test_viz_renders.py -q
```

## 7. The executive script — the test that matters

```sh
uv run python tests_browser/test_executive_script.py --headed --slow
```

Ten minutes, and **watch it**. It types the four sentences an executive would
type, answers whatever the copilot asks, approves what it proposes, and then
opens the dashboard and reads it:

1. *"Build me a dashboard showing how our labs are performing"*
2. *"Show me where our patients come from"*
3. *"Actually make that first chart bigger"*
4. *"Undo that"*

Expect **15 passed, 0 failed**. Drop `--headed --slow` to run it invisibly and
faster.

**Then open `tests_browser/artefacts/exec-5-the-dashboard.png` and look at it.**
This is not optional and not superstition. Superset's API returns `2xx` for
charts it will not draw, and five separate defects in this build were invisible
to every automated check until someone looked at a screenshot:

| what you see | what it means |
|---|---|
| *"Data error"* | duplicate column labels, usually `x_axis` and `groupby` on the same column |
| *"is not registered"* | a viz type this Superset build has no plugin for |
| *"There is no chart definition…"* | the chart is related to the dashboard but has no layout node |
| **"No data"** on a fresh build | the chart is reading a control name nobody set — see runbook §16 |
| a raw identifier anywhere on screen | `rpt_lab_orders`, `facility_name`, `avg_turnaround_hours` — the copilot is speaking schema |

The script now fails on the first four. The last one it checks against the panel
text; it cannot check what Superset itself renders.

## 8. Against the real model — costs money

```sh
uv run pytest -m live_model -q
```

Four tests, manual and billed. Everything above mocks Anthropic, which means
the model id, the tool schemas and the cache breakpoints are otherwise
unverified. This confirms the model resolves, all 25 tool schemas are accepted,
and the cache actually caches. Run it after changing a tool schema, the model
id, or the prompt structure.

## 9. By hand, as a person

Automated checks cannot tell you whether the thing is *pleasant*. Log in at
<http://localhost:8080> as the admin from your `.env`, open any dashboard, press
**Alt+L**, and try:

* Ask for something impossible — *"Plot each patient's home on a map"*. It must
  refuse on the grounds of patient geography, offer districts instead, and
  mention that districts with fewer than five patients are suppressed. A refusal
  that only says "I can't" is a failure.
* Ask for something ambiguous — *"How are we doing?"*. It should ask a
  multiple-choice question in plain English, not guess and not demand a dataset.
* Ask it to change someone else's dashboard. It must decline by ownership, and
  say whose it is.
* Press **Stop** mid-build. The turn must end, and the next message must work.
* Press **Apply**, then immediately try to type and send. The composer must be
  busy — if a message goes through, you have found the race that step 7's
  `pressing Apply marks the panel busy immediately` check exists to catch.
* Reload the page mid-conversation. The transcript must come back.

For a pilot, give people their own accounts rather than sharing the admin —
`manage.py create_pilot_users` makes a non-Admin `LifeData Executive` role.
Ownership refusals are meaningless when everyone is the same user.

---

## When something fails

`docs/runbook.md` is organised by symptom — the ownership refusal, the session
expiring mid-build, the panel not initialising, "the copilot is still working on
the previous message", capacity errors. Start there rather than in the code.

Stack logs:

```sh
docker compose --env-file .env -f deploy/docker-compose.yml logs --tail 200 django
```
