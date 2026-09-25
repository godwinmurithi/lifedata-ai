# LifeData AI copilot — panel bundle

The browser half of the copilot (spec §7). nginx injects it into every Superset
HTML response:

```nginx
sub_filter '</head>'
           '<script defer src="/lifedata-ai/static/panel/panel.js"></script></head>';
```

so it runs inside Superset's own page, on the same origin, with the user's
session cookie already present. Superset itself stays an unmodified image.

| File | Role |
| --- | --- |
| `panel.js` | SSE client, page-context collector, chat UI, **and the write executor** |
| `markdown.js` | A small streaming-safe markdown renderer, loaded by `panel.js` |
| `panel.css` | Styles, injected as a same-origin `<link>` by `panel.js` |

**The panel writes.** Every change to Superset is executed here, by the browser,
with the user's own session cookie and CSRF token — the orchestrator signs a
decision and never performs one. That is what makes "the copilot can never
exceed your Superset permissions" a structural property rather than a promise.
`executeAction` is the security-critical function in this file; the two rules it
lives under are stated at its definition.

---

## Deliberate deviation: no build step

**Spec §7 asks for TypeScript compiled with esbuild. This is plain ES2020 with
JSDoc type annotations and no build step.**

Why:

* The stack today is Docker + Python + nginx. TypeScript would add node, npm, a
  lockfile, a build stage in `deploy/django/Dockerfile` and a "did you rebuild
  the bundle?" failure mode — for a single-file, zero-dependency script.
* Source is bind-mounted, so editing `panel.js` and reloading the browser is the
  whole edit cycle. A compile step would break that immediacy during the phase
  where the panel changes most.
* JSDoc `@param`/`@typedef`/`@returns` gives VS Code the same completion and
  hover types as `.ts` would, and `tsc --checkJs` can type-check this file
  today without compiling it.

What is given up: no compile-time type *enforcement* in CI, no minification, no
`const`/`let` block-scoping discipline enforced by a linter.

**Migration path back to TS + esbuild**, if the panel outgrows this:

The file is already split into the sections a TS port would become modules:
`sse.ts` (`createSseDecoder`), `context.ts` (`readPageContext`,
`watchRouteChanges`), `transport.ts` (`PanelError`, `getCsrfToken`,
`streamChat`), `ui.ts` (`el`, `icon`, `buildUi`, `createTranscript`) and
`panel.ts` (`start`). Each section is closed over nothing outside itself except
the config constants at the top, so the port is: cut each section into a file,
turn the JSDoc `@typedef`s into `interface`s, add `export`, and point esbuild at
`panel.ts` with `--bundle --format=iife --target=es2020`. No behaviour changes.

---

## Constraints this file lives under

* **No CDN loads.** Superset's Talisman CSP allows `script-src 'self'`
  (`deploy/superset/superset_config.py`). A same-origin bundle is precisely why
  the injection approach works at all; anything external is blocked.
* **No dependencies, no framework**, and no use of Superset's React internals,
  Redux store or emotion classes. Those are not a supported API and would break
  on a Superset upgrade. The panel appends exactly two elements to `<body>`.
* **`z-index: 980`, not `2147483647`.** antd — which Superset uses for modals,
  drawers and dropdowns — starts its overlays at 1000, so Superset's own drawers
  still open *above* the panel rather than being obscured by it, while the panel
  stays above Superset's sticky dashboard chrome.
* **No `innerHTML`, anywhere, including in the markdown renderer.** Model
  output is untrusted input by definition, and this panel sits inside a page
  holding the user's Superset session. `markdown.js` builds every node with
  `createElement` and `textContent`; the only SVG is built with
  `createElementNS`. `test_end_to_end.py` asserts that no HTML or code sink
  appears in either file, and `tests_browser/test_markdown.py` walks the live DOM
  to prove no rendered element ever carries an attribute.
* **No links in rendered markdown.** Deliberately unsupported. A copilot that
  can render `[click here](javascript:…)` into a page that already holds the
  user's session is a bad trade for prettier prose. Links, images, tables and
  HTML passthrough are all out; paragraphs, three heading levels, one list
  level, emphasis, code and blockquotes are in.
