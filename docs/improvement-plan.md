# LifeData Copilot — Improvement Plan

**Goal:** take functionality, UI/UX and ease of use from their honest current scores
(**6/10, 5/10, 3/10**) to excellent, for a specific audience: **internal executives who are not
technical at all and need to create dashboards.**

**Estimated effort:** 14–17 working days for one implementer who knows this codebase.

---

## 1. Context

The copilot works. Embedded in Superset 6.1.0, it has built 3 dashboards and 8 charts unaided, with
every write executed by the user's own browser session and a full audit trail behind it.

The gap is not capability, it is **fit**. Every prompt that has worked so far was schema-literate:

> *"from rpt_lab_orders (dataset 3), counting lab_order_id grouped by facility_name"*

An executive will not write that. Meanwhile a real bug is live in production code, the entire write
pipeline is untested, and the repository is not under version control.

### Decisions taken

| Decision | Consequence |
| --- | --- |
| **No recipes or templates** — free-form building, better guided | The catalogue and the clarification flow carry all reliability weight. `layout_presets` stays unused. |
| **Interactive clarification with multiple-choice questions.** The copilot asks, then builds — and only once it has full context | The centrepiece. Replaces silent guessing with asking. |
| **Create and modify their own only** | Ownership needs hard enforcement, not risk-scoring. Undo must actually work. |
| **No dynamic-forms work** | Reporting only. |
| Internal pilot with real users | Reliability and usability, not scale-hardening. |

---

## 2. Findings that change the plan

Six discoveries from a full read of the stack. All verified against the running system. Each one
changes the work, so they come before the plan.

### 2.1 There is no version control — fix this first

No `.git` directory. Instead there is a `CTI_Life_Data.rar` (0.2 MB) and a `_dump_tmp.py` at the
repository root.

This plan deletes a load-bearing function, rewrites the panel's render path, and PUTs metadata into
a live Superset instance. **`git init` and a baseline commit is step one. No exceptions.** Add the
`.rar` and `_dump_tmp.py` to `.gitignore` or delete them.

### 2.2 The layout compiler *can* place a new chart — `build_position_json` is redundant

This corrects an earlier assessment of mine, which was wrong.

`validation/layout.py:763` defines `_op_add_chart`, registered in `_OPERATIONS` at `layout.py:848`
and exposed via `SUPPORTED_OPERATIONS`. It handles `placement` of `append | new_row | kpi_row | top`,
sizes tiles and reflows the row. And `parse_position_json(None)` returns `empty_skeleton()`
(`layout.py:141`), so `compile_operations(None, [{"operation": "add_chart", …}])` builds a valid
layout from nothing at all.

So `build_position_json` (`tools/superset_write.py`) is a weaker duplicate — hardcoded rows of two,
fixed height 50, no placement semantics — **and it is the only reason the compiler's 59 tests have
never run end to end.** Deleting it is the highest-leverage single change in this plan, and with
recipes off the table it is essential rather than tidy.

### 2.3 `placement` and `layout_preset` are accepted and silently ignored

`_ADD_CHART_TO_DASHBOARD` declares `placement: [append, new_row, kpi_row, top]` and
`AddChartToDashboardTool.build_calls` never reads it. `_CREATE_DASHBOARD` declares `layout_preset`
and `CreateDashboardTool.build_calls` never reads it. The model can already ask for a KPI row and be
ignored.

### 2.4 `create_chart(dashboard_id=…)` creates a chart that does not render

`CreateChartTool.build_call` sets `body["dashboards"] = [dashboard_id]` and emits **only** the POST.
A chart needs *both* the relation **and** a `position_json` CHART node — layout alone renders
*"There is no chart definition associated with this component"*, and the relation alone leaves the
chart absent from the layout. This is likely a large part of why early dashboards looked wrong.

### 2.5 `update_chart` does not merge params, contradicting its own contract

`validation/schemas/actions.py` states explicitly that the write tool *"must read the existing
params, merge these keys over them, and write the result back — otherwise 'make it horizontal'
silently discards the chart's filters."* `superset_write.py` replaces wholesale.

For an executive, *"make that a line chart instead"* is the **most common follow-up**, and today it
silently destroys the chart's filters, time range and metrics.

### 2.6 Undo corrupts the audit trail before it crashes

Verified line ordering in `views/undo.py`:

```
undo.py:106    action.status = ActionRecord.Status.UNDONE
undo.py:107    action.save(update_fields=["status", "updated_at"])
undo.py:109    envelope = build_envelope(          ← TypeError raised here
```

`build_envelope` now takes `calls: list[dict]`, not `method`/`path`/`body`. So **every
`POST /api/undo/` marks the original action as undone and then 500s.** The record now lies, which is
worse than the crash. The `UNDONE` transition must move into `action_result` and fire only when the
undo action reports `executed`.

A second break sits behind it: `undo.py` returns `{"preview": {…}}` but the panel's `executeAction`
reads `action.calls` and bails when empty — so even with the `TypeError` fixed, undo would silently
no-op.

Also: `WriteTool.run` never sets `ActionRecord.superset_object_type`, which deletion-as-inverse needs.

### 2.7 There is no prompt caching, so the ordering discipline pays for nothing

`agent/prompts.py`'s docstring builds a careful argument about stable-prefix ordering for Anthropic
prompt caching. But `loop.py` passes `system=` as a plain string with **no `cache_control`
breakpoint anywhere**, and the `tools` array has none either. Caching requires explicit breakpoints.

The 25 tool schemas — the largest stable block in every request — are re-billed at full price on
every round of every turn.

---

## 3. Sequencing

| Phase | Content | Why here |
| --- | --- | --- |
| **0** | git init; CI skeleton; 14 stale tests; invariant guards | You cannot safely rewrite load-bearing code with a red suite, no CI and no VCS. Stale tests first, or a new break is indistinguishable from an old one. |
| **1** | Write-pipeline correctness; ownership; `action_result` state machine + per-conversation in-flight guard (§5.7); the three security one-liners (§9.5); same-day panel wins | Undo is broken *and* corrupting records. Nothing may be built on an untested envelope path. The state machine and the in-flight guard protect the same record, so they land together. |
| **2** | Semantic layer: catalogue, saved metrics, prompt prefix, real caching | The ease-of-use crux. Also *reduces* how often clarification is needed, so it precedes Phase 3, and supplies the number formats Phase 4 needs. |
| **3** | Clarification loop (`ask_user`), incl. pair-aware history truncation | Depends on a tested loop (1) and on catalogue defaults defining what *not* to ask (2). Proves the deferred-resume path on one tool before §5.7 extends it. |
| **4** | Executive output: layout compiler wired up, filters, cross-filters, `review_dashboard_design`, chart-type policy | Depends on the 2.2–2.4 fixes and Phase 2's metrics. This is what replaces recipes. |
| **5** | Panel Tier 1; **preview cards onto the deferred-resume path (§5.7)** | Built once against the final event set, not twice — and the preview deferral is a panel change as much as a loop change, so it lands with the rebuild rather than before it. |
| **6** | Panel Tier 2/3; browser tests in CI | |
| **7** | Measure: `effort` experiment, pilot Superset accounts, runbook | The `effort` question is only answerable after 2 and 4 change the token profile. |

**Pulled into Phase 1 as same-day wins** — each under ~10 lines, each immediately visible: prefer
`data.summary` over `TOOL_LABELS`; fix the two `--ld-muted` CSS bugs; add the missing
`.ld-action.is-applied` rule; replace the false intro copy; gate the Undo button on the `undoable`
flag the server already returns.

---

## 4. Ease of use — the strategy

### 4.1 The decision

**Registry-as-source-of-truth, projected two ways. No Python phrase resolver.**

- `registry.yaml` gains labels, synonyms, per-column metadata, default dimensions and SQL-bearing
  metric definitions
- One management command projects it into **Superset's own dataset metadata** (`verbose_name`,
  `description`, saved `metrics` with `expression` and `d3format`)
- One function compiles the same file into a **curated catalogue in the prompt's stable prefix**,
  including column names and friendly labels

### 4.2 Why

**Superset saved metrics are mandatory, not optional.** Three of six governed indicators —
`confirmed_diagnoses` (`COUNT(*) FILTER (WHERE …)`), `malaria_positivity` and `lab_completion_rate`
(ratios) — are structurally unrepresentable in `_SIMPLE_AGGREGATION`, and that unrepresentability is
a security property that must not be weakened. Saved metrics are the only route.

**And both sides are already built.** `get_dataset` already returns `verbose_name`/`expression`/
`description` per metric, and `_to_superset_metric`'s first branch passes a bare identifier straight
through. So `"metrics": ["malaria_positivity"]` works **today, with zero orchestrator changes**, the
moment the metric exists on the dataset.

**The catalogue is mandatory too, because saved metrics alone never reach the model.** Superset
metadata is only visible via `get_dataset`, which costs a tool round — and on a fresh chat the model
does not even know *which id* to fetch, because `build_semantics_block` resolves a dataset only from
`page_context.dataset_id`, so the common path emits name + grain + notes and **not even the dataset
id**. Every chart therefore costs `list_datasets` → `get_dataset` → `create_chart`, and every hop is
a chance to describe instead of build.