* **Deliberately no focus trap.** This is a persistent, non-modal complementary
  region beside the application. Trapping focus would break tabbing back into
  Superset, which is the entire point of a copilot next to the app rather than
  in front of it. Escape closes and restores focus, which is correct for this
  pattern. Recorded as a decision, not an omission.

---

## SSE contract consumed by the panel

`POST /lifedata-ai/api/chat/`

Request body (JSON):

```json
{
  "message": "what datasets are available for laboratory analysis?",
  "page_context": {"page_type": "dashboard", "dashboard_id": 3},
  "conversation_id": "optional-uuid-from-a-previous-message_start"
}
```

Request headers: `Content-Type: application/json`, `Accept: text/event-stream`,
`X-CSRFToken: <Superset token>`, cookies via `credentials: "same-origin"`.

Response: `text/event-stream`. Frames are separated by a blank line; every frame
carries a JSON `data:` payload. Comment lines (`: keep-alive`) are parsed and
discarded. **Event names the panel handles — anything else is ignored silently,
which is what lets Phase 2 add events without breaking a cached copy of this
file:**

| `event:` | `data:` payload | Panel behaviour |
| --- | --- | --- |
| `message_start` | `{"conversation_id": "<uuid>"}` | Stores the id (and persists it to `localStorage`) |
| `text_delta` | `{"text": "…"}` (`{"delta": "…"}` also accepted) | Appends to the raw buffer; the bubble re-renders on the next animation frame |
| `tool_use` | `{"id": "toolu_…", "name": "get_dataset", "summary": "inspecting dataset 2"}` | Adds an activity line and updates the status strip |
| `tool_result` | `{"tool_use_id": "toolu_…", "is_error": false}` | Marks that activity line done, or failed if `is_error` |
| `progress` | `{"phase": "applying", "waited_seconds": 6, "message": "…"}` | Updates the status strip. Doubles as the SSE keep-alive |
| `action_envelope` | `{action_id, action_type, risk, summary, envelope, calls}` | **Low risk — executes immediately** against Superset as the user |
| `action_preview` | same shape | Mounts an Apply/Cancel card in the actions rail. The turn ends here |
| `clarification` | `{clarification_id, tool_use_id, questions: [...]}` | Mounts a multiple-choice card in the transcript. The turn ends here |
| `message_stop` | `{"stop_reason": "end_turn"\|"awaiting_user", "conversation_id": "<uuid>"}` | Ends the turn cleanly |
| `error` | `{"message": "…", "code": "…"}` | Renders an inline error, with a Try again button for recoverable codes |

Notes for the orchestrator side:

* **`summary` is authoritative.** The panel renders `data.summary` for the
  activity line. It used to keep its own label table, which was a byte-for-byte
  fork of the server's that labelled none of the write tools; that table is
  deleted. A frame with no summary degrades to the tool name with underscores
  replaced by spaces.
* `tool_result` is matched to its `tool_use` by `tool_use_id`, falling back to
  `id`, then `name`. Emitting a stable id is strongly preferred — and **every**
  `tool_use` must eventually get one, including the synthetic `_retry` line, or
  the panel force-fails it red at the end of the turn.
* **`stop_reason: "awaiting_user"` means the turn ended on purpose**, waiting for
  a person. The panel suppresses its "returned no text for this turn" guard for
  it: a turn that only asks a question correctly produces no text.
* A stream that ends **without** `message_stop` and without `error` is treated
  as a truncated connection and surfaced as "the response ended unexpectedly" —
  do not close the stream silently on the server.
* `proxy_buffering off` is already set for `/lifedata-ai/` in
  `deploy/nginx/lifedata.conf`; without it deltas arrive in one lump.

### Resuming: `{"resume": true}`

After the user presses Apply or Cancel, the panel reports the outcome to
`/api/action-result/` and then POSTs to `/api/chat/` with:

```json
{"resume": true, "conversation_id": "<uuid>"}
```

There is genuinely no user message. The loop writes a `tool_result` instead, so
the model resumes its own pending tool call rather than seeing a fresh user turn
— which is what lets it lay out a dashboard using the id the Apply just
produced. `MAX_MESSAGE_LENGTH` and the empty-message error stay exactly as
strict for a real message.

A clarification answer is **not** a resume: it is an ordinary message on the same
conversation, and the server turns it into the tool result.

### Executing an envelope

`calls` is an ordered list of `{method, path, body}`, optionally with
`json_encode`. Two mechanisms are worth knowing:

* **`__NEW_ID__`** stands in for an id that does not exist until an earlier call
  creates it. The panel substitutes the first call's returned `id` and touches
  nothing else in the signed body.
* **`json_encode: ["position_json"]`** names body keys Superset wants as JSON
  *strings*. They travel as nested objects so `__NEW_ID__` can be substituted
  structurally; putting a placeholder inside an already-serialised string would
  mean string surgery on signed content, which this panel must never do. The key
  list is inside the signature.

### Error handling

| Condition | What the user sees |
| --- | --- |
| `GET /api/v1/security/csrf_token/` → **401** | "Your Superset session has expired. Reload the page and sign in again." plus a Reload button. **Not retried** — verified finding §1 in `docs/superset-integration-findings.md`: this endpoint returns 401 on an expired session, so a retry loop would spin forever. |
| Chat endpoint **401** | Same session-expired surface |
| Chat endpoint **403** | "Superset refused this request for your account." |
| Chat endpoint **502/503/504** | "The copilot could not reach Superset (HTTP n). Try again shortly." |
| Any other non-200 | "The copilot returned an error (HTTP n)." plus a truncated body excerpt |
| `fetch` rejection (network/proxy down) | "Could not reach the LifeData copilot." |
| Mid-stream `error` frame | The frame's `message`, plus `code` if present |

Nothing fails silently: every path above ends in a visible `role="alert"` block
in the transcript.

### About `X-CSRFToken` on a Django endpoint

The orchestrator has no Django CSRF middleware (`lifedata/settings.py` documents
why: identity is the Superset session, not a Django one). The header carries
**Superset's** token because (a) obtaining it proves the caller holds a live
Superset session, and (b) it is the same cached token the Phase 2 write executor
must send to Superset. The token is fetched once per page session and cached;
the cache is dropped on failure so a retry after re-authentication succeeds.

---

## Page context

Recomputed on every route change — Superset 6 is an SPA, so `history.pushState`
and `replaceState` are wrapped (once, still calling the originals) to emit a
synthetic `lifedata:locationchange` event, alongside `popstate` and
`hashchange`.

Shape sent to the orchestrator:

```ts
{ page_type: "dashboard"|"explore"|"chart_list"|"dataset_list"|"sqllab"|"other",
  dashboard_id?: number, chart_id?: number, dataset_id?: number }
```

**The URL patterns below were inferred from Superset 6.1.0 routing, not read out
of its source. Re-verify them whenever the pinned version is bumped** (spec §10
asks for exactly this note); they are repeated in a comment above
`readPageContext` in `panel.js`.

| URL | `page_type` | Ids |
| --- | --- | --- |
| `/superset/dashboard/<id>/` | `dashboard` | `dashboard_id` when numeric |
| `/superset/dashboard/<slug>/` | `dashboard` | none — slugs are not sent as ids |
| `/superset/dashboard/p/<key>/` | `dashboard` | none — permalink, id unresolved |
| `/explore/`, `/superset/explore/`, `/explore/p/<key>/` | `explore` | `chart_id` from `slice_id`; `dataset_id` from `datasource_id` or the packed `datasource=<id>__table` |
| `/chart/list/` | `chart_list` | — |
| `/tablemodelview/list/`, `/dataset/list/` | `dataset_list` | — |
| `/sqllab`, `/superset/sqllab` | `sqllab` | — |
| anything else (including `/dashboard/list/`) | `other` | — |