**A Python phrase resolver is rejected.** It is an unbounded NLP project that reimplements, worse,
what the model already does well — and every miss produces a *confident wrong answer* with no
signal. `rank_datasets` is the existing proof: exact whole-token equality after a whitespace split,
so *"lab orders by facility"* scores zero against `laboratory/turnaround/testing` and zero against
`facilities`, and `recommend_dataset` then dumps all five datasets unranked. Its only working branch
requires typing `rpt_lab_orders`. Under the clarification decision, ambiguity is answered by
**asking**, which removes the motivation entirely.

### 4.3 Prompt caching must actually be switched on

Two changes, both in scope for Phase 2:

1. **Make the catalogue page-independent and put it in the stable prefix** — same bytes for every
   user on every page. Order becomes: role, hard rules, capabilities, style, **catalogue**,
   **how-to-build**, **how-to-ask** ‖ then identity, then page context.
2. **Add real cache breakpoints** in `loop.py`: pass `system` as a block list with
   `cache_control: {"type": "ephemeral"}` on the last stable block, and set `cache_control` on the
   final entry of `tool_specs`. `ToolRegistry` already documents that insertion order is preserved
   to keep the cache warm — honour that promise.

Cost: ~2.5k tokens of stable prefix. Against cache-read pricing that is a rounding error, and it
removes one to two `get_dataset` round trips per build. **Net token cost is negative**, and caching
the 25 tool schemas is a larger win than the catalogue itself.

### 4.4 Registry schema extension

```yaml
datasets:
  - name: rpt_lab_orders
    label: "Laboratory orders"
    superset_dataset_id: 3
    grain: lab_order
    one_row_is: "one laboratory ORDER, not a completed test"
    synonyms: [lab, labs, laboratory, tests, testing, samples, turnaround,
               results, malaria test, specimen]
    time_column: ordered_at            # -> main_dttm_col
    default_dimension: facility_name   # <- resolves "by facility"
    default_time_range: "Last 12 months"
    columns:
      - {name: facility_name,  label: "Facility",       role: dimension,
         note: "The facility's name, e.g. Kampala HC IV"}
      - {name: facility_id,    role: key}               # never a groupby
      - {name: facility_level, label: "Facility level", role: dimension,
         note: "HC II / HC III / HC IV / Hospital"}
      - {name: district_name,  label: "District",       role: dimension}
      - {name: ordered_month,  role: internal}          # hidden; see §8
    metrics:                            # projected as Superset saved metrics
      - {name: orders, label: "Orders", expression: "COUNT(*)", d3format: ",d"}
      - name: malaria_positivity
        label: "Malaria positivity"
        expression: >-
          COUNT(*) FILTER (WHERE test_type='malaria_rdt'
            AND result_value='positive' AND order_status='verified')::numeric
          / NULLIF(COUNT(*) FILTER (WHERE test_type='malaria_rdt'
            AND order_status='verified'), 0)
        d3format: ".1%"
        indicator: malaria_positivity   # back-link to the governed definition
```

`role` values: `dimension | measure_source | key | internal | temporal`.

The `indicator:` back-link keeps `explain_indicator` and the chart drawn on screen on the same
definition — the repo's stated principle applied to metrics.

> **This file is the only place SQL is authored in the whole system**: by a human, in a reviewed
> file, applied by an admin command, never by the model and never at request time. Say so in the
> file header, or a reader will think the no-SQL rule has been violated.

### 4.5 The rendered catalogue block

```
# What you can chart (the LifeData catalogue — complete; nothing else exists)

## Laboratory orders  ·  dataset id 3  ·  rpt_lab_orders
one row is one laboratory ORDER, not a completed test
use for: laboratory volume, turnaround, test results, malaria testing
time column: ordered_at   ·   default period: Last 12 months
DEFAULT BREAKDOWN: facility_name  <- use this for an unqualified "by facility"
break down by: facility_name "Facility" | facility_level "Facility level"
  (HC II/III/IV/Hospital) | district_name "District" | region "Region"
  | test_type "Test" | order_status "Order status"
measure with: orders "Orders" (,d) | malaria_positivity "Malaria positivity" (.1%)
  | lab_completion_rate "Lab completion rate" (.1%)
  | avg_turnaround_hours "Average turnaround (hours)" (.1f)
never group by: facility_id, lab_order_id (internal keys)
traps: turnaround_hours is NULL unless verified_at is set, so averages describe
  completed work only. An order is never a result.
```

Those are saved-metric names, so they pass straight into `params.metrics` as bare strings and clear
validation unchanged.

### 4.6 "By facility" — resolved three ways over

1. **A declared default in the prefix.** `DEFAULT BREAKDOWN: facility_name` makes the unqualified
   case deterministic and free. Prompt rule: use it, and *say* you did.
2. **Ask when it genuinely matters.** If the request implies a comparison where `facility_level` is
   plausible (*"which kinds of facility are slowest?"*), `ask_user` offers
   `Facility · Facility level · District · Region` as chips, with each column's `note` as the hint.
   That is how a non-technical executive chooses between four columns without learning what a column
   is.
3. **Enforce the negative — as an allowlist, not a denylist.** New validator rule: **only columns
   declared `role: dimension` or `role: temporal` are valid `groupby`/`x_axis` values.** Everything
   else is refused with the catalogue's alternatives named (*"`facility_id` is an internal key; group
   by `facility_name` instead"*). A silent wrong chart becomes a self-healing tool error.

   The allowlist is chosen over refusing `role: key` specifically, for three reasons: it is one rule
   rather than a growing list of forbidden roles; it matches the catalogue header's own claim
   (*"complete; nothing else exists"*) instead of contradicting it; and it closes the case a denylist
   leaves open — a column the registry never declared at all (a new view column, a future dataset)
   silently becoming groupable. Same fail-closed posture the geography rule already takes.

### 4.7 The projection command

Extend `register_superset_datasets.py` with a `--sync-metadata` step, run by default:

- `GET /api/v1/dataset/{id}` first. For `columns`, **merge and preserve each existing entry's `id`**
  — `PUT` replaces the collection, so a blind PUT deletes every column
- `PUT {metrics, columns, main_dttm_col, description}`
- Idempotent and re-runnable. **Runbook line: re-run after any "Sync columns from source" in the
  Superset UI, which wipes curated labels.** CI cannot catch that; document it
- Verify: `get_dataset(3)` shows non-null `verbose_name` on every column and six metrics with
  expressions and formats; then build one chart with `"metrics": ["malaria_positivity"]` and confirm
  it renders a percentage with a sensible axis title

---

## 5. Interactive clarification — the centrepiece

### 5.1 Mechanism: `ask_user` as a deferred tool result

Three shapes were considered. **Block-and-poll (as `_await_action_outcome` does) is rejected as the
primary mechanism**: that pattern waits ~30s for a *browser*, whereas a human reading four options
takes 20–90 seconds or goes to a meeting. It would hold a WSGI thread and a DB connection for up to
the 300s turn timeout, sits exactly on nginx's `proxy_read_timeout`, and — decisively — **cannot
survive a page reload**, which is fatal for a mechanism whose whole premise is waiting for a person.

**Chosen: end the turn, but resume the *tool call*.**

1. Model calls `ask_user(…)`. It runs through the normal audited path (so `ToolCallLog` records it)
   and writes a `Clarification` row
2. The loop persists the assistant message containing the `tool_use` block, emits an SSE
   `clarification` event, and **breaks with `stop_reason="awaiting_user"`** — deliberately writing
   **no** `tool_result`
3. The panel renders the card. Clicking a chip submits the answer as a normal message on the same
   `conversation_id`
4. `run_turn` checks for a pending `Clarification` first. If one exists it marks it answered and —
   instead of a user text message — writes
   `Message(role=TOOL, content=[{"type": "tool_result", "tool_use_id": <the ask_user id>, …}])`.
   `_WIRE_ROLE` already maps `TOOL → "user"`

The model then sees `[assistant: tool_use ask_user][user: tool_result "facility_name"]` and continues
as though `ask_user` simply took ninety seconds to return. That buys block-and-poll's coherence with
none of its fragility: no held thread, no held connection, survives a reload, survives days — and
*"only start when you have full context"* becomes **structural**, because the model literally cannot
proceed without the tool result.

It also closes a latent hazard: an assistant message ending in `tool_use` with no matching
`tool_result` is rejected by the Messages API. This design guarantees the pairing completes.

### 5.2 Pieces

**`models.py` — new `Clarification`:** conversation FK, `tool_use_id`, `question` JSON, `answer`
JSON, `status: pending|answered|abandoned`, timestamps, index on `(conversation, status)`. At most
one `pending` row per conversation by construction.

**New `tools/interaction.py`, `AskUserTool`**, `mode = ToolMode.READ` (it writes nothing to Superset;
the SSE emission is the loop's job). Schema **grouped from the start** — up to 3 questions in one
card, 2–5 options each, optional `hint` and `allow_free_text`. Grouping is the right call: the common
real case is *(breakdown, period)*, and asking those in two round trips doubles the latency and the
annoyance, which is exactly the abandonment risk.

**Three loop guards**, each preventing state that would otherwise need managing:

- `ask_user` alongside other `tool_use` blocks → `ToolError` (*"call ask_user on its own"*), because
  the API requires every `tool_use` in a message to receive a `tool_result`
- `ask_user` after a write in the same turn → `ToolError` (*"ask before you build, not after"*)
- One `ask_user` per turn

**SSE contract:** new `clarification` event, and `message_stop` gains
`stop_reason: "awaiting_user"`. Guard the "returned no text" path so a turn that only asks does not
trigger it.

**`views/conversations.py`:** `conversation_detail` must include `pending_clarification` — a
mechanism that waits indefinitely must survive a reload. Also surface the answer, since
`_visible_text` returns `""` for `TOOL`-role messages today, so a replayed thread would lose it.

**Two `run_turn` changes the design implies and must state explicitly:**

- **The resume branch must not create the user message.** `run_turn` today *unconditionally* writes
  `Message(role=USER, content=[text])` at `loop.py:317` and then updates `title` and `page_scope` in
  the same block. The resume branch writes the `TOOL` message instead, so it must skip that
  creation — while still performing the `page_scope` update, and still deriving a title if the
  conversation somehow lacks one. Without the skip, a resumed turn persists both rows and the model
  sees a spurious user turn beside its own tool result.

- **Truncation must never split a `tool_use`/`tool_result` pair.** `_load_history` slices the last
  `MAX_HISTORY_MESSAGES = 40` rows, then pops leading rows until the first is a genuine user message
  and not a `tool_result`. If a pending pair straddles that boundary, the assistant `tool_use` falls
  off the front and the orphaned `tool_result` — the executive's answer — is popped: **the answer
  vanishes silently and the model re-asks or guesses.**

  This is not an edge case. One turn writes an `ASSISTANT` row plus a `TOOL` row per tool round
  (`loop.py:464`, `loop.py:579`) and `LIFEDATA_AI_MAX_TOOL_ROUNDS = 12`, so **a single dashboard
  build can write 25 rows** — the second build in a conversation already overflows a 40-row window.

  So fix it as a general rule rather than a clarification special case: make the truncation
  pair-aware (never begin the window between a `tool_use` and its `tool_result`; extend backwards to
  include the whole pair), and raise `MAX_HISTORY_MESSAGES` so one build is not most of the budget.
  A pending `Clarification` is then simply the case the general rule already covers. Negative test
  in §5.6.

### 5.3 Panel rendering

Mounted **in the transcript**, not the actions rail — it belongs in reading order beside the question
it answers.

```
.ld-ask                      card, accent left border
  .ld-ask__group             one per question
    .ld-ask__question
    .ld-ask__options         role="group" aria-labelledby=<question id>
      button.ld-suggestion.ld-ask__option
        .ld-ask__label       option.label
        .ld-ask__hint        option.hint  <- "The facility's name, e.g. Kampala HC IV"
      button.ld-ask__other   "Something else…" -> focuses the composer
  button.ld-ask__continue    only when questions.length > 1
```

- Reuses `.ld-suggestion` chip styling, which already has hover and inherits the focus ring. Built
  with the existing `el()` helper; **zero `innerHTML`**, unchanged
- Keyboard accessible for free: real `<button>`s in DOM order
- **Single question → click submits immediately.** Multi-question → each group records a selection
  (`aria-pressed`), Continue enables when all are answered
- **Free-text escape hatch two ways:** a "Something else…" chip, and the composer stays live with its
  placeholder swapped to *"Choose an option above, or type your answer"*
- After answering: disable the chips, mark the chosen ones, and **keep the card** — an executive
  scrolling back should see what they chose. Also render the choice as a normal user bubble

### 5.4 The prompt discipline — avoiding interrogation

New stable-prefix section, to ship close to verbatim:

```
# Asking before you build

Your users are executives. They know what they want to see. They do not know
column names, dataset names or chart types, and must never be made to learn any.
Never show a column name without a plain-English label beside it.

Before building, you must be able to state in ONE sentence all four of: what is
measured, how it is broken down, over what period, and from which dataset. If one
is genuinely undetermined by the request AND by the catalogue above, call
`ask_user` — once — with the choices.

ASK when two or more readings are equally defensible AND would produce materially
different charts. "By facility" can mean the facility's name or its level.
"Patients" can mean people or visits. "Recently" can mean 3 months or 12.

DO NOT ASK when:
  - the catalogue already names a default. A DEFAULT BREAKDOWN is the answer to
    an unqualified "by facility". Use it and say that you did.
  - a tool can determine it. Never ask which columns or charts exist.
  - the answer does not change what the user sees: colours, legend position,
    row limit, the exact grain when a period is already stated.
  - you are only missing a title. Write a good one and say what you called it.
  - the user already answered it earlier. Re-read the conversation.
  - you have already proposed a change this turn. Ask first, or not at all.

Put EVERY open decision into that one call (at most three questions, at most five
options each). Never ask twice in one build.

If something is still ambiguous after one round, do not ask again. Choose the most
defensible reading, BUILD it, and say plainly which reading you used and how to
change it. One sentence of correction is far cheaper for an executive than a
second interrogation.

Prefer a stated default over a question. "I'll show unique patients by facility
for the last 12 months — building it now" beats three questions.

Do NOT ask the user to confirm that they want the thing they just asked for.
Authorising changes is the panel's job, not yours.
```

**Rule of thumb:** ask only when two defensible readings produce materially different charts, never
about anything a tool can determine, and never more than one card per build.

### 5.5 No double-asking

`ask_user` gathers **intent**; the Apply card authorises **effect**. Different questions, both
legitimate. Three things stop it *feeling* like double-confirmation:

1. **Make the preview card read as a receipt, not a new question.** `summarise()` currently emits
   `create chart "X"`. Make it
   `Unique patients by facility, last 12 months → new chart "Patients by facility"`
2. **The common path has no card at all.** A single `create_chart` with no `dashboard_id` lands on
   `touched = 1` → `RISK_LOW` → auto-applied. So "ask once, chart appears" is one interaction. A
   `create_dashboard` with five charts gives one preview card for the whole dashboard. That shape
   emerges for free from the existing risk rules — do not disturb it
3. The prompt rule above

### 5.6 Testability

`conftest.py`'s `FakeAnthropic` already has `queue_tool_use`, so a round trip is two `run_turn` calls:

```
Turn 1: queue_tool_use("ask_user", {...})
  assert a `clarification` event, stop_reason == "awaiting_user"
  assert one pending Clarification, and NO Message(role=TOOL)   <- the deferral

Turn 2: queue_tool_use("create_chart", {...}); queue_text("Built it.")
  run_turn(user_message="facility_name")
  assert the newest Message is role=TOOL with the matching tool_use_id
  assert fake.last_request.messages[-1] is that tool_result     <- the wire shape
```

That last assertion is load-bearing: it proves the model resumes its own tool call rather than seeing
a fresh user turn. Negative tests: `ask_user` beside another tool; after a write; twice in a turn;
7 options; 4 questions. Plus one truncation test: a conversation long enough that the pending pair
straddles the history window, asserting the `tool_use` survives and the answer still reaches the
model.

**The invariant test, which covers §5.7's third path.** One parametrised test over both pending
kinds — a pending `ask_user` and a pending preview — asserting that a turn started by *any* of
`{a chip answer, free text, a resume request, an unrelated new message}` produces a history whose
every `tool_use` has a matching `tool_result`, and never a request the API would reject. Cheapest
form: a helper that walks `fake.last_request.messages` and pairs ids, asserted in each case. Then the
specific ones:

- typed message against a pending **preview** → `superseded` tool result, `ActionRecord` `CANCELLED`,
  and the typed message present as a user turn *after* it
- a late `action_result` POST for that now-cancelled record → refused by the §9.1(5) state machine,
  and **no** second turn started
- a `{"resume": true}` request with no pending anything → 400, not a turn with an empty user message
- two concurrent turns on one conversation → the second gets 409

### 5.7 Preview cards resume the same way — one wait-for-a-human mechanism, not two

§5.1 rejects block-and-poll for clarification. **The same reasoning applies to the `action_preview`
card, which is also waiting for a person**, so previews use the same machinery. This is a *removal*:
the codebase ends up with one deferred-resume path rather than one deferred path and one polling
path.

**The mechanism, unchanged from §5.1:** emit the `action_preview` event, persist the assistant
message carrying the pending `tool_use`, end the turn with `stop_reason="awaiting_user"`, and write
**no** tool result. When the panel executes on Apply and POSTs to `/api/action-result/`, that outcome
becomes the deferred `tool_result` — the same pending-check in `run_turn`, and the panel follows its
report with a continuation request exactly as a clarification chip does. Cancel resolves the same
way, with a `cancelled` result.

**Why it matters here specifically.** The `create_dashboard` preview is the one place a human
decision sits mid-chain, and it is **functionally load-bearing**: the subsequent
`make_full_width`/`kpi_row` operations need the dashboard id the Apply produces. So "skip the wait on
previews" would break the chain, while blocking and hoping an executive clicks inside the wait window
is a race with a timeout — and if it loses, the model receives `pending`, cannot lay out the
dashboard, and ends with *"press Apply and then ask me to arrange it"*: the half-finished flagship
flow §13 names as the worst outcome. Deferring instead makes *"ask → build → one receipt → Apply →
layout"* work at human speed with no cliff, and previews survive a reload for free.

**What stays synchronous.** Low-risk auto-applied writes keep the short `_await_action_outcome`
wait — the browser answers in a couple of seconds and the chaining benefit is real there. That is
also the common single-`create_chart` path from §5.5(2), which must stay a one-interaction flow.

**The third path: the user replies instead of applying. This is the one that breaks the API.**
`ask_user` has three exits and the design covers all of them, because the pending-`Clarification`
check turns *whatever the user sends next* into the tool result — chip, "Something else…", or free
text. A deferred preview as described above has only **one** stated resume trigger, the
`action_result` POST, and that is not enough. Walk the likely case: the executive reads the receipt
card, applies nothing, and types *"actually add malaria positivity too."* That message reaches
`run_turn` while the newest assistant message ends in an **unresolved `tool_use`** — the exact state
§5.1 identifies as rejected outright by the Messages API. The turn 400s before the model is reached.
This is a first-week-of-the-pilot event, not an edge case, and unlike the clarification path nothing
currently catches it.

Close it with the same machinery, one branch in the same pending-check:

- Resolve the dangling `tool_use` with
  `{"status": "superseded", "note": "The user replied instead of applying this. It was not executed."}`
- Transition the `ActionRecord` to `CANCELLED` (which §9.1(5)'s state machine must permit from
  `PROPOSED|APPROVED`, and must then refuse a late `action_result` POST against — the browser may
  still send one)
- Mark the card cancelled in the rail rather than leaving it pending (§8.9)
- **Then** append the typed message as a normal user turn, so the model sees the superseded result
  followed by the new instruction and simply carries on

So the invariant to state plainly, and to test: **`run_turn` never begins a turn with an unresolved
`tool_use` in history.** Every pending `tool_use` — clarification or preview — is resolved by the
first thing that happens next, whatever that is. Three exits per pending call: answered/applied,
cancelled, superseded.

**The continuation request needs a declared shape.** After Apply, the panel POSTs `action_result` and
then asks the loop to continue — but `chat` requires a non-empty `message` and 400s otherwise
(`chat.py:78`), and the resume branch deliberately writes no user message. Pick one and write it
down, because leaving it implicit means the panel author invents it: accept
`{"resume": true, "conversation_id": …}` and move the empty-message check after the resume branch.
That is the honest shape — there is no user message — and it keeps `MAX_MESSAGE_LENGTH` and the
empty-message error intact for real messages. Same endpoint, same SSE contract, one extra branch.

**One concurrency guard, ~5 lines, Phase 1.** Short-lived turns dissolve most of the old concurrency
worry, but one race survives by construction: the panel's post-Apply continuation and a message the
user typed a moment earlier can both hit `run_turn` for the same conversation. Add a per-conversation
in-flight marker and return **409** while a turn is streaming. That makes the interleaving impossible
rather than unlikely, and it is cheaper than reasoning about two turns racing to resolve the same
`tool_use`. Do it in Phase 1 beside the `action_result` state machine, since both are guarding the
same record.

**On the nginx argument — do not repeat it.** Earlier drafts justified this partly by
`proxy_read_timeout`. That justification is false and was checked:
`deploy/nginx/lifedata.conf` already sets `proxy_read_timeout 300s` explicitly on the
`/lifedata-ai/` location (and on `/`), the Django container runs `runserver` with no worker timeout,
and the panel's `fetch()` has no timeout — only an `AbortController` for Stop. Nothing in the chain
sits at 60 s. The case for this change is reload-survival and one mechanism instead of two; it does
not need a timeout story.

**But there is a real timeout hazard nearby, and it belongs in the runbook.**
`deploy/django/Dockerfile` says *"Swap for gunicorn when this stops being a POC."* Gunicorn's default
sync-worker timeout is **30 s**, which would kill **every** build turn, not just previews. Whoever
makes that swap must set `--timeout 300` and a threaded or async worker class. Record it beside the
nginx conf, not in this section.

**Sequencing.** Land `ask_user` on the deferred path first (Phase 3) and prove it with §5.6's tests;
extend previews onto it in **Phase 5**, when the panel is already being rebuilt against the final
event set. The seam this design has — the truncation and resume-branch issues in §5.2 — is worth
proving on one tool before ~10 write tools depend on it.

---

## 6. Executive-grade dashboards without recipes

### 6.1 Wire up the layout compiler

- **Delete `build_position_json`** (see §2.2)
- `CreateDashboardTool`: `compile_operations(None, [{"operation": "add_chart", …} for each chart])`
- `AddChartToDashboardTool`: **honour `placement`**, which the schema has declared all along
- `RemoveChartFromDashboardTool`: use `{"operation": "remove_chart"}` rather than rebuilding, so
  tabs and manual layout survive
- **Fix §2.4:** `create_chart` with a `dashboard_id` must also emit a dashboard `PUT` writing the
  layout. That needs an id that does not exist yet — so carry `position_json` in the envelope as a
  **nested object** and mark the call `{"json_encode": ["position_json"]}`, letting the panel
  substitute the placeholder as a *value* and serialise afterwards. Putting a placeholder inside an
  already-serialised JSON string would force string surgery on signed content, which is not
  acceptable
- **Verify** whether `meta.uuid` is still required once the relation exists. One live test settles it

Payoff: `make_full_width`, `kpi_row` and `equalize_row` become reachable on a freshly built
dashboard — precisely the vocabulary an executive layout needs — and 59 existing tests already cover
the compiler.

### 6.2 Number formats, titles, KPI tiles

- Phase 2's `d3format` per saved metric gives every chart correct formatting with no model knowledge
  of d3 syntax, and kills `COUNT_DISTINCT(patient_id)` axis titles
- Add `number_format` to the params schema and **verify which key `big_number_total` honours in
  6.1.0**. The metric's `d3format` is the backstop either way
- `big_number_total` is allowlisted and validated but has never been produced. Nothing is missing
  except a reason to use it — which the layout policy supplies

### 6.3 Layout policy in the prompt, not a template

```
# What an executive dashboard looks like

Top to bottom, always in this order:
  1. A row of 2 to 4 single-figure tiles (big_number_total), one saved metric
     each, no grouping. This row is not optional.
  2. One full-width trend over time (echarts_timeseries_line, x_axis = the
     dataset's time column, time_grain_sqla = P1M).
  3. One or two breakdowns side by side (echarts_timeseries_bar,
     x_axis = a dimension, no groupby).
  4. A table last, if detail is wanted at all.
Every dashboard gets a date filter. Titles are plain English and under 60
characters — "Unique patients", never "COUNT_DISTINCT(patient_id)".

Build the tiles first, pass them to create_dashboard as chart_ids, then use
make_full_width on the trend. When finished, call review_dashboard_design and fix
what it reports BEFORE telling the user you are done.
```

### 6.4 `review_dashboard_design` — now load-bearing

With recipes gone, this replaces the recipe's determinism, so implement it properly: KPI row present
and at top; mixed widget heights in a row; more than 8 slices in a pie; missing number formats;
duplicate charts; titles over 60 chars; redundant legend on a single-series chart.

It is a pure function over `position_json` plus the chart list — cheap, deterministic, fully
unit-testable. Every finding maps onto an action the model already has. It is also already a dead
entry in the panel's label table, so implementing it retires that inconsistency.

### 6.5 Native filters and cross-filtering

`AddNativeFilterTool` has never run, and reading `_native_filter_config` shows at least four
problems: `filter_type: "time_grain"` falls through to a value select; no `type: "NATIVE_FILTER"`
key; no `chartsInScope`/`tabsInScope`; `target_chart_ids` ignored so scope is always all charts.

**Do not guess the shape.** Create one native filter by hand in the Superset UI,
`GET /api/v1/dashboard/{id}`, and diff `json_metadata.native_filter_configuration` against what the
tool emits. Twenty minutes, and it is the only honest way.

Then add `cross_filters_enabled: true` on dashboard creation — one line, and clicking a facility bar
to filter everything else is a disproportionately large perceived-quality win.

### 6.6 Chart-type selection

A decision table keyed on the *question shape*, not on viz names:

```
one figure for a period               -> big_number_total (one metric, no groupby)
a measure over time                   -> echarts_timeseries_line (time_grain_sqla P1M)
a measure across a dimension          -> echarts_timeseries_bar (x_axis = dimension, NO groupby)
parts of a whole, at most 6 categories-> pie
many columns, or detail               -> table

Never put the same column in both x_axis and groupby — Superset rejects the query
("Duplicate column/metric labels").
`bar` is not a registered plugin in this build; the bar chart is
echarts_timeseries_bar, and it handles a non-time x-axis perfectly well.
```

That last anti-rule is currently *silently repaired* after burning five of the first eight charts.
Keep the repair as a backstop, but state the rule so the model stops emitting it.

### 6.7 Write-count consequences of dropping recipes

A dashboard is now ~5 `create_chart` + 1 `create_dashboard` + 1 filter + 1–2 layout ops ≈ **9 write
actions**, each a browser round trip:

- `LIFEDATA_AI_ACTION_WAIT_SECONDS` **stays at 30.** §5.7 removes the reason to raise it: the only
  wait that depended on human reaction time — the preview card — no longer blocks. What remains
  behind this setting is the auto-applied path, where the browser answers in seconds and 30 s is
  already generous
- `LIFEDATA_AI_MAX_WRITES_PER_TURN` 10 → **14** (nine is uncomfortably close to a cap whose
  overshoot behaviour is a half-built dashboard). Note that with §5.7 a previewed build spans two
  turns, so `writes_proposed` resets across the Apply — this cap bounds a *turn*, not a dashboard,
  and the §6.4 review is what catches a partial result
- **Exclude awaited browser time from the turn deadline.** `run_turn` sets
  `deadline = now + TURN_TIMEOUT_SECONDS` and then passes `timeout=remaining` into
  `client.messages.stream` (`loop.py:397`). So every second spent in `_await_action_outcome` is
  subtracted from the *next model call's own* timeout — nine round trips can hand the SDK a
  three-second timeout mid-build, surfacing as a provider timeout that looks like an Anthropic fault.
  Measure each awaited interval and push the deadline out by it (~3 lines), so the budget bounds
  *model* work, which is what it was for. Otherwise turn timeouts track network quality, the worst
  possible failure distribution for a Ugandan facility pilot
- **Progress feedback is no longer cosmetic** — nine round trips behind a static panel reads as a
  hang. Promoted to Tier 1. Emit the heartbeat from *inside* `_await_action_outcome`'s poll loop: it
  currently `time.sleep(0.4)`s in a generator that yields nothing, so the strip in §8.3 and the SSE
  keepalive are the same three lines

---

## 7. Making the model build, not describe

1. **The catalogue removes the excuse.** Today the model *must* call `list_datasets` → `get_dataset`
   before it can legally name a column, and every hop invites a description instead of a chart
2. **Fix the false copy users actually see.** The intro still says *"This copilot reads; it does not
   change anything yet."* The product is telling users it cannot do the thing. Replace with
   *"Ask for a chart or a dashboard and I'll build it. You approve anything that changes Superset."*
3. **Add a `# How to build` section.** `_STYLE` says "ground every claim in a tool call" and nothing
   about acting, while `FULL_CAPABILITIES.cannot[0]` — *"Execute the change yourself… never claim an
   action succeeded"* — is easy to over-read as "don't act". State explicitly: *"When the user asks
   for a chart or a dashboard, build it. Do not describe how one could be built, do not offer, and
   do not ask permission — the panel handles authorisation. Say what you built in one sentence, with
   the interpretation you used."*
4. **Clarification changes the failure mode from guessing to asking**, which is what makes rule 3
   safe to state that forcefully
5. **`review_dashboard_design` lets the model check its own work** before claiming done
6. **Shrink the viz allowlist to what is verified** (§10) — fewer options, more reliable selection

---

## 8. UI/UX, prioritised by user-visible impact

### Tier 1 — the difference between unusable and fine

**8.1 Prefer the server's summary — one line, fixes six things.**
The panel recomputes a coarse label from `TOOL_LABELS`, ignoring the specific `data.summary` the
server already sends on every `tool_use` frame (e.g. *"inspecting dataset 2"*). Change to
`data.summary || name.replace(/_/g," ")` and **delete `TOOL_LABELS`** — it is a byte-for-byte fork
of the server's table, labels **none of the 12 write tools**, and holds a dead
`review_dashboard_design` entry.
Then fix the retry line: the loop emits a `_retry` `tool_use` with a good summary but **never a
matching `tool_result`**, so it is force-failed red. Emit the result after the backoff.

**8.2 Streaming-safe markdown renderer.**
One mutation site (`body.textContent += delta`); deltas are raw token chunks so **partial markdown is
guaranteed**.

- Keep `var raw = ""` per bubble; `appendText` appends to it and `isEmpty()` reads it — the raw
  accumulator preserves the hidden coupling to the "returned no text" check
- Coalesce on `requestAnimationFrame`; each tick clears and rebuilds from `raw`. **Full re-render per
  frame is correct**: an answer is a few KB, and it makes partial markdown trivially right — you
  never reconcile a half-parsed fence, you re-parse the buffer
- Unterminated *inline* renders literally; unterminated *fence* renders as an open code block
- Deliberately small subset: paragraphs, `#`–`###`, one list level, bold, italic, inline and fenced
  code, blockquote. **No links, no tables, no HTML passthrough** — one less injection surface
- Built with the existing `el()` helper. **Zero `innerHTML` preserved**
- CSS: move `white-space: pre-wrap` off `.ld-msg` onto `.ld-msg--user` only
- Extract as `static/panel/markdown.js` and expose it so Playwright can drive ~25 cases via
  `page.evaluate` — **including every prefix of a sample answer**, which is the streaming-safety test

**8.3 Progress feedback.** No `Date.now()` or `setInterval` exists anywhere. Add a status strip above
the composer: `Working · step 3 · 42s`, plus `Creating chart 3 of 5` counted from
`action_envelope`/`action_preview` events. Two-minute turns are normal; silence is not an option.

**8.4 Page-aware, submitting suggestion chips.** Today three static analyst-flavoured read-only
strings that only *fill* the textarea. This is the largest ease-of-use lever in the panel — it turns
a blank box into a menu. Key off `page_type`, phrase as executive intents, **submit on click**, and
regenerate after each turn so there is always a next step:

- lists: *"Build me an executive overview dashboard"* · *"Unique patients by district, last 12
  months"* · *"Which facilities have the slowest lab turnaround?"*
- dashboard: *"Add a KPI row"* · *"Add a date filter"* · *"Review this dashboard's design"*
- explore: *"Explain this chart"* · *"Make this monthly"* · *"Compare by facility level instead"*

**8.5 Stop survives the model speaking.** Stop lives in the thinking row, which is destroyed on the
first text delta — so once the model talks there is no cancel, even though builds commonly speak then
work for another minute. **Move Stop into the composer**: while busy, Send becomes Stop in the same
slot. Fixes it at the root.

**8.6 Composer stays enabled.** It is disabled for the whole turn, so during a two-minute build the
user cannot draft. Keep the Send state change and `aria-busy`; re-enable the textarea (`submit()`
already guards on busy). Also a prerequisite for the clarification free-text hatch.

**8.7 Conversation identity persists.** `conversationId` is never persisted, so **every Superset
navigation silently starts a new conversation** and the old thread is unreachable — while
`conversations.py`'s docstring claims the panel already uses these endpoints. Persist to
`localStorage`, rehydrate, replay through the new renderer, re-render any pending clarification, and
add **New conversation**. ~40 lines, entirely server-supported today.

**8.8 Undo button only where undo can work** — see §9.

**8.9 Use the dead actions rail for pending previews.** `.ld-actions` is styled and completely dead —
preview cards append into the transcript and scroll away. Mount **pending** previews there so Apply
is always reachable above the composer; move outcomes into the transcript on resolution. An executive
must never scroll to find the approve button.
Under §5.7 a pending preview outlives the turn, so the rail must **rehydrate on load** from the same
`conversation_detail` payload that carries `pending_clarification` — a card that survives on the
server but not on screen is worse than one that never persisted.

**8.10 Copy and CSS bugs.** `--ld-muted` → `--ld-text-muted` (two invalid declarations); add the
missing `.ld-action.is-applied` rule; fix the intro copy. Keep the "acts as you" badge — a genuine
trust signal — and the "Aggregates only" footnote, still true.

**8.11 Make the applied row pay off.** Link to the created object using the returned id —
*"Open 'Lab turnaround by facility'"*. "The thing exists, here it is" is where the product earns its
keep.

### Tier 2

**8.12 History UI** — list, load, delete. Fully implemented server-side already.
**8.13 Copy-to-clipboard** — trivial with the raw accumulator; copy `raw`, not the rendered DOM.
**8.14 Retry / regenerate** — `addError` already supports an action button, used only for "Reload
page". Wire it for `anthropic_error`, `turn_timeout`, `tool_round_limit`.

### Tier 3 — accessibility and responsiveness

**8.15** Remove `role="alert"` from error cards — nested inside an `aria-live` region they announce
**twice**.
**8.16** `aria-relevant="additions text"` re-announces every completed tool line. Use
`additions`, mark tool lines `aria-hidden`, and carry activity in one status region — which dovetails
with 8.3.
**8.17** Tap targets: icon buttons are 26×26; go to a 44×44 hit area.
**8.18** Add `Alt+L` to open. **Deliberately no focus trap** — this is a persistent non-modal
complementary region, and trapping focus would break tabbing back into Superset, which is the whole
point of a copilot beside the app. Escape-to-close with focus restoration is already correct. Record
as a decision, not an omission.
**8.19** Responsiveness: **not one `@media (max-width)` query exists**. Add a bottom-sheet layout
under 640px, hide the badge under 420px, cap the actions rail under 600px height.
**8.20** Widen to `min(440px, 100vw)`; ideally a drag handle persisted to `localStorage` — ~25 lines
for a disproportionate perceived-quality win.

---

## 9. Undo, deletion-as-inverse, and ownership

### 9.1 Fix undo properly — Phase 1, first thing

1. **The `TypeError`** (§2.6)
2. **The response contract.** Make the undo response **byte-shape-identical to a write tool's return
   value** — `{action_id, action_type, risk, summary, envelope, calls, warnings}` — so
   `executeAction` needs no special case. That was clearly the intent and it got lost
3. **The audit corruption.** Move the `UNDONE` transition into `action_result`, firing only when the
   undo reports `executed`
4. **Hide the button where undo cannot work.** `action_result` already returns
   `undoable: EXECUTED and snapshots.exists()` and the panel throws the response body away. Return
   it and gate the button. ~6 lines using a field the server already computes — and today
   `Snapshot.objects.count()` is 0, so **every** Undo button is guaranteed to fail
5. **An `action_result` state machine, which 9.1(3) makes load-bearing.** The view today accepts any
   reportable status against any current status, any number of times: it assigns
   `action.status = _REPORTABLE[reported]` with no check on what the record already holds
   (`action_result.py:64`). Enforce `PROPOSED|APPROVED → EXECUTED|FAILED|CANCELLED` and reject a
   report against an already-terminal record (409). Once the `UNDONE` transition moves into this
   view, a duplicate or late report can corrupt **two** records instead of one. This is also the
   resume trigger for §5.7, so a replayed POST must not restart a finished turn — and it is what
   refuses the late report for a preview the user already superseded by typing (§5.7), where the
   browser may still POST an outcome for a record the loop has cancelled

### 9.2 Deletion-as-inverse — but not behind the Undo button

Executives create things, so *"I didn't want that"* must be answerable. But it must be a **distinct
affordance**:

- Undo restores state; deleting is destructive and *itself* unundoable
- The chart may already be on another dashboard, or a colleague may have edited it
- Silently redefining "Undo" as "Delete" is the sort of thing that ends a pilot

Design:

- `POST /api/undo/` gains `mode: "restore" | "delete"`, defaulting to `restore` when a snapshot
  exists and `delete` when the record was a creation. Both return the same `calls` shape
- **`delete_chart`/`delete_dashboard` stay out of the tool registry.** The model can never propose a
  deletion — strictly better than adding two destructive tools. The human gets the affordance; the
  model does not get the capability
- Panel: for creations the applied row offers **"Delete this chart"** in preview styling, requiring a
  second confirming click and naming the object. For dashboards: *"Delete dashboard (charts are
  kept)"*