Spec §7 also lists an edit-mode flag (`.dashboard--editing`), a selected-widget
hint and native filter state. **Those are deliberately not sent**: the
`page_context` schema belongs to the orchestrator and is validated strictly, so
unilaterally adding keys it does not declare would fail validation. Add them on
both sides together.

Page type also drives the **suggestion chips**, which submit on click rather than
filling the textarea. That is the largest ease-of-use lever in the panel: it
turns a blank box into a menu. They are regenerated after each turn so there is
always an obvious next step.

The panel does not mount on `/login` or `/logout` — nginx injects the script
there too, and there is no session to talk to yet.

---

## Accessibility

* Toggle is a real `<button>` with `aria-label`, `aria-expanded` and
  `aria-controls`; it is hidden while the panel is open, since the panel's own
  close button takes over that role.
* Panel is `role="complementary"` with an `aria-label`; when closed it is
  `inert` and `visibility: hidden`, so it is out of the tab order.
* Message list is `role="log"` with `aria-live="polite"` and
  **`aria-relevant="additions"`**, not `additions text`. Completing a tool line
  *rewrites* its text, so `text` re-announced every internal step; tool lines are
  now `aria-hidden` and activity is carried by one `role="status"` strip above
  the composer.
* Errors are **not** `role="alert"`. They already sit inside an `aria-live`
  region, and nesting one inside the other announced the same text twice.
* Clarification cards are real `<button>`s in DOM order inside a
  `role="group"` labelled by its question, so keyboard access is free and the
  relationship between a question and its options is stated rather than implied.
* Escape closes the panel and returns focus to the toggle; **Alt+L** toggles it
  from anywhere in Superset. Opening moves focus to the textarea; restoring the
  open state on page load deliberately does *not*.
* Icon buttons render at 28px with a 44×44 transparent hit area, so a mis-tap in
  a 13px header cannot close the panel mid-sentence.
* `:focus-visible` outlines throughout; animations respect
  `prefers-reduced-motion`.
* Under 640px the panel becomes a bottom sheet rather than a 380px drawer that
  would cover a phone screen entirely.

Light and dark are both styled via `prefers-color-scheme`. Superset 6 has its
own theme switch, but reading it reliably would mean touching Superset
internals, so the panel follows the OS instead.

---

## Verifying

```powershell
# served (200 expected; Django serves it via staticfiles in DEBUG)
curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/lifedata-ai/static/panel/panel.js
curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/lifedata-ai/static/panel/panel.css

# injected into Superset HTML
curl -s http://localhost:8080/login/ | Select-String "panel.js"
```

Then sign in at <http://localhost:8080/>, open any dashboard, and use the
"Ask LifeData" launcher at the bottom right. From the browser console,
`__lifedataCopilot.pageContext()` prints the context the panel would send for
the current route, and `__lifedataCopilot.open()` / `.close()` drive the panel.

## Tests

```sh
uv run pytest tests_browser/test_markdown.py     # no stack needed
uv run pytest tests_browser                      # needs docker compose up
```

`markdown.js` is exposed as `window.__lifedataMarkdown` precisely so Playwright
can drive it directly, in a blank page with no Superset, no login and no
orchestrator — a markdown regression then reports as a markdown regression rather
than as "the stack is down".

The test that matters is `test_every_prefix_of_an_answer_renders`. Deltas are raw
token chunks, so the renderer is handed **partial markdown on every frame of
every answer**: an unterminated `**`, a fence with no closing fence, a list item
cut mid-word. Rendering every prefix of a realistic answer is the only honest way
to prove that never throws and never produces markup.

The renderer never reconciles a half-parsed structure. Each bubble keeps a `raw`
accumulator; every animation frame clears the node and re-parses the whole
buffer. An answer is a few kilobytes, so a full re-render is cheaper than being
clever — and it makes partial markdown trivially correct, because an unterminated
run renders as literal text and the next frame fixes it.

`test_end_to_end.py` additionally asserts, from Python, that neither file
contains an HTML or code sink, and proves that guard still fires by injecting one
into a copy of the real source.