- Server-side preconditions before signing: re-check ownership; refuse if `changed_by` is not the
  caller; refuse if the chart now belongs to a dashboard outside the original action's payload
- **Prerequisite:** `WriteTool.run` must set `ActionRecord.superset_object_type` (§2.6)
- Keep `snapshot_target() → None` for creations — correct, and the button now disappears via 9.1(4)

### 9.3 Ownership enforcement

Today ownership only picks low-vs-preview risk; a non-owner action still proceeds once confirmed, and
`UpdateChartTool` has no ownership check at all.

**Put the hard refusal in the write tools, not the validator.** The validator is deliberately
network-free, and `UpdateChartTool` already sets the precedent with identical reasoning: *"the
validator has no network access, so it can only police the dataset the payload claims… Close that
here, where we can look."* Ownership is the same shape of problem.

- `WriteTool._require_ownership(context, object_type, object_id)` raising
  `ToolError(code="not_owner")`, reusing the existing casefolded owner comparison
- Drive it from `ownership_target(payload)` whose **default is `snapshot_target`**. Not a
  coincidence: any action with prior state worth snapshotting is by definition modifying an existing
  object — exactly the set needing a check. One default covers every current tool **except one**
- **`create_chart(dashboard_id=…)` is the exception, and §2.4's own fix creates it.** Once that call
  also emits a dashboard layout `PUT`, it modifies an existing dashboard — while
  `CreateChartTool` declares no `snapshot_target` at all, so the base returns `None` (correctly: the
  *chart* has no prior state). Result: the one creation-flavoured tool that mutates someone else's
  object would bypass **both** the ownership check and the dashboard snapshot. Fix: when
  `dashboard_id` is present, `create_chart`'s ownership *and* snapshot target is the **dashboard**.
  The chart is new; the layout is not. One override, plus its row in the §10.2 table — this is
  exactly the shape of hole that table exists to catch
- `add_chart_to_dashboard`: check the **dashboard** only. Superset's own permissions will reject
  touching someone else's chart, and the panel already surfaces that as a clean 403
- Keep the risk rule as a second layer
- **Verify creations end up owned.** Enforcement is worthless if `POST /api/v1/chart/` leaves
  `owners` empty. Check one; if empty, add `"owners": [me["id"]]` to the POST body

### 9.4 The prerequisite, stated plainly

**The pilot currently runs as a single Superset `admin` account.** With one shared admin, ownership
is a no-op *and* Admin bypasses Superset's own object permissions.

> Per-executive Superset accounts with a **non-Admin** role are a **prerequisite** for the
> create-and-modify-your-own boundary to be real.

Add a `create_pilot_users` command (or a documented runbook) creating one account per participant
with a custom role holding `can_write` on Chart and Dashboard plus datasource access to the five
`rpt_*` datasets, and **not** Admin. Ship the enforcement regardless — it still stops the copilot
editing a colleague's chart once two people share a role — but do not claim the boundary holds until
the accounts exist.

### 9.5 Three security one-liners, deferred through three planning cycles — do them in Phase 1

Each is a handful of lines and each has now been dropped from two successive plans without a listed
decision. They are here so the decision is explicit.

- **Refuse to boot on the dev `SECRET_KEY` when `DEBUG=False`.** `settings.py:46` defaults
  `SECRET_KEY` to `"dev-insecure-orchestrator-key"` and line 47 defaults `DEBUG` to `True`. Envelope
  signing hangs off that key, so a production-shaped deployment that forgets the environment variable
  signs every action with a value published in the repository. Raise `ImproperlyConfigured` when
  `not DEBUG` and the key is the default. Three lines.
- **An `Origin`/`Referer` check in the middleware** on the state-changing endpoints. `action_result`
  is `@csrf_exempt` by design — identity comes from the verified Superset session cookie, not a
  Django session — which means a same-site cookie is the only thing standing between a foreign page
  and a POST. The surface grows again with `/api/answer/` and, under §5.7, `action_result` now
  *resumes agent turns*. Check the header in `SupersetIdentityMiddleware.__call__`, where the
  identity is already being resolved.
- **`views/health.py` should probe `SELECT 1`.** It runs `SELECT postgis_version()` (`health.py:17`)
  and returns 503 with `status: "degraded"` on any non-PostGIS database — including the `postgres:16`
  image §10.4 deliberately chooses for the unit job. Make the liveness probe `SELECT 1` and report
  PostGIS as an optional detail, or the first person to run the container against plain Postgres
  burns an hour on a red herring.

---

## 10. Test and CI strategy

### 10.1 Fix the 14 stale tests — Phase 0, before anything else lands

All 14 are stale assumptions, not regressions. Each encodes a Phase-1 truth that deliberately
changed. Fix expectations, never weaken assertions.

| Group | Fix |
| --- | --- |
| Registry grew 13 → 25 tools (7 tests) | Assert the **set of names** equals an explicit literal, so adding a tool is a deliberate test edit rather than a count bump. Rewrite the "no write tools" tests against `build_registry(read_only=True)` — exactly what that argument exists for — and add a positive `test_write_tools_are_registered` |
| `effort: high` (4 tests) | Read `settings.LIFEDATA_AI_EFFORT`. One test already does this correctly — copy it |
| Phase-1 prompt string | Pass `phase_capabilities=PHASE_1_READ_ONLY` explicitly; add a companion asserting `FULL_CAPABILITIES` *does* promise writes. Keeping the read-only path covered is right — it is a real deployment mode |
| `viz_type: "bar"` | Use a registered type; add a separate test pinning that `bar` is aliased *and* refused by the allowlist, so the behaviour is deliberate |

**One test deserves a refactor rather than a fix.** `test_loop_emits_no_event_the_panel_ignores`
regex-scrapes `loop.py` for `_event("…")`, which both false-fails on `action_envelope` and **cannot
see `action_preview`** (emitted via a conditional expression) — so it under-covers even once
"fixed". Replace with a declared contract: a `PANEL_EVENTS: frozenset` in a new `agent/events.py`,
asserted in `_event()` under `DEBUG`, with the test asserting the literal **and** that every name
appears in `panel.js`. That is also where `clarification` and `awaiting_user` get registered.

### 10.2 Close the write-pipeline vacuum

Zero tests exist for `superset_write.py` (12 tools), `actions/envelopes.py`, `views/action_result.py`
and `views/undo.py`. That vacuum hid the undo `TypeError` and findings §2.3–§2.6.

New `test_superset_write.py`, table-driven, one row per tool: valid payload → exactly one
`ActionRecord`, the expected `calls` list, envelope round-trip, declared snapshot/ownership targets.

**Plus `test_every_write_tool_is_covered`**: iterate `WRITE_TOOL_CLASSES` and assert each name
appears in the coverage table. This is the structural fix — tool #13 fails the suite until someone
tests it.

Regressions for each finding: `create_chart(dashboard_id)` emits a layout call (§2.4);
`update_chart` preserves pre-existing filters (§2.5); `create_chart(dashboard_id)` declares the
**dashboard** as both its ownership and snapshot target while plain `create_chart` declares neither
(§9.3); the `{"json_encode": ["position_json"]}` envelope round-trips with `position_json` as a
nested object, and `substituteNewId` recurses into it **before** serialisation (§6.1) — the one
envelope shape where placeholder substitution and signing interact.

New `test_views_undo.py` — **the first test is the currently-red one**: undo of an `update_chart`
with a snapshot returns 200 with `calls` (not `preview`). That single test catches both undo bugs.
Then: stale snapshot → 409; no snapshot → 409 with the "delete it instead" wording; another user's
action → 404; `GET` → 405; the original stays `EXECUTED` until the undo reports executed (§2.6);
`mode: "delete"` for a creation.

New `test_views_action_result.py`: status mapping, caller scoping (404 not 403), the `undoable` flag,
excerpt truncation, and the §9.1(5) state machine — a second report against an `EXECUTED` record is
refused, and a `cancelled` report cannot overwrite a terminal status.

### 10.3 Wire the browser tests in

They hold the **only** coverage of the write path and no runner invokes them: `testpaths` excludes the
directory, they are `def main()` scripts, and the `superset_live` marker is unregistered.

- `testpaths = ["lifedata_ai/tests", "tests_browser"]`; register `superset_live` and `live_model`
  markers; `addopts = "-m 'not superset_live and not live_model'"` so the default run is unchanged
- New `tests_browser/conftest.py` with a session-scoped fixture that **skips unless
  `LIFEDATA_LIVE=1`**, checks the stack responds and Chromium is installed. It must never run inside
  the django container — the panel is injected by nginx and must be exercised through the same origin
  a real user sees
- Convert each `main()` into a test, keeping thin `main()` wrappers so `demo.py` still works

### 10.4 Minimal CI

One workflow, three jobs:

**`unit`** — `postgres:16`, **not** PostGIS. `django.contrib.gis` in `INSTALLED_APPS` does not need
GEOS/GDAL at import; only the `postgis` *backend* does, and `DJANGO_DB_ENGINE` already exists as the
override for exactly this. Steps: `uv sync` → `ruff check` → `pytest -q`.

**`invariants`** — three greps, ~5 seconds, protecting the two load-bearing properties nothing
currently enforces:

- `superset_write.py` contains no `client.post(`/`.put(`/`.delete(` — its own docstring demands
  exactly this (*"If you are editing this file and reach for `client.post(...)`, stop"*) and nothing
  checks it
- `static/panel/*.js` contains no `innerHTML`, `insertAdjacentHTML` or `document.write`
- Every name in `PANEL_EVENTS` appears in `panel.js`

**`e2e`** — using `scripts/mock_anthropic_server.py` via `ANTHROPIC_BASE_URL`: compose up → seed →
`register_superset_datasets --sync-metadata` → `playwright install chromium` →
`pytest -m superset_live`. Deterministic, free, **needs no secret** (so fork PRs work), and still
exercises nginx injection, the panel, the SSE contract and the real write path.

**`live_model`** — manual, with the real key. **Its first test is a minimal smoke call**: one request
to `settings.LIFEDATA_AI_MODEL` with `output_config={"effort": settings.LIFEDATA_AI_EFFORT}` and a
two-token answer, asserting only that the model resolves and the parameter is accepted. Nothing CI
runs today touches a real model, so the entire `effort` narrative and the `claude-opus-5` string are
unverified by anything — the `e2e` job deliberately uses the mock server. This makes a model-string
or parameter regression fail in one second with a clear message, rather than at an executive's first
prompt. The rest of the job then exercises actual model behaviour.

---

## 11. Verification

Each phase ends with evidence, not assertion. **The lesson from this build is that the Superset API
accepts almost anything and only the browser reveals the truth** — three defects (missing
chart→dashboard relation, unregistered `bar` viz, duplicate column labels) all returned `2xx` and
only appeared in a screenshot.

1. Unit suite green in the container, `-rs` to prove nothing is silently skipped
2. `ruff check lifedata_ai` clean
3. Browser tests on the host, plus new cases for clarification, KPI cards, filters and undo
4. **Screenshot every generated dashboard and look at it.** Assert absence of *"Data error"*,
   *"no chart definition"* and *"not registered"*
5. **The executive script** — the real acceptance test. From a clean database, typing only plain
   language:
   - *"Build me a dashboard showing how our labs are performing"* → clarifying questions with
     choices, then a dashboard with a KPI row, a monthly trend, a breakdown and a date filter
   - *"Show me where our patients come from"* → district-level output, never points
   - *"Actually make that first chart bigger"* → the layout compiler runs
   - *"Undo that"* → it works

   No schema words, no dataset IDs, no column names anywhere. **If that script needs technical
   vocabulary, ease of use is not fixed.**

---

## 12. Deliberately not doing

| Not doing | Why |
| --- | --- |
| Recipes / `layout_presets` | Free-form was chosen. |
| Any dynamic-forms work | Out of scope. Optionally drop the two form tools from the registry — two lines, −2 tool schemas from the prefix, one failure surface gone, zero capability an executive wants. |
| A Python phrase resolver | Unbounded, and every miss is a confident wrong answer. Clarification removes the motivation (§4.2). |
| A focus trap | Wrong for a persistent non-modal side panel; would break tabbing back into Superset (§8.18). |
| TypeScript / esbuild migration | Moves none of the three scores. The markdown renderer is the only tricky module and Playwright covers it. **Revisit when the panel moves off nginx injection onto a Superset extension (§16)** — that migration brings a build step with it, and doing both at once is the only time the cost is already paid. |
| Promoting `encounter_month` / `ordered_month` | They exist and nothing uses them. The paved path is `x_axis` + `time_grain_sqla: P1M`. Two ways to express "monthly" is how you get two charts that disagree. Mark them `role: internal` — which also makes the `is_dttm` question moot. |
| `deck_polygon` — **drop it** | `rpt_patient_geography` carries *centroids*, and **centroids are points; `deck_polygon` needs polygon geometry**, so a choropleth is probably not renderable on this data. Sourcing district boundaries is a GIS task, not a copilot task. A bar or table of districts answers the actual question. Reword the two validator messages that currently recommend it. |
| `histogram_v2` — **drop it** | Unverified against 6.1.0, and no executive wants a histogram. The allowlist shrinks to five verified types, which also makes selection more reliable. |
| `apply_dashboard_theme` — **drop it** | It writes `json_metadata["theme"]`, but Superset 6 exposes theme *objects* at `/api/v1/theme/`, and nothing ever creates the three registry themes — so the ids correspond to nothing and it is almost certainly a silent no-op. A consistent `color_scheme` per chart delivers the coherence an executive notices, for free. |
| `refresh_chart` — **drop it** | It permanently sets `cache_timeout: 0` — a persistent config change mislabelled as a refresh. An executive never asks to bust a cache. |
| Cross-filtering beyond the one-line flag | The flag is cheap and high-value; a full event-routing framework is not. |
| Production hardening | Internal pilot. A gate before wider rollout. |
| Rewriting `_op_add_chart` | It already does what is needed — the bug was duplicating it (§2.2). |

Net effect on the tool surface: **12 write tools → 10**, and the viz allowlist → **5 verified types**.
Fewer, better-verified options make the model more reliable, not less capable.

### 12.1 Delete what the drop table orphans — negative complexity

A dropped tool leaves live policy behind it, and **a validator rule that can never fire is
misdirection for the next reader.** Each deletion below is part of the drop, not a follow-up:

- `apply_dashboard_theme` orphans the validator's `theme_allowlist` rule, `THEME_IDS` in the action
  schemas, and the registry's `themes:` section — plus their tests
- `refresh_chart` orphans its schema and summary entries
- `deck_polygon` and `histogram_v2` leave the two validator messages that **recommend `deck_polygon`**
  pointing at a tool that no longer exists. §12 already flags the rewording; do it in the same commit
  as the drop

The §10.4 invariant job is the natural place to notice regressions here later: a name in a schema that
matches no registered tool is a grep away.

---

## 13. Risks

| Risk | Mitigation |
| --- | --- |
| `PUT /api/v1/dataset/{id}` replaces the columns collection; the UI's "Sync columns from source" wipes curated labels | Sync command idempotent; runbook says re-run after any sync. CI cannot catch it. Accept. |
| Ownership is a no-op on one shared admin account | Stated as a prerequisite (§9.4); ship enforcement anyway as defence against the model's own mistakes. |
| Native filter JSON shape is guessed | Do not guess — diff a hand-built filter (§6.5). |
| No recipes ⇒ 9 write actions per dashboard; a mid-sequence failure leaves a half-built dashboard | Raise the per-turn write cap and stop the awaited time eating the turn deadline (§6.7); defer the one human-gated wait rather than racing it (§5.7); make progress visible (§8.3); `review_dashboard_design` detects a partial result so the model can finish it. |
| Deferred resume becomes the single point of failure for both questions *and* previews | It replaces two mechanisms with one, which is the point — but prove it on `ask_user` in Phase 3 with §5.6's tests, including the truncation case, before previews depend on it in Phase 5. The seam is history truncation and the resume branch's message writes (§5.2), both tested directly. |
| A pending `tool_use` is left unresolved and the next turn is rejected by the API before the model is reached | The stated invariant: **`run_turn` never begins a turn with an unresolved `tool_use`.** Three exits per pending call — answered/applied, cancelled, superseded (§5.7) — plus the parametrised pairing test in §5.6 that walks the wire request. The in-flight 409 removes the race that could produce a fourth. |
| Clarification becomes an interrogation and executives abandon | Hard schema caps (≤3 questions, ≤5 options), one call per turn, refused after a write, and "prefer a stated default over a question". **Measure it:** `Clarification` rows left `pending` with no answer are the abandonment metric, free from the table. |
| `ask_user` waits indefinitely and clutters | A cleanup command marking pending clarifications `abandoned` after ~7 days. Low priority. |
| Rewriting the panel's render path and deleting `build_position_json` with no VCS | **`git init` first** (§2.1). Non-negotiable. |
| Prompt-cache invalidation from putting the catalogue in the volatile tail | Keep static content first, and add the breakpoints that make ordering matter at all (§4.3). |

### On revisiting `effort=high`

`medium` was chosen from a real bisect: no tools + high fine; 13 tools + high `overloaded_error`;
25 tools + medium at 32k fine. Three things change that measurement — Phase 2 adds cache breakpoints
(so input volume per round drops), Phase 2 removes one to two tool rounds per build, and **the retry
loop postdates the original bisect**, so `high` may now be survivable where it wasn't.

**Ship the pilot on `medium`.** In Phase 7, run a 20-request bisect across
(medium, high) × (32k, 16k `max_tokens`) — the untested cell is `high` at lower `max_tokens`. If high
clears, make it a per-conversation opt-in, not the default. Do not put real users on a setting that
overloads on most requests; a half-built dashboard is worse than a slightly shallower one.

---

## 14. What moves each score

**Functionality 6 → excellent.** Undo works and has a real inverse including delete (§9);
`create_chart(dashboard_id)` produces a *visible* chart (§2.4); `update_chart` stops destroying
filters (§2.5); the layout compiler is finally reachable, so KPI rows, full-width trends and
`placement` work (§6.1); native filters and cross-filtering work (§6.5); `big_number_total` gets used
(§6.2); `review_dashboard_design` exists (§6.4); three previously inexpressible governed indicators
become chartable (§4.2); ownership is enforced rather than scored (§9.3); the write pipeline has
tests at all (§10.2).

**UI/UX 5 → excellent.** Markdown renders (§8.2); progress is visible through two-minute builds
(§8.3); Stop survives the model speaking (§8.5); the composer stays usable (§8.6); conversations
survive navigation and have history (§8.7, §8.12); the preview rail stops scrolling away and a pending
Apply survives a reload instead of orphaning the turn (§8.9, §5.7); tool
lines say what is happening (§8.1); and the CSS bugs, stale copy, tap targets, double-announcements
and total absence of media queries are all fixed (§8.10, §8.15–§8.19).

**Ease of use 3 → excellent.** The copilot **asks clarifying multiple-choice questions and only
builds once it has full context** (§5); the model knows every column and metric with a friendly label
before it acts (§4); *"by facility"* has a declared default, a clickable choice **and** a validator
refusal for the wrong column (§4.6); page-aware chips that submit turn a blank box into a menu
(§8.4); the panel stops telling users it cannot change anything (§7.2); numbers and axis titles read
as English rather than SQL (§6.2).

---

## 15. Critical files

| File | Role in this plan |
| --- | --- |
| `lifedata_ai/semantics/registry.yaml` + `loader.py` | The single source of truth every other change reads from |
| `lifedata_ai/agent/prompts.py` | Catalogue, how-to-build, how-to-ask, stable-prefix reordering |
| `lifedata_ai/agent/loop.py` | Cache breakpoints, the `ask_user` deferred-tool-result branch, `PANEL_EVENTS`, retry `tool_result` |
| `lifedata_ai/tools/superset_write.py` | Delete `build_position_json`, wire `compile_operations`, ownership preconditions, param merge, the `create_chart` layout call |
| `lifedata_ai/tools/interaction.py` *(new)* | `AskUserTool` |
| `lifedata_ai/static/panel/panel.js` + `markdown.js` *(new)* | Markdown renderer, clarification card, progress, Stop, persistence, actions rail, `undoable` gating |
| `lifedata_ai/views/undo.py` | The broken signature, the `calls` contract, the premature `UNDONE`, `mode: delete` |
| `lifedata_ai/views/action_result.py` | The state machine (§9.1(5)), and the resume trigger for deferred previews (§5.7) |
| `lifedata_ai/views/chat.py` | The `{"resume": true}` continuation shape and the per-conversation in-flight 409 (§5.7) |
| `lifedata_ai/agent/context.py` | `PAGE_TYPES` — renamed to match upstream's `Page` union (§16) |
| `lifedata_ai/middleware.py` | The `Origin`/`Referer` check (§9.5) |
| `deploy/nginx/lifedata.conf` | The mount point for the whole panel, and the file §16's migration retires. Already correct on SSE buffering and `proxy_read_timeout 300s` — **do not regress those**; a deliverable, not background |
| `lifedata_ai/management/commands/register_superset_datasets.py` | `--sync-metadata`, and the saved metrics that make three governed indicators chartable |
| `lifedata_ai/validation/layout.py` | Unchanged — `_op_add_chart` already does what is needed |

---

## 16. Upstream Superset alignment

Three items, taken from the upstream extension/SIP threads. **Nothing else from those threads enters
the pilot** — they describe a direction, not work this plan does.

**16.1 Rename `PAGE_TYPES` to match upstream's `Page` union.** `agent/context.py:33` declares
`{dashboard, explore, chart_list, dataset_list, sqllab, other}`, invented locally. Aligning now costs
one frozenset, one panel-side mapping and the `page_type` tests, and means the §16.3 migration is a
mount change rather than a vocabulary change as well.

SIP-214's `Page` union, as read from the SIP:

```
dashboard | dashboard_list | explore | chart_list | sqllab
query_history | saved_queries | dataset | dataset_list | home
```

Five of the six local values already match a member exactly (`dashboard`, `explore`, `chart_list`,
`dataset_list`, `sqllab`) — so this is not a rename so much as a **narrowing to the shared vocabulary
plus room to grow**: `dashboard_list`, `query_history`, `saved_queries`, `dataset` and `home` are
pages the panel currently lumps into `other` and could give real chips to (§8.4).

**`other` has no upstream counterpart, and that is fine — keep it.** It is the fallback
`PageContext.from_payload` degrades to for an unrecognised value, and its job is to absorb pages the
panel has not been taught, including future upstream members. Keep it as a documented local extra
rather than waiting for upstream to supply one; the comment at `context.py:30` already states exactly
that purpose, so it only needs a line saying the rest of the set is upstream's and this one is ours.
Re-check the union at implementation time if the SIP has moved on — one lookup, and the shape of this
item does not change either way.

**16.2 Wrap the panel bootstrap in log-and-render-nothing.** `panel.js`'s `start()` runs on
`DOMContentLoaded` inside injected HTML, so a throw during bootstrap happens *inside a page the user
came to use for something else*. A `try/catch` around `start()` that logs and returns keeps a copilot
defect from ever looking like a Superset defect. Five lines, and it is the same posture the loop
already takes toward provider errors.

**16.3 One roadmap line: migrate the mount from nginx `sub_filter` to a `core.chat` extension at the
next Superset upgrade.** Not pilot work. Recording it matters because it is the trigger for two
deferred decisions: the TypeScript/build-step migration (§12), and retiring the `sub_filter` injection
whose fragility the nginx conf already documents (`Accept-Encoding ""` is load-bearing and silent when
wrong). Until then the injection stays — it works, it is one origin, and it needs no Superset fork.
