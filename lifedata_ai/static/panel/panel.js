/**
 * LifeData AI copilot — browser panel (spec §7).
 *
 * nginx injects this file into every Superset HTML response (see
 * deploy/nginx/lifedata.conf), so it runs inside Superset's own page, on the
 * same origin, with the user's session cookie already present. That co-tenancy
 * dictates most of the decisions below:
 *
 *   * Zero dependencies and zero CDN loads. Superset's Talisman CSP allows
 *     script-src 'self' only (deploy/superset/superset_config.py); a bundle
 *     from anywhere else would simply be blocked.
 *   * Nothing here touches Superset's React internals, Redux store or DOM
 *     beyond appending two elements to <body>. Superset must stay an
 *     unmodified image, and its internals are not a supported API.
 *   * Model output is rendered without innerHTML, always. markdown.js builds
 *     every node with createElement/textContent; text arriving from an LLM is
 *     untrusted input by definition, and this panel sits inside a page holding
 *     the user's Superset session.
 *
 * DELIBERATE DEVIATION FROM THE SPEC: §7 asks for TypeScript compiled with
 * esbuild. This is plain ES2020 with JSDoc types and no build step, so the
 * panel ships without adding node/npm to the stack. See README.md in this
 * directory for the reasoning and the migration path.
 *
 * THE PANEL WRITES. Every change is executed HERE, by the browser, with the
 * user's own Superset session and CSRF token — the orchestrator signs a
 * decision and never performs one. That is what makes "the copilot can never
 * exceed your Superset permissions" structural rather than promised.
 *
 * Waiting for a person happens BETWEEN turns, not inside one. A clarification
 * card and an Apply card both end the turn with the model's tool call
 * unresolved; whatever the user does next resolves it. So both survive a page
 * reload, and both are rehydrated from `conversation_detail` on load.
 */

(function () {
  "use strict";

  // nginx uses `sub_filter_once on`, but a stray second injection (or a manual
  // console load while debugging) must not produce two panels.
  if (window.__lifedataCopilot) {
    return;
  }

  // --- Configuration -----------------------------------------------------

  var ORCHESTRATOR = "/lifedata-ai";
  var CHAT_URL = ORCHESTRATOR + "/api/chat/";
  var ACTION_RESULT_URL = ORCHESTRATOR + "/api/action-result/";
  var EXECUTE_MCP_ACTION_URL = ORCHESTRATOR + "/api/execute-mcp-action/";
  var UNDO_URL = ORCHESTRATOR + "/api/undo/";
  var CONVERSATIONS_URL = ORCHESTRATOR + "/api/conversations/";
  var STYLESHEET_URL = ORCHESTRATOR + "/static/panel/panel.css?v=20261103-redesign";
  var MARKDOWN_URL = ORCHESTRATOR + "/static/panel/markdown.js";

  /**
   * Superset's CSRF endpoint. VERIFIED FINDING (docs/superset-integration-
   * findings.md §1): this returns 401 when the session has expired, not a
   * retryable error. It is treated as terminal for the turn.
   */
  var CSRF_URL = "/api/v1/security/csrf_token/";

  var OPEN_STATE_KEY = "lifedata-ai.panel.open";
  var CONVERSATION_KEY = "lifedata-ai.panel.conversation";
  var THEME_KEY = "lifedata:panel-theme";
  var MAX_ERROR_DETAIL = 300;

  /**
   * Suggestions phrased as executive intents and keyed off the page.
   *
   * The largest ease-of-use lever in the panel: it turns a blank box into a
   * menu. The previous three were static, analyst-flavoured, read-only and only
   * *filled* the textarea, so a first-time user's first act was to read three
   * things they did not want and then type something else. These submit.
   */
  var SUGGESTIONS = {
    dashboard: [
      { label: "Update chart", prompt: "Help me update a single chart on this dashboard. Ask which chart and what change I want before making an edit. For a custom dashboard, use its individual chart settings and preserve the other charts and layout." },
      { label: "Summarize dashboard", prompt: "Summarize the dashboard I am currently viewing using its current source data. Give a concise overview, key numbers and main findings. Respect active filters and clearly identify missing data or capped results. Do not edit or republish anything." },
      { label: "Insights", prompt: "Analyze the dashboard I am currently viewing and give the most useful insights, differences and data-quality gaps supported by its current source data. Respect active filters, distinguish findings from possible explanations, and suggest practical follow-up questions. Do not edit or republish anything." },
    ],
    explore: [
      "Explain this chart",
      "Make this monthly",
      "Compare by facility level instead",
    ],
    chart_list: [
      "Build me an executive overview dashboard",
      "Unique patients by district, last 12 months",
      "Which facilities have the slowest lab turnaround?",
    ],
    dataset_list: [
      "Build me an executive overview dashboard",
      "What can I chart from the laboratory data?",
      "Malaria positivity by facility, last 12 months",
    ],
    dashboard_list: [
      "Build me an executive overview dashboard",
      "Build a laboratory performance dashboard",
      "Which facilities have the slowest lab turnaround?",
    ],
    home: [
      "Build me an executive overview dashboard",
      "Unique patients by district, last 12 months",
      "How are our labs performing?",
    ],
    other: [
      "Build me an executive overview dashboard",
      "Unique patients by district, last 12 months",
      "Which facilities have the slowest lab turnaround?",
    ],
  };

  /** Follow-ups offered after a turn, so there is always an obvious next step. */
  var FOLLOW_UPS = {
    dashboard: SUGGESTIONS.dashboard,
    other: ["Show that by district instead", "Make that monthly", "Put that on a dashboard"],
  };

  // --- Small helpers -----------------------------------------------------

  /**
   * @param {string} tag
   * @param {Record<string, any>} [attrs] `class`/`text` are special-cased; `on*` values are listeners.
   * @param {Array<Node|string>} [children]
   * @returns {HTMLElement}
   */
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value === null || value === undefined || value === false) {
          return;
        }
        if (key === "class") {
          node.className = value;
        } else if (key === "text") {
          node.textContent = String(value);
        } else if (key.indexOf("on") === 0 && typeof value === "function") {
          node.addEventListener(key.slice(2).toLowerCase(), value);
        } else {
          node.setAttribute(key, value === true ? "" : String(value));
        }
      });
    }
    (children || []).forEach(function (child) {
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  /**
   * Build an inline SVG without innerHTML, so no HTML string ever reaches the
   * parser from this file.
   * @param {string} pathData
   * @returns {SVGElement}
   */
  function icon(pathData) {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "16");
    svg.setAttribute("height", "16");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.8");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    var path = document.createElementNS(ns, "path");
    path.setAttribute("d", pathData);
    svg.appendChild(path);
    return svg;
  }

  /** @param {string} value @param {number} max */
  function truncate(value, max) {
    if (typeof value !== "string") {
      return "";
    }
    return value.length > max ? value.slice(0, max) + "…" : value;
  }

  // A raw tool name ("call_mcp_tool", "execute_sql") is implementation detail,
  // not something a user should read mid-conversation — it exposes the wiring
  // instead of what's actually happening. These rotate through whenever the
  // server hasn't sent its own summary, so the fallback never looks like a
  // function name that leaked into the UI.
  // What the status icon's label cycles through while a turn is in flight.
  // Deliberately generic — earlier versions surfaced the literal tool being
  // called ("inspecting dataset 2", "call mcp tool") as a line per step. That
  // narrated internals the user has no use for; a single rotating-verb
  // indicator (the same idea as Claude Code's own CLI spinner) reads as
  // "working" without it.
  var WORKING_PHRASES = [
    "Working",
    "Tinkering",
    "Generating",
    "Thinking it through",
    "Connecting the dots",
    "Crunching the numbers",
    "Consulting the data",
    "Piecing it together",
    "Double-checking",
  ];
  var workingPhraseIndex = 0;

  function nextWorkingPhrase() {
    var phrase = WORKING_PHRASES[workingPhraseIndex % WORKING_PHRASES.length];
    workingPhraseIndex += 1;
    return phrase;
  }

  /** Read/write panel-scoped localStorage without letting private mode throw. */
  function stored(key, value) {
    try {
      if (value === undefined) {
        return window.localStorage.getItem(key);
      }
      if (value === null) {
        window.localStorage.removeItem(key);
      } else {
        window.localStorage.setItem(key, value);
      }
    } catch (err) {
      // Private-mode storage failures are cosmetic; the panel still works, it
      // just forgets between navigations.
    }
    return null;
  }

  // --- SSE decoding ------------------------------------------------------
  //
  // EventSource cannot issue a POST, and the chat turn needs a JSON body, so
  // the stream is read from fetch() and the wire format is parsed here.
  // Frames are separated by a blank line; within a frame, `event:` names it and
  // one or more `data:` lines carry the payload (joined with newlines, per the
  // SSE spec). Lines starting with ':' are comments — Django or nginx may send
  // them as keep-alives, and they must not be mistaken for data.

  /**
   * @typedef {{event: string, data: any, raw: string}} SseFrame
   */

  /**
   * @param {(frame: SseFrame) => void} onFrame
   * @returns {{push: (chunk: string) => void, flush: () => void}}
   */
  function createSseDecoder(onFrame) {
    var buffer = "";

    function emit(block) {
      if (!block.trim()) {
        return;
      }
      var eventName = "message";
      var dataLines = [];
      block.split(/\r?\n/).forEach(function (line) {
        if (!line || line.charAt(0) === ":") {
          return;
        }
        var colon = line.indexOf(":");
        var field = colon === -1 ? line : line.slice(0, colon);
        var value = colon === -1 ? "" : line.slice(colon + 1);
        if (value.charAt(0) === " ") {
          value = value.slice(1);
        }
        if (field === "event") {
          eventName = value;
        } else if (field === "data") {
          dataLines.push(value);
        }
      });
      var raw = dataLines.join("\n");
      if (!dataLines.length && eventName === "message") {
        // A comment-only block: an nginx or Django keep-alive, not a turn event.
        return;
      }
      var data = null;
      if (raw) {
        try {
          data = JSON.parse(raw);
        } catch (err) {
          // A non-JSON payload is a backend bug, not a reason to kill the turn:
          // hand it on as raw text and let the handler decide.
          data = { raw: raw };
        }
      }
      onFrame({ event: eventName, data: data, raw: raw });
    }

    return {
      push: function (chunk) {
        buffer += chunk;
        var parts = buffer.split(/\r?\n\r?\n/);
        buffer = parts.pop() || "";
        parts.forEach(emit);
      },
      flush: function () {
        var remainder = buffer;
        buffer = "";
        if (remainder) {
          emit(remainder);
        }
      },
    };
  }

  // --- Page context ------------------------------------------------------
  //
  // URL patterns matched below were INFERRED from Superset 6.1.0 routing and
  // must be re-verified against the pinned version whenever it is bumped
  // (spec §10 requires exactly this kind of note):
  //
  //   /superset/dashboard/<id-or-slug>/      -> dashboard   (numeric id captured; slugs are not)
  //   /superset/dashboard/p/<key>/           -> dashboard   (permalink; id unknown until the SPA resolves it)
  //   /dashboard/list/                       -> other       (a list surface; the contract's enum has no dashboard_list)
  //   /explore/                              -> explore     (ids come from query params, see below)
  //   /explore/p/<key>/                      -> explore     (permalink form)
  //   /superset/explore/...                  -> explore     (legacy path, still redirected in 6.x)
  //   /chart/list/                           -> chart_list
  //   /tablemodelview/list/  or /dataset/list/ -> dataset_list  (the FAB path and its newer alias)
  //   /sqllab  or  /superset/sqllab          -> sqllab
  //   anything else                          -> other
  //
  // Explore query params, also inferred: `slice_id=<id>` for an existing chart,
  // and either `datasource_id=<id>&datasource_type=table` or the packed
  // `datasource=<id>__table` for the dataset. Both spellings are read because
  // Superset has used each at different points.
  //
  // Deliberately NOT sent: edit-mode flag, selected-widget id and native filter
  // state (spec §7 lists them). The chat endpoint's page_context schema is owned
  // by the orchestrator and validated strictly; adding keys it does not declare
  // would fail validation. See README.md.

  /**
   * @typedef {{page_type: string, dashboard_id?: number, chart_id?: number, dataset_id?: number}} PageContext
   */

  /** @param {string|null|undefined} value @returns {number|undefined} */
  function toId(value) {
    if (value === null || value === undefined || value === "") {
      return undefined;
    }
    var parsed = Number(value);
    return Number.isInteger(parsed) && parsed > 0 ? parsed : undefined;
  }

  /** @returns {PageContext} */
  function readPageContext() {
    if (window.__lifedataDashboardContext) return window.__lifedataDashboardContext;
    var path = window.location.pathname;
    var params = new URLSearchParams(window.location.search);
    /** @type {PageContext} */
    var context = { page_type: "other" };

    var dashboardMatch = path.match(/\/superset\/dashboard\/([^\/]+)\/?/);
    if (dashboardMatch) {
      context.page_type = "dashboard";
      if (dashboardMatch[1] !== "p") {
        var dashboardId = toId(dashboardMatch[1]);
        if (dashboardId) {
          context.dashboard_id = dashboardId;
        }
      }
      return context;
    }

    if (/^\/(superset\/)?explore(\/|$)/.test(path)) {
      context.page_type = "explore";
      var sliceId = toId(params.get("slice_id"));
      if (sliceId) {
        context.chart_id = sliceId;
      }
      var datasetId = toId(params.get("datasource_id"));
      if (!datasetId) {
        var packed = params.get("datasource");
        if (packed) {
          datasetId = toId(packed.split("__")[0]);
        }
      }
      if (datasetId) {
        context.dataset_id = datasetId;
      }
      return context;
    }

    if (/^\/chart\/list\/?/.test(path)) {
      context.page_type = "chart_list";
      return context;
    }
    if (/^\/dashboard\/list\/?/.test(path)) {
      context.page_type = "dashboard_list";
      return context;
    }
    if (/^\/(tablemodelview|dataset)\/list\/?/.test(path)) {
      context.page_type = "dataset_list";
      return context;
    }
    if (/^\/(superset\/)?sqllab\/history/.test(path)) {
      context.page_type = "query_history";
      return context;
    }
    if (/^\/savedqueryview\/list\/?/.test(path)) {
      context.page_type = "saved_queries";
      return context;
    }
    if (/^\/(superset\/)?sqllab(\/|$)/.test(path)) {
      context.page_type = "sqllab";
      return context;
    }
    if (path === "/" || /^\/superset\/welcome/.test(path)) {
      context.page_type = "home";
      return context;
    }
    return context;
  }

  /** @param {PageContext} context @returns {string} */
  function describeContext(context) {
    switch (context.page_type) {
      case "dashboard":
        if (context.dashboard_surface === "custom") return "Custom dashboard";
        return context.dashboard_id ? "Dashboard " + context.dashboard_id : "Dashboard";
      case "explore":
        if (context.chart_id) {
          return "Chart " + context.chart_id;
        }
        return context.dataset_id ? "Dataset " + context.dataset_id : "Explore";
      case "chart_list":
        return "Chart list";
      case "dashboard_list":
        return "Dashboard list";
      case "dataset_list":
        return "Dataset list";
      case "dataset":
        return "Dataset";
      case "sqllab":
        return "SQL Lab";
      case "query_history":
        return "Query history";
      case "saved_queries":
        return "Saved queries";
      case "home":
        return "Superset home";
      default:
        return "Superset";
    }
  }

  /**
   * Superset 6 is a single-page app, so the URL changes without a page load.
   * history.pushState/replaceState are wrapped (once, and still calling the
   * originals) to emit a synthetic event; popstate and hashchange cover the
   * back button and in-page anchors.
   *
   * @param {() => void} onChange
   */
  function watchRouteChanges(onChange) {
    var EVENT = "lifedata:locationchange";
    if (!window.__lifedataHistoryHooked) {
      window.__lifedataHistoryHooked = true;
      ["pushState", "replaceState"].forEach(function (method) {
        var original = window.history[method];
        window.history[method] = function () {
          var result = original.apply(this, arguments);
          window.dispatchEvent(new Event(EVENT));
          return result;
        };
      });
    }
    window.addEventListener(EVENT, onChange);
    window.addEventListener("popstate", onChange);
    window.addEventListener("hashchange", onChange);
  }

  // --- Transport ---------------------------------------------------------

  /**
   * Errors the UI must distinguish. `kind` drives which message the user sees.
   * @param {string} kind
   * @param {string} message
   * @param {string} [detail]
   */
  function PanelError(kind, message, detail) {
    this.name = "PanelError";
    this.kind = kind;
    this.message = message;
    this.detail = detail || "";
  }
  PanelError.prototype = Object.create(Error.prototype);

  /** @type {Promise<string>|null} Cached for the session — the token is stable while the session is. */
  var csrfPromise = null;

  /** @returns {Promise<string>} */
  function getCsrfToken() {
    if (!csrfPromise) {
      csrfPromise = fetch(CSRF_URL, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      })
        .then(function (response) {
          if (response.status === 401 || response.status === 403) {
            throw new PanelError(
              "session_expired",
              "Your Superset session has expired. Reload the page and sign in again."
            );
          }
          if (!response.ok) {
            throw new PanelError(
              "unreachable",
              "Could not obtain a Superset CSRF token (HTTP " + response.status + ")."
            );
          }
          return response.json();
        })
        .then(function (payload) {
          return payload && payload.result ? payload.result : "";
        })
        .catch(function (err) {
          // Drop the cache so a retry after re-authentication can succeed.
          csrfPromise = null;
          throw err instanceof PanelError
            ? err
            : new PanelError("unreachable", "Could not reach Superset to obtain a CSRF token.", String(err));
        });
    }
    return csrfPromise;
  }

  /**
   * Execute a signed action envelope against Superset, as the user.
   *
   * This is the security-critical function in the whole panel. Two rules:
   *
   *   1. Execute EXACTLY what was signed — the envelope's method, path and
   *      body, and nothing else. Nothing from the page, the model's prose, or
   *      any other frame may influence the request. The orchestrator signed a
   *      specific decision; this sends that decision and no other.
   *   2. Use the user's own credentials (`credentials: "same-origin"` plus the
   *      CSRF token). That is what bounds the copilot to the signed-in user's
   *      permissions — a viewer gets a clean 403 from Superset rather than the
   *      copilot succeeding on their behalf.
   *
   * The outcome is reported to /lifedata-ai/api/action-result/ so the audit
   * trail records what actually happened, not merely what was proposed.
   *
   * When `action.execution_mode === "mcp"`, none of the above applies: the
   * server itself performs the write via native MCP (its own minted identity
   * for this user, not this browser's cookie) and reports the outcome in the
   * same response, so this function just asks and displays the result --
   * see `executeMcpAction` below. That path exists for action types
   * tools/mcp_write_dispatch.py covers (create_chart, create_dashboard,
   * add_chart_to_dashboard); everything else still comes through here.
   *
   * @param {{action_id: string, action_type: string, envelope: string,
   *          summary: string, calls: Array<any>, execution_mode?: string}} action
   * @param {any} transcript
   * @returns {Promise<{ok: boolean}>}
   */
  function executeAction(action, transcript) {
    if (action && action.execution_mode === "mcp") {
      return executeMcpAction(action, transcript);
    }
    var calls = (action && action.calls) || [];
    if (!calls.length) {
      transcript.addError({
        title: "This change carried nothing to apply.",
        detail: "Vima proposed an action with no Superset calls in it.",
      });
      return Promise.resolve({ ok: false });
    }
    // Refuse anything that is not a Superset API path. The orchestrator already
    // enforces this when it builds the envelope; repeating it here means a
    // tampered frame cannot point the user's authenticated session at another
    // host.
    for (var i = 0; i < calls.length; i++) {
      if (!calls[i].path || calls[i].path.indexOf("/api/") !== 0) {
        transcript.addError({ title: "Refused an action with an unexpected target." });
        return Promise.resolve({ ok: false });
      }
    }
    var line = transcript.addToolLine("applying: " + (action.summary || action.action_type));

    // Always fetch a fresh token for a write. Superset ties the token to the
    // session, and immediately after sign-in the endpoint briefly 401s while
    // the session settles — caching that failure (or a token from a previous
    // session) makes the write fail with a confusing "CSRF token is invalid"
    // from Superset rather than a clear message from us.
    csrfPromise = null;
    return getCsrfToken()
      .catch(function () {
        csrfPromise = null;
        return getCsrfToken();
      })
      .then(function (token) {
        if (!token) {
          throw new PanelError(
            "session_expired",
            "Could not obtain a Superset CSRF token. Reload the page and sign in again."
          );
        }
        // Run the sequence in order, stopping at the first failure. `createdId`
        // carries the id of a just-created object to later calls, which is how
        // "create the dashboard, then attach each chart to it" works when the
        // dashboard's id does not exist until the first call returns.
        var createdId = null;
        var firstOutcome = null;

        function runOne(index) {
          if (index >= calls.length) {
            return Promise.resolve(firstOutcome);
          }
          var call = calls[index];
          // `multipart` calls (today, only the dataset-upload action) carry a
          // file that JSON cannot transport. Everything else is the ordinary
          // JSON path. Never both: a signed call is one shape or the other.
          var requestInit = call.multipart
            ? {
                method: call.method,
                credentials: "same-origin",
                headers: { "X-CSRFToken": token },
                body: buildMultipartBody(call),
              }
            : {
                method: call.method,
                credentials: "same-origin",
                headers: { "Content-Type": "application/json", "X-CSRFToken": token },
                body:
                  call.method === "GET"
                    ? undefined
                    : JSON.stringify(encodeBody(substituteNewId(call.body, createdId), call.json_encode) || {}),
              };
          return fetch(call.path, requestInit)
            .then(function (response) {
              return response
                .json()
                .catch(function () {
                  return {};
                })
                .then(function (payload) {
                  return { status: response.status, ok: response.ok, payload: payload };
                });
            })
            .then(function (outcome) {
              if (index === 0) {
                firstOutcome = outcome;
                if (outcome.payload && outcome.payload.id) {
                  createdId = outcome.payload.id;
                }
              }
              if (!outcome.ok) {
                return outcome;
              }
              return runOne(index + 1);
            });
        }

        return runOne(0);
      })
      .then(function (outcome) {
        line.complete(!outcome.ok);
        return report(action, outcome).then(function (reported) {
          if (outcome.ok) {
            transcript.addActionApplied(action, outcome.payload, reported);
            refreshSupersetView();
          } else {
            transcript.addError({
              title: "Superset refused this change.",
              detail: describeSupersetFailure(outcome),
            });
          }
          return { ok: outcome.ok };
        });
      })
      .catch(function (err) {
        line.complete(true);
        transcript.addError({
          title: err instanceof PanelError ? err.message : "The change could not be applied.",
          detail: err instanceof PanelError ? err.detail : String(err),
        });
        return { ok: false };
      });
  }

  /**
   * Confirm an execution_mode="mcp" action. The server performs the actual
   * Superset write (see views/execute_mcp_action.py) -- this just asks and
   * shows the result. Only `action_id` is sent: every detail of the write
   * already lives server-side in the ActionRecord WriteTool.run() created,
   * so there's nothing here for a tampered request to change.
   *
   * @param {{action_id: string, summary: string}} action
   * @param {any} transcript
   * @returns {Promise<{ok: boolean}>}
   */
  function executeMcpAction(action, transcript) {
    var line = transcript.addToolLine("applying: " + (action.summary || action.action_type));
    return fetch(EXECUTE_MCP_ACTION_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action_id: action.action_id }),
    })
      .then(function (response) {
        return response.json().catch(function () {
          return {};
        });
      })
      .then(function (result) {
        line.complete(!result.ok);
        if (result.ok) {
          transcript.addActionApplied(
            action,
            { id: result.superset_object_id, url: result.url },
            result
          );
          refreshSupersetView();
        } else {
          transcript.addError({
            title: "Superset refused this change.",
            detail: result.error || result.message || "The MCP call failed.",
          });
        }
        return { ok: !!result.ok };
      })
      .catch(function (err) {
        line.complete(true);
        transcript.addError({
          title: "The change could not be applied.",
          detail: String(err),
        });
        return { ok: false };
      });
  }

  /**
   * Replace the new-id placeholder with the id the first call actually created.
   *
   * The orchestrator cannot know a dashboard's id before it exists, so it emits
   * a placeholder and we fill it in here. Only this exact sentinel is
   * substituted — nothing else in the signed body is touched.
   */
  function substituteNewId(body, createdId) {
    if (createdId === null || body === null || body === undefined) {
      return body;
    }
    if (Array.isArray(body)) {
      return body.map(function (item) {
        return substituteNewId(item, createdId);
      });
    }
    if (typeof body === "object") {
      var out = {};
      Object.keys(body).forEach(function (key) {
        out[key] = substituteNewId(body[key], createdId);
      });
      return out;
    }
    return body === "__NEW_ID__" ? createdId : body;
  }

  /**
   * Serialise the body keys the envelope says Superset wants as JSON strings.
   *
   * Superset stores `position_json`, `params` and `json_metadata` as strings.
   * A body that arrived here already serialised could not carry a `__NEW_ID__`
   * placeholder as a value — substituting one would mean string surgery on
   * content the orchestrator signed, which is not something this panel should
   * ever do. So those values travel as nested objects, ids are substituted
   * structurally above, and the named keys are stringified last.
   *
   * The key list is inside the signature, so a tampered frame cannot add a key
   * here or drop one.
   */
  function encodeBody(body, keys) {
    if (!keys || !keys.length || !body || typeof body !== "object") {
      return body;
    }
    var out = {};
    Object.keys(body).forEach(function (key) {
      out[key] = keys.indexOf(key) === -1 ? body[key] : JSON.stringify(body[key]);
    });
    return out;
  }

  /**
   * Build a `multipart/form-data` body for a signed file-upload call.
   *
   * `call.body` carries the plain form fields (table_name, type, ...);
   * `call.multipart` names the one file field and carries its bytes
   * pre-encoded as base64, because JSON cannot carry raw bytes. `fetch` sets
   * the multipart boundary itself from a `FormData` body, so no
   * `Content-Type` header is set by the caller — setting one here would fix
   * the boundary to the wrong value and Superset would refuse the request.
   *
   * @param {{body?: Record<string, any>, multipart: {field: string, filename: string,
   *          content_type?: string, content_base64: string}}} call
   * @returns {FormData}
   */
  function buildMultipartBody(call) {
    var form = new FormData();
    var fields = call.body || {};
    Object.keys(fields).forEach(function (key) {
      form.append(key, String(fields[key]));
    });
    var spec = call.multipart;
    var binary = atob(spec.content_base64);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) {
      bytes[i] = binary.charCodeAt(i);
    }
    var blob = new Blob([bytes], { type: spec.content_type || "application/octet-stream" });
    form.append(spec.field, blob, spec.filename);
    return form;
  }

  /**
   * Reverse the last applied change.
   *
   * The orchestrator returns an ordinary signed envelope that restores the
   * snapshot, so undo goes through exactly the same execution path — audited,
   * permission-checked, and performed by the user's own session. It refuses
   * server-side if the object changed after the snapshot was taken.
   */
  function undoAction(action, transcript, mode) {
    return fetch(UNDO_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action_id: action.action_id, mode: mode || undefined }),
    })
      .then(function (response) {
        return response.json().then(function (payload) {
          return { ok: response.ok, payload: payload };
        });
      })
      .then(function (outcome) {
        if (!outcome.ok) {
          transcript.addError({
            title: "Could not undo this change.",
            detail: outcome.payload && outcome.payload.message,
          });
          return;
        }
        return executeAction(outcome.payload, transcript);
      })
      .catch(function (err) {
        transcript.addError({ title: "Could not undo this change.", detail: String(err) });
      });
  }

  /**
   * Tell the orchestrator what happened, so the audit trail is truthful.
   * @returns {Promise<any>} the server's verdict, which says whether undo is possible.
   */
  function report(action, outcome) {
    return fetch(ACTION_RESULT_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action_id: action.action_id,
        status: outcome.ok ? "executed" : "failed",
        http_status: outcome.status,
        response_excerpt: JSON.stringify(outcome.payload || {}).slice(0, 1000),
        superset_object_id:
          (outcome.payload && (outcome.payload.id || outcome.payload.result_id)) || null,
      }),
    })
      .then(function (response) {
        return response.json().catch(function () {
          return {};
        });
      })
      .catch(function () {
        // A failed audit report must not hide a successful change from the user.
        return {};
      });
  }

  /** Tell the orchestrator the user declined, so the record does not sit pending. */
  function reportCancelled(action) {
    return fetch(ACTION_RESULT_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action_id: action.action_id, status: "cancelled" }),
    }).catch(function () {});
  }

  function describeSupersetFailure(outcome) {
    if (outcome.status === 403) {
      return "Your Superset account does not have permission to make this change.";
    }
    if (outcome.status === 401) {
      return "Your Superset session expired. Reload the page and sign in again.";
    }
    var payload = outcome.payload || {};
    var message = payload.message || (payload.errors && payload.errors[0] && payload.errors[0].message);
    return message ? String(message) : "HTTP " + outcome.status;
  }

  /**
   * Note that the page is now stale, without yanking it out from under anyone.
   *
   * Superset is an SPA, so content created underneath it does not appear until
   * something reloads. An automatic reload was the first attempt and it is the
   * wrong behaviour: it fires while the user is still reading the copilot's
   * explanation, and it discards anything they had typed. Offer the reload
   * instead and let them take it when they are ready.
   */
  function refreshSupersetView() {
    if (document.querySelector(".ld-stale")) {
      return;
    }
    var row = el("div", { class: "ld-stale" }, [
      el("span", { text: "Superset has changed. Reload to see it." }),
      el("button", {
        type: "button",
        class: "ld-stale__reload",
        text: "Reload",
        onclick: function () {
          window.location.reload();
        },
      }),
    ]);
    var host = document.querySelector(".ld-messages");
    if (host) {
      host.appendChild(row);
      host.scrollTop = host.scrollHeight;
    }
  }

  /**
   * POST one turn and stream the SSE response.
   *
   * @param {{message: string|null, pageContext: PageContext, conversationId: string|null,
   *          resume: boolean, signal: AbortSignal, onFrame: (frame: SseFrame) => void}} options
   * @returns {Promise<void>}
   */
  function streamChat(options) {
    return getCsrfToken().then(function (token) {
      /** @type {Record<string, string>} */
      var headers = {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      };
      // The orchestrator has no Django CSRF middleware (see lifedata/settings.py);
      // this header carries Superset's token, which is what the write executor
      // needs and what proves the caller holds a live Superset session.
      if (token) {
        headers["X-CSRFToken"] = token;
      }

      var body = { page_context: options.pageContext };
      if (options.resume) {
        // The declared shape for "the user pressed Apply, carry on". There is
        // genuinely no user message: the loop writes a tool_result instead, so
        // the model resumes its own tool call rather than seeing a fresh turn.
        body.resume = true;
      } else {
        body.message = options.message;
      }
      if (options.conversationId) {
        body.conversation_id = options.conversationId;
      }

      var requestBody = JSON.stringify(body);
      if (options.files && options.files.length) {
        delete headers["Content-Type"];
        requestBody = new FormData();
        requestBody.append("payload", JSON.stringify(body));
        options.files.forEach(function (file) { requestBody.append("files", file, file.name); });
      }
      return fetch(CHAT_URL, {
        method: "POST",
        credentials: "same-origin",
        headers: headers,
        body: requestBody,
        signal: options.signal,
      })
        .catch(function (err) {
          if (err && err.name === "AbortError") {
            throw err;
          }
          throw new PanelError("unreachable", "Could not reach Vima.", String(err));
        })
        .then(function (response) {
          if (!response.ok) {
            return response
              .text()
              .catch(function () {
                return "";
              })
              .then(function (text) {
                throw describeHttpFailure(response.status, text);
              });
          }
          if (!response.body) {
            throw new PanelError("server", "Vima returned a response the browser cannot stream.");
          }
          return pumpStream(response.body, options.onFrame);
        });
    });
  }

  /**
   * @param {number} status
   * @param {string} bodyText
   * @returns {PanelError}
   */
  function describeHttpFailure(status, bodyText) {
    var detail = "";
    try {
      var parsed = JSON.parse(bodyText);
      detail = parsed.message || parsed.error || parsed.detail || "";
    } catch (err) {
      detail = bodyText;
    }
    detail = truncate(String(detail || "").trim(), MAX_ERROR_DETAIL);

    if (status === 401) {
      return new PanelError(
        "session_expired",
        "Your Superset session has expired. Reload the page and sign in again.",
        detail
      );
    }
    if (status === 403) {
      return new PanelError("forbidden", "Superset refused this request for your account.", detail);
    }
    if (status === 409) {
      return new PanelError(
        "busy",
        "Vima is still working on your previous message. Give it a moment.",
        detail
      );
    }
    if (status === 502 || status === 503 || status === 504) {
      return new PanelError(
        "unreachable",
        "Vima could not reach Superset (HTTP " + status + "). Try again shortly.",
        detail
      );
    }
    return new PanelError("server", "Vima returned an error (HTTP " + status + ").", detail);
  }

  /**
   * @param {ReadableStream<Uint8Array>} stream
   * @param {(frame: SseFrame) => void} onFrame
   * @returns {Promise<void>}
   */
  function pumpStream(stream, onFrame) {
    var reader = stream.getReader();
    var decoder = new TextDecoder("utf-8");
    var sse = createSseDecoder(onFrame);

    function read() {
      return reader.read().then(function (result) {
        if (result.done) {
          sse.push(decoder.decode());
          sse.flush();
          return;
        }
        sse.push(decoder.decode(result.value, { stream: true }));
        return read();
      });
    }
    return read();
  }

  // --- UI ----------------------------------------------------------------

  function injectAssets() {
    if (!document.querySelector("link[data-lifedata-panel]")) {
      document.head.appendChild(
        el("link", { rel: "stylesheet", href: STYLESHEET_URL, "data-lifedata-panel": "true" })
      );
    }
    if (!document.querySelector("script[data-lifedata-markdown]")) {
      // A dynamically inserted <script> is ASYNC regardless of `defer` — that
      // attribute only applies to parser-inserted ones — so there is a window
      // in which the panel is live and the renderer is not. `renderMarkdown`
      // falls back to plain text for it, and `onload` repaints whatever was
      // drawn during it. Without that repaint, a conversation replayed on page
      // load would stay as unformatted text for ever, because a replayed
      // message never receives another delta to trigger a redraw.
      document.head.appendChild(
        el("script", {
          src: MARKDOWN_URL,
          "data-lifedata-markdown": "true",
          onload: repaintAll,
        })
      );
    }
  }

  /** Every live bubble's repaint function, so a late renderer can catch up. */
  var painters = [];

  function repaintAll() {
    painters.forEach(function (paint) {
      paint();
    });
  }

  /** Render markdown, or fall back to plain text before markdown.js has loaded. */
  function renderMarkdown(node, source) {
    if (window.__lifedataMarkdown) {
      window.__lifedataMarkdown.renderInto(node, source);
      return;
    }
    node.textContent = source;
  }

  function buildUi() {
    // Stroke icon paths (24x24, 1.8 stroke, currentColor — see icon()).
    var SPARKLE = "M12 3l1.8 4.4 4.4 1.8-4.4 1.8L12 15.4l-1.8-4.4-4.4-1.8 4.4-1.8L12 3z M19 15l.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1-2.1-.9 2.1-.9.9-2.1z";
    var SUN = "M12 16a4 4 0 100-8 4 4 0 000 8z M12 3v2 M12 19v2 M3 12h2 M19 12h2 M5.6 5.6l1.4 1.4 M17 17l1.4 1.4 M18.4 5.6L17 7 M7 17l-1.4 1.4";
    var MOON = "M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z";
    var CLOCK = "M12 21a9 9 0 100-18 9 9 0 000 18z M12 7v5l3 2";
    var PLUS = "M12 5v14 M5 12h14";
    var CROSS = "M6 6l12 12 M18 6L6 18";
    var PAPERCLIP = "M21 12.5l-8.5 8.5a6 6 0 01-8.5-8.5l9-9a4 4 0 015.7 5.7l-9 9a2 2 0 01-2.8-2.8l8.5-8.5";

    var toggle = el(
      "button",
      {
        type: "button",
        class: "ld-scope ld-toggle",
        id: "lifedata-ai-toggle",
        "aria-label": "Open Vima (Alt+L)",
        "aria-expanded": "false",
        "aria-controls": "lifedata-ai-panel",
      },
      [
        (function () {
          var glyph = icon(SPARKLE);
          glyph.setAttribute("class", "ld-toggle__icon");
          return glyph;
        })(),
        el("span", { class: "ld-toggle__label", text: "Ask Vima" }),
      ]
    );

    var contextLabel = el("div", { class: "ld-context", id: "lifedata-ai-context" });
    var themeButton = el("button", {
      type: "button",
      class: "ld-iconbtn",
      "aria-label": "Switch to dark theme",
      title: "Theme",
    }, [icon(SUN)]);
    var historyButton = el("button", {
      type: "button",
      class: "ld-iconbtn",
      "aria-label": "Show earlier conversations",
      title: "Earlier conversations",
    }, [icon(CLOCK)]);
    var newButton = el("button", {
      type: "button",
      class: "ld-iconbtn",
      "aria-label": "Start a new conversation",
      title: "New conversation",
    }, [icon(PLUS)]);
    var closeButton = el("button", {
      type: "button",
      class: "ld-iconbtn",
      "aria-label": "Close Vima",
    }, [icon(CROSS)]);

    var messages = el("div", {
      class: "ld-messages",
      id: "lifedata-ai-messages",
      role: "log",
      "aria-live": "polite",
      // `additions text` re-announced every completed tool line, because
      // finishing one REWRITES its text. Additions only; the running commentary
      // lives in the status strip below, which is its own polite region.
      "aria-relevant": "additions",
      tabindex: "0",
    });

    // Pending proposals mount HERE, not in the transcript. `.ld-actions` was
    // styled and completely dead while preview cards appended into the message
    // list and scrolled away — an executive must never have to scroll to find
    // the approve button. Outcomes move into the transcript on resolution.
    var actions = el("div", { class: "ld-actions", id: "lifedata-ai-actions" });

    // Two minutes of silence reads as a hang. This says something is
    // happening and for how long; it is also the one place activity is
    // announced, so the transcript can stop re-announcing tool lines. A
    // single spinning icon plus a rotating verb (never the literal tool
    // being called) — see startStatusClock.
    var statusIcon = el("span", { class: "ld-status__icon", "aria-hidden": "true", text: "✻" });
    var statusText = el("span", { class: "ld-status__text" });
    var status = el(
      "div",
      {
        class: "ld-status",
        id: "lifedata-ai-status",
        role: "status",
        "aria-live": "polite",
      },
      [statusIcon, statusText]
    );

    var input = el("textarea", {
      class: "ld-input",
      id: "lifedata-ai-input",
      rows: "1",
      placeholder: "Ask for a chart, or a dashboard…",
      "aria-describedby": "lifedata-ai-footnote",
    });

    // One slot, two states. Stop used to live in the thinking row, which is
    // destroyed on the first text delta — so once the model started talking
    // there was no way to cancel, even though a build commonly speaks and then
    // works for another minute.
    var send = el("button", { type: "submit", class: "ld-send", text: "Send" });

    // Hidden entirely rather than shown-disabled when the browser has no
    // SpeechRecognition (only Chromium-family browsers ship it, prefixed, as
    // of this writing) — a disabled mic invites "why doesn't this work"
    // where a missing one just reads as "there's no mic button here".
    var SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;
    var voiceButton = SpeechRecognitionCtor
      ? el(
          "button",
          {
            type: "button",
            class: "ld-voice",
            "aria-label": "Dictate your message",
            "aria-pressed": "false",
            title: "Dictate your message",
          },
          [icon("M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z M19 10v2a7 7 0 0 1-14 0v-2 M12 19v4 M8 23h8")]
        )
      : null;

    var fileInput = el("input", { type: "file", multiple: "", class: "ld-visually-hidden",
      accept: ".png,.jpg,.jpeg,.gif,.webp,.pdf,.docx,.xlsx,.csv,.tsv,.txt,.md,.json,.log,.yaml,.yml,.html,.htm",
      "aria-label": "Attach images or files" });
    var attachButton = el("button", { type: "button", class: "ld-attach",
      title: "Attach images or files", "aria-label": "Attach images or files" }, [icon(PAPERCLIP)]);
    var attachments = el("div", { class: "ld-attachments", "aria-live": "polite" });
    var composer = el(
      "form",
      { class: "ld-composer" },
      [
        el("label", {
          class: "ld-visually-hidden",
          for: "lifedata-ai-input",
          text: "Message Vima",
        }),
        fileInput,
        attachButton,
        input,
        voiceButton,
        send,
      ].filter(Boolean)
    );

    var panel = el(
      "aside",
      {
        class: "ld-scope ld-panel",
        id: "lifedata-ai-panel",
        role: "complementary",
        "aria-label": "Vima",
      },
      [
        el("header", { class: "ld-panel__header" }, [
          el("div", { class: "ld-brand" }, [
            el("span", { class: "ld-brand__mark", "aria-hidden": "true" }, [icon(SPARKLE)]),
            document.createTextNode("Vima"),
            el("span", { class: "ld-badge", text: "AI assistant" }),
          ]),
          contextLabel,
          themeButton,
          historyButton,
          newButton,
          closeButton,
        ]),
        messages,
        actions,
        status,
        attachments,
        composer,
        el("p", {
          class: "ld-footnote",
          id: "lifedata-ai-footnote",
          text: "Aggregates only. No patient-level records are sent to the model.",
        }),
      ]
    );

    document.body.appendChild(toggle);
    document.body.appendChild(panel);

    // Explicit light/dark on BOTH .ld-scope roots (toggle and panel are
    // siblings). Defaults to the saved choice, then the OS preference; the
    // header button persists the user's explicit choice.
    function applyTheme(theme, persist) {
      toggle.setAttribute("data-theme", theme);
      panel.setAttribute("data-theme", theme);
      themeButton.replaceChildren(icon(theme === "dark" ? SUN : MOON));
      themeButton.setAttribute("aria-label", theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
      if (persist) {
        stored(THEME_KEY, theme);
      }
    }
    var savedTheme = stored(THEME_KEY);
    var systemDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    var initialTheme = savedTheme === "light" || savedTheme === "dark"
      ? savedTheme
      : (systemDark ? "dark" : "light");
    applyTheme(initialTheme, false);
    themeButton.addEventListener("click", function () {
      applyTheme(panel.getAttribute("data-theme") === "dark" ? "light" : "dark", true);
    });

    return {
      toggle: toggle,
      panel: panel,
      contextLabel: contextLabel,
      themeButton: themeButton,
      historyButton: historyButton,
      newButton: newButton,
      closeButton: closeButton,
      messages: messages,
      actions: actions,
      status: status,
      statusText: statusText,
      composer: composer,
      fileInput: fileInput,
      attachButton: attachButton,
      attachments: attachments,
      input: input,
      voiceButton: voiceButton,
      send: send,
    };
  }

  /** @param {ReturnType<typeof buildUi>} refs */
  function createTranscript(refs, hooks) {
    var atBottom = true;

    refs.messages.addEventListener("scroll", function () {
      var distance = refs.messages.scrollHeight - refs.messages.scrollTop - refs.messages.clientHeight;
      atBottom = distance < 40;
    });

    /** Only auto-scroll when the user has not deliberately scrolled up to read. */
    function scroll() {
      if (atBottom) {
        refs.messages.scrollTop = refs.messages.scrollHeight;
      }
    }

    /** @param {HTMLElement} node */
    function append(node) {
      refs.messages.appendChild(node);
      scroll();
      return node;
    }

    var api = {
      clear: function () {
        while (refs.messages.firstChild) {
          refs.messages.removeChild(refs.messages.firstChild);
        }
        while (refs.actions.firstChild) {
          refs.actions.removeChild(refs.actions.firstChild);
        }
        // The bubbles are gone, so their repaint closures would hold detached
        // nodes alive for the life of the page.
        painters.length = 0;
        atBottom = true;
      },

      /** @param {"user"|"assistant"} role @param {string} [text] */
      addMessage: function (role, text) {
        var bubble = el("div", { class: "ld-msg ld-msg--" + role }, [
          el("span", { class: "ld-msg__role", text: role === "user" ? "You" : "Vima" }),
        ]);
        var body = el("div", { class: "ld-msg__body" });
        bubble.appendChild(body);

        // The raw accumulator is the whole streaming-safety design. Deltas are
        // token chunks, so partial markdown is guaranteed; rather than
        // reconciling a half-parsed structure, every frame clears the node and
        // re-parses this buffer. It also preserves the hidden coupling that
        // `isEmpty()` depends on, and gives copy-to-clipboard the source text
        // rather than the rendered DOM.
        var raw = text || "";
        var frame = null;

        function paint() {
          frame = null;
          renderMarkdown(body, raw);
          scroll();
        }

        if (raw) {
          renderMarkdown(body, raw);
        }
        // Registered so a markdown.js that arrives after this bubble was drawn
        // can repaint it. A replayed message never gets another delta, so
        // without this it would stay unformatted.
        painters.push(paint);
        if (role === "assistant") {
          bubble.appendChild(
            el("button", {
              type: "button",
              class: "ld-msg__copy",
              "aria-label": "Copy this answer",
              title: "Copy",
              onclick: function (event) {
                var button = event.currentTarget;
                // Copy `raw`, not the rendered DOM: what the user wants in
                // their document is the markdown, not a flattened paragraph.
                copyText(raw).then(function (ok) {
                  button.textContent = ok ? "✓" : "×";
                  window.setTimeout(function () {
                    button.replaceChildren(icon("M9 9h11v11H9z M5 15H4a1 1 0 01-1-1V4a1 1 0 011-1h10a1 1 0 011 1v1"));
                  }, 1200);
                });
              },
            }, [icon("M9 9h11v11H9z M5 15H4a1 1 0 01-1-1V4a1 1 0 011-1h10a1 1 0 011 1v1")])
          );
        }
        append(bubble);

        return {
          appendText: function (delta) {
            raw += delta;
            // Coalesced onto an animation frame: a full re-render per token
            // would be wasteful, and per frame is imperceptible.
            if (frame === null) {
              frame = window.requestAnimationFrame(paint);
            }
          },
          isEmpty: function () {
            return raw.length === 0;
          },
          text: function () {
            return raw;
          },
        };
      },

      /** @param {string} label */
      addToolLine: function (label) {
        var text = el("span", { text: label + "…" });
        // Tool lines are decoration, not content. Marking them hidden is what
        // lets the transcript keep `aria-live` without narrating every internal
        // step; the status strip carries activity for assistive technology.
        var line = el("div", { class: "ld-tool", "aria-hidden": "true" }, [
          el("span", { class: "ld-tool__dot" }),
          text,
        ]);
        append(line);
        return {
          complete: function (isError) {
            line.classList.add(isError ? "is-error" : "is-done");
            text.textContent = label + (isError ? " — failed" : "");
          },
        };
      },

      /**
       * Phase 7/10 prototype: an agent-authored visual, isolated in an
       * opaque-origin sandboxed iframe. `sandbox="allow-scripts"` only --
       * deliberately never `allow-same-origin`, no `src` pointed at a real
       * origin (a bare `srcdoc` document has no origin at all, so it cannot
       * read cookies, storage, or the parent page even if the snippet tried).
       * Data reaches it only via postMessage, after the iframe reports it has
       * loaded -- never inlined into the HTML itself, so the snippet's own
       * script never needs (or gets) a way to fetch its own data.
       *
       * @param {{title: string, html_snippet: string, rows: any[], columns: any[]}} data
       */
      addCanvas: function (data) {
        var wrapper = el("div", { class: "ld-canvas" }, [
          el("div", { class: "ld-canvas__title", text: data.title || "Agent-authored visual" }),
          el("div", {
            class: "ld-canvas__badge",
            text: "Sandboxed prototype — admin only, not yet promotable to a saved chart type",
          }),
        ]);
        var iframe = el("iframe", {
          class: "ld-canvas__frame",
          sandbox: "allow-scripts",
          title: data.title || "Agent-authored visual",
        });
        var doc =
          "<!doctype html><html><head><meta charset=\"utf-8\"></head><body>" +
          (data.html_snippet || "") +
          "</body></html>";
        iframe.srcdoc = doc;
        iframe.addEventListener("load", function () {
          // The iframe's opaque origin means '*' here is not a leak: there is
          // no legitimate origin string to check against, and nothing outside
          // this exact frame could ever receive it instead.
          iframe.contentWindow.postMessage(
            { rows: data.rows || [], columns: data.columns || [] },
            "*"
          );
        });
        wrapper.appendChild(iframe);
        append(wrapper);
        return wrapper;
      },

      /**
       * @param {{title: string, detail?: string, action?: {label: string, run: () => void}, variant?: "network"}} spec
       * @returns {{remove: () => void}}
       */
      addError: function (spec) {
        var children = [el("span", { class: "ld-error__title", text: spec.title })];
        if (spec.detail) {
          children.push(el("span", { class: "ld-error__detail", text: spec.detail }));
        }
        if (spec.action) {
          children.push(
            el("button", {
              type: "button",
              class: "ld-error__action",
              text: spec.action.label,
              onclick: spec.action.run,
            })
          );
        }
        var className = "ld-error" + (spec.variant === "network" ? " ld-notice--network" : "");
        // No role="alert". The card is already inside an aria-live region, and
        // nesting one inside the other announces the same text twice.
        var card = el("div", { class: className }, children);
        append(card);
        return {
          // Used when connectivity returns mid-retry: the "waiting for
          // network" card should disappear rather than sit stale once the
          // auto-retry it promised has actually started.
          remove: function () {
            if (card.parentNode) {
              card.parentNode.removeChild(card);
            }
          },
        };
      },

      /**
       * The clarification card (plan §5.3).
       *
       * Mounted in the TRANSCRIPT, not the actions rail: it belongs in reading
       * order beside the question it answers. Real <button>s in DOM order, so
       * keyboard access is free.
       *
       * @param {{questions: Array<any>}} data
       * @param {(answer: string) => void} onAnswer
       */
      addClarification: function (data, onAnswer) {
        var questions = (data && data.questions) || [];
        if (!questions.length) {
          return null;
        }
        var card = el("div", { class: "ld-ask", role: "group", "aria-label": "Vima needs to know" });
        var chosen = new Array(questions.length);
        var groups = [];

        function submit() {
          var answered = chosen.filter(function (value) {
            return value;
          });
          if (answered.length !== questions.length) {
            return;
          }
          card.classList.add("is-answered");
          groups.forEach(function (group) {
            group.buttons.forEach(function (button) {
              button.disabled = true;
            });
          });
          if (continueButton) {
            continueButton.disabled = true;
          }
          onAnswer(
            questions
              .map(function (question, index) {
                return questions.length > 1
                  ? question.question + " " + chosen[index]
                  : chosen[index];
              })
              .join("; ")
          );
        }

        var continueButton = null;

        questions.forEach(function (question, index) {
          var questionId = "ld-ask-q" + index + "-" + Math.random().toString(36).slice(2, 8);
          var group = el("div", { class: "ld-ask__group" });
          group.appendChild(
            el("div", { class: "ld-ask__question", id: questionId, text: question.question })
          );
          var optionRow = el("div", {
            class: "ld-ask__options",
            role: "group",
            "aria-labelledby": questionId,
          });
          var buttons = [];

          (question.options || []).forEach(function (option) {
            var button = el(
              "button",
              {
                type: "button",
                class: "ld-suggestion ld-ask__option",
                "aria-pressed": "false",
                onclick: function () {
                  chosen[index] = option.label;
                  buttons.forEach(function (other) {
                    other.setAttribute("aria-pressed", other === button ? "true" : "false");
                  });
                  // A single question submits on click — one interaction, no
                  // Continue button to find. Several questions record the
                  // choice and wait.
                  if (questions.length === 1) {
                    submit();
                  } else if (continueButton) {
                    continueButton.disabled = chosen.filter(Boolean).length !== questions.length;
                  }
                },
              },
              [el("span", { class: "ld-ask__label", text: option.label })]
            );
            if (option.hint) {
              button.appendChild(el("span", { class: "ld-ask__hint", text: option.hint }));
            }
            buttons.push(button);
            optionRow.appendChild(button);
          });

          if (question.allow_free_text !== false) {
            optionRow.appendChild(
              el("button", {
                type: "button",
                class: "ld-suggestion ld-ask__other",
                text: "Something else…",
                onclick: function () {
                  hooks.focusComposer(
                    "Type your answer, or choose an option above"
                  );
                },
              })
            );
          }

          group.appendChild(optionRow);
          card.appendChild(group);
          groups.push({ buttons: buttons });
        });

        if (questions.length > 1) {
          continueButton = el("button", {
            type: "button",
            class: "ld-ask__continue",
            text: "Continue",
            disabled: true,
            onclick: submit,
          });
          card.appendChild(continueButton);
        }

        append(card);
        return {
          // Kept on screen after answering, marked up — an executive scrolling
          // back should be able to see what they chose.
          settle: function () {
            card.classList.add("is-answered");
            groups.forEach(function (group) {
              group.buttons.forEach(function (button) {
                button.disabled = true;
              });
            });
            if (continueButton) {
              continueButton.disabled = true;
            }
          },
        };
      },

      /**
       * A change the user must approve before it happens (spec §6.6 `preview`).
       *
       * Mounted in the ACTIONS RAIL so it is always reachable above the
       * composer. It reads as a receipt, not a second question: the server's
       * summary already says what will be built, in the user's own vocabulary.
       *
       * @param {{summary: string, action_type: string, calls: any}} action
       */
      addActionPreview: function (action, handlers) {
        var card = el("div", { class: "ld-action", role: "group" });
        card.appendChild(el("div", { class: "ld-action__title", text: "Ready to apply" }));
        card.appendChild(
          el("div", { class: "ld-action__summary", text: action.summary || action.action_type })
        );
        (action.warnings || []).forEach(function (warning) {
          card.appendChild(el("div", { class: "ld-action__warning", text: warning }));
        });

        var buttons = el("div", { class: "ld-action__buttons" });
        var settled = false;
        var apply = el("button", {
          type: "button",
          class: "ld-action__apply",
          text: "Apply",
          onclick: function () {
            if (settled) return;
            settled = true;
            apply.disabled = true;
            cancel.disabled = true;
            card.classList.add("is-applied");
            handlers.onApply();
          },
        });
        var cancel = el("button", {
          type: "button",
          class: "ld-action__cancel",
          text: "Cancel",
          onclick: function () {
            if (settled) return;
            settled = true;
            card.classList.add("is-cancelled");
            apply.disabled = true;
            cancel.disabled = true;
            handlers.onCancel();
          },
        });
        buttons.appendChild(apply);
        buttons.appendChild(cancel);
        card.appendChild(buttons);

        refs.actions.appendChild(card);
        return {
          remove: function () {
            if (card.parentNode) {
              card.parentNode.removeChild(card);
            }
          },
          markSuperseded: function () {
            if (settled) return;
            settled = true;
            apply.disabled = true;
            cancel.disabled = true;
            card.classList.add("is-cancelled");
            card.appendChild(
              el("div", {
                class: "ld-action__detail",
                text: "Superseded — you asked for something else, so this was not applied.",
              })
            );
          },
        };
      },

      /** Confirmation, a link to the thing, and Undo where undo can work. */
      addActionApplied: function (action, payload, reported) {
        var row = el("div", { class: "ld-applied" });
        row.appendChild(
          el("span", {
            class: "ld-applied__text",
            text: "Applied: " + (action.summary || action.action_type),
          })
        );

        // "The thing exists, here it is" is where the product earns its keep.
        var link = objectLink(action, payload, reported);
        if (link) {
          row.appendChild(
            el("a", {
              class: "ld-applied__open",
              href: link.href,
              target: "_top",
              text: link.label,
            })
          );
        }

        // Only offer Undo where undo can actually work. The server already says
        // so — creating an object has no prior state to restore, and a stale
        // snapshot is refused — and a button that always 409s teaches people to
        // stop trusting the panel.
        if (reported && reported.undoable) {
          row.appendChild(
            el("button", {
              type: "button",
              class: "ld-applied__undo",
              text: "Undo",
              onclick: function (event) {
                var button = event.currentTarget;
                button.disabled = true;
                button.textContent = "Undoing…";
                undoAction(action, api, "restore");
              },
            })
          );
        } else if (link) {
          // A creation has no prior state, so its only inverse is deletion —
          // and deletion is a DISTINCT affordance, never Undo quietly
          // redefined. Undo restores state; deleting is destructive and is
          // itself unundoable, and the chart may already be on someone else's
          // dashboard. Silently making "Undo" mean "Delete" is the sort of
          // thing that ends a pilot.
          row.appendChild(deleteButton(action, api, link.label));
        }
        append(row);
      },

      addSuggestions: function (items, onPick) {
        var existing = refs.messages.querySelector(".ld-followups");
        if (existing && existing.parentNode) {
          existing.parentNode.removeChild(existing);
        }
        if (!items || !items.length) {
          return;
        }
        append(
          el(
            "div",
            { class: "ld-suggestions ld-followups" },
            items.map(function (item) {
              var text = typeof item === "string" ? item : item.label;
              var prompt = typeof item === "string" ? item : item.prompt;
              return el("button", {
                type: "button",
                class: "ld-suggestion",
                text: text,
                onclick: function () {
                  onPick(prompt);
                },
              });
            })
          )
        );
      },

      clearIntro: function () {
        var intro = refs.messages.querySelector(".ld-intro");
        if (intro && intro.parentNode) {
          intro.parentNode.removeChild(intro);
        }
        var followups = refs.messages.querySelector(".ld-followups");
        if (followups && followups.parentNode) {
          followups.parentNode.removeChild(followups);
        }
      },
    };
    return api;
  }

  /**
   * "I didn't want that" for something the copilot just created.
   *
   * Two clicks by design, and the first one is not a confirmation dialog — it
   * changes the label to name what is about to go. Deletion is unundoable, so
   * the cost of a mis-click is unrecoverable and the cost of one extra click is
   * nothing.
   *
   * The server re-checks ownership and existence before it signs anything, and
   * classifies the action `preview` however small it looks.
   */
  function deleteButton(action, transcript, label) {
    var what = label === "Open dashboard" ? "dashboard" : "chart";
    var armed = false;
    return el("button", {
      type: "button",
      class: "ld-applied__delete",
      title: "Remove the " + what + " this created",
      text: "Delete this " + what,
      onclick: function (event) {
        var button = event.currentTarget;
        if (!armed) {
          armed = true;
          button.classList.add("is-armed");
          button.textContent =
            what === "dashboard"
              ? "Really delete? (charts are kept)"
              : "Really delete this chart?";
          return;
        }
        button.disabled = true;
        button.textContent = "Deleting…";
        undoAction(action, transcript, "delete");
      },
    });
  }

  /** Where the applied object lives, so the user can go and look at it. */
  function objectLink(action, payload, reported) {
    var id =
      (payload && (payload.id || payload.result_id)) ||
      (reported && reported.superset_object_id) ||
      null;
    if (!id) {
      return null;
    }
    var type = String(action.action_type || "");
    if (type.indexOf("dashboard") !== -1 && type.indexOf("chart") === -1) {
      return { href: "/superset/dashboard/" + id + "/", label: "Open dashboard" };
    }
    if (type.indexOf("chart") !== -1) {
      return { href: "/explore/?slice_id=" + id, label: "Open chart" };
    }
    return null;
  }

  /** Clipboard with a fallback for browsers that refuse the async API. */
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(
        function () {
          return true;
        },
        function () {
          return false;
        }
      );
    }
    return Promise.resolve(false);
  }

  // --- Controller --------------------------------------------------------

  function start() {
    // The panel is useless on the sign-in pages — there is no session yet — and
    // nginx injects the script into those responses too.
    if (/^\/(login|logout)/.test(window.location.pathname)) {
      return;
    }

    injectAssets();
    var refs = buildUi();

    var state = {
      files: [],
      previews: [],
      open: false,
      busy: false,
      /** @type {string|null} */
      conversationId: stored(CONVERSATION_KEY),
      /** @type {AbortController|null} */
      controller: null,
      context: readPageContext(),
      /** @type {any} The pending preview card, if one is on screen. */
      pendingAction: null,
      /** @type {any} The pending clarification card, if one is on screen. */
      pendingAsk: null,
      turnStarted: 0,
    };

    var transcript = createTranscript(refs, {
      focusComposer: function (placeholder) {
        if (placeholder) {
          refs.input.placeholder = placeholder;
        }
        refs.input.focus();
      },
    });

    // --- Status strip ----------------------------------------------------

    var statusTimer = null;

    //: How long one rotating verb stays up before the next one takes over.
    var PHRASE_ROTATE_MS = 2600;

    function setStatus(text) {
      refs.statusText.textContent = text || "";
      refs.status.classList.toggle("is-visible", Boolean(text));
    }

    function startStatusClock(label) {
      state.turnStarted = Date.now();
      stopStatusClock();
      // `forced` is an explicit server-provided message (see the "progress"
      // frame) that should stick rather than be overtaken by the next
      // rotation — everything else is just a generic verb on a timer, never
      // the name of whatever tool is actually running.
      var forced = null;
      var phrase = label || "Working";
      var rotateAt = Date.now() + PHRASE_ROTATE_MS;

      function render() {
        var seconds = Math.round((Date.now() - state.turnStarted) / 1000);
        setStatus((forced || phrase) + "… " + seconds + "s");
      }

      statusTimer = window.setInterval(function () {
        if (!forced && Date.now() >= rotateAt) {
          phrase = nextWorkingPhrase();
          rotateAt = Date.now() + PHRASE_ROTATE_MS;
        }
        render();
      }, 1000);
      render();
      return {
        set: function (text) {
          forced = text;
        },
      };
    }

    function stopStatusClock() {
      if (statusTimer !== null) {
        window.clearInterval(statusTimer);
        statusTimer = null;
      }
    }

    // --- Intro -----------------------------------------------------------

    function showIntro() {
      transcript.clearIntro();
      refs.messages.appendChild(
        el("div", { class: "ld-intro" }, [
          el("p", {
            // The old copy said "This copilot reads; it does not change anything
            // yet." The product was telling users it could not do the thing it
            // does, on the very first screen they see.
            text:
              "Ask for a chart or a dashboard and I'll build it. " +
              "You approve anything that changes Superset.",
          }),
          el("div", { class: "ld-suggestions" }, suggestionButtons()),
        ])
      );
    }

    function suggestionsFor(pageType) {
      return SUGGESTIONS[pageType] || SUGGESTIONS.other;
    }

    function suggestionButtons() {
      return suggestionsFor(state.context.page_type).map(function (item) {
        var text = typeof item === "string" ? item : item.label;
        var prompt = typeof item === "string" ? item : item.prompt;
        return el("button", {
          type: "button",
          class: "ld-suggestion",
          text: text,
          // Submit on click. Filling the box and making the user press Send
          // again turns a menu back into a blank page.
          onclick: function () {
            send(prompt);
          },
        });
      });
    }

    // --- Open/close ------------------------------------------------------

    /**
     * @param {boolean} open
     * @param {boolean} [manageFocus] Pass false when restoring state on page
     *   load — moving focus then would yank it away from the page the user
     *   actually navigated to.
     */
    function setOpen(open, manageFocus) {
      state.open = open;
      refs.panel.classList.toggle("is-open", open);
      refs.toggle.setAttribute("aria-expanded", open ? "true" : "false");
      // `inert` keeps the closed panel out of the tab order in browsers that
      // support it; `visibility: hidden` in the stylesheet does the same for
      // the rest.
      if (open) {
        refs.panel.removeAttribute("inert");
      } else {
        refs.panel.setAttribute("inert", "");
      }
      stored(OPEN_STATE_KEY, open ? "1" : "0");
      if (manageFocus === false) {
        return;
      }
      if (open) {
        refs.input.focus();
      } else {
        refs.toggle.focus();
      }
    }

    refs.toggle.addEventListener("click", function () {
      setOpen(true);
    });
    refs.closeButton.addEventListener("click", function () {
      setOpen(false);
    });
    refs.panel.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.stopPropagation();
        setOpen(false);
      }
    });
    // Alt+L opens the panel from anywhere in Superset.
    //
    // DELIBERATELY NO FOCUS TRAP. This is a persistent, non-modal complementary
    // region sitting beside the application, and trapping focus would break
    // tabbing back into Superset — which is the entire point of a copilot next
    // to the app rather than in front of it. Escape closes and restores focus,
    // which is the correct behaviour for this pattern. Recorded as a decision,
    // not an omission.
    window.addEventListener("keydown", function (event) {
      if (event.altKey && !event.ctrlKey && !event.metaKey && event.key.toLowerCase() === "l") {
        event.preventDefault();
        setOpen(!state.open);
      }
    });

    // --- Composer --------------------------------------------------------

    function autosize() {
      refs.input.style.height = "auto";
      refs.input.style.height = Math.min(refs.input.scrollHeight, 140) + "px";
    }

    refs.input.addEventListener("input", autosize);
    refs.input.addEventListener("keydown", function (event) {
      // `isComposing` guards IME candidate selection, where Enter commits text
      // rather than submitting.
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        submit();
      }
    });
    refs.composer.addEventListener("submit", function (event) {
      event.preventDefault();
      if (state.busy) {
        stop();
      } else {
        submit();
      }
    });

    // --- Voice dictation ---------------------------------------------------
    //
    // Dictation into the composer, not a separate voice-command grammar: the
    // copilot already understands free-text requests, so transcribing speech
    // into the same textarea the user would otherwise type into reuses that
    // entirely rather than building and maintaining a second, narrower command
    // language. The user reviews the transcript exactly like typed text and
    // sends it the same way — nothing is spoken directly into an action.
    if (refs.voiceButton) {
      var recognition = null;
      var dictationBaseText = "";

      function stopDictation() {
        if (recognition) {
          // onend still fires after this; it no-ops there via the null check.
          recognition.stop();
          recognition = null;
        }
        refs.voiceButton.classList.remove("is-listening");
        refs.voiceButton.setAttribute("aria-pressed", "false");
      }

      function startDictation() {
        if (recognition || state.busy) {
          return;
        }
        recognition = new SpeechRecognitionCtor();
        recognition.lang = document.documentElement.lang || "en-US";
        recognition.continuous = true;
        recognition.interimResults = true;
        // Captured once at start, not re-read per result: appending every
        // interim result onto whatever is currently in the box would
        // duplicate text, since each interim replaces the previous interim
        // for the same utterance rather than extending it.
        dictationBaseText = refs.input.value;
        refs.voiceButton.classList.add("is-listening");
        refs.voiceButton.setAttribute("aria-pressed", "true");

        recognition.onresult = function (event) {
          var transcript = "";
          for (var i = 0; i < event.results.length; i += 1) {
            transcript += event.results[i][0].transcript;
          }
          var joiner = dictationBaseText && !/\s$/.test(dictationBaseText) ? " " : "";
          refs.input.value = dictationBaseText + joiner + transcript;
          autosize();
        };
        // Browsers raise "no-speech"/"aborted" routinely (a pause, the user
        // clicking stop) — these are not failures worth an error card, just
        // the end of this dictation session.
        recognition.onerror = function () {
          stopDictation();
        };
        recognition.onend = function () {
          recognition = null;
          refs.voiceButton.classList.remove("is-listening");
          refs.voiceButton.setAttribute("aria-pressed", "false");
          refs.input.focus();
        };
        recognition.start();
      }

      refs.voiceButton.addEventListener("click", function () {
        if (recognition) {
          stopDictation();
        } else {
          startDictation();
        }
      });
    }

    /**
     * @param {boolean} busy
     * @param {{stoppable?: boolean}} [options]
     */
    function setBusy(busy, options) {
      state.busy = busy;
      // Stoppable only while a request is in flight to abort. Applying an
      // envelope is a sequence of writes against Superset with no controller
      // behind it, so offering "Stop" there would be a button that does
      // nothing — worse than no button, because the user believes it worked.
      var stoppable = busy && (!options || options.stoppable !== false);
      // The composer stays ENABLED. It was disabled for the whole turn, so
      // during a two-minute build the user could not draft their next question
      // — and the clarification card's free-text escape hatch would have been
      // unusable. `submit()` guards on busy, so nothing races.
      refs.send.textContent = stoppable ? "Stop" : "Send";
      refs.send.classList.toggle("is-stop", stoppable);
      refs.send.disabled = busy && !stoppable;
      refs.messages.setAttribute("aria-busy", busy ? "true" : "false");
    }

    function stop() {
      if (state.controller) {
        state.controller.abort();
      }
    }

    // --- Conversation identity -------------------------------------------

    function setConversation(id) {
      state.conversationId = id || null;
      stored(CONVERSATION_KEY, id || null);
    }

    function newConversation() {
      state.files = [];
      renderAttachments();
      setConversation(null);
      state.pendingAction = null;
      state.pendingAsk = null;
      transcript.clear();
      showIntro();
      refs.input.focus();
    }

    refs.newButton.addEventListener("click", newConversation);

    /**
     * Replay a stored conversation, including anything still pending.
     *
     * `conversationId` was never persisted, so every Superset navigation
     * silently started a new conversation and the old thread became
     * unreachable — while `conversations.py`'s own docstring claimed the panel
     * used these endpoints.
     */
    function rehydrate(id) {
      return fetch(CONVERSATIONS_URL + id + "/", { credentials: "same-origin" })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("gone");
          }
          return response.json();
        })
        .then(function (payload) {
          transcript.clear();
          (payload.messages || []).forEach(function (message) {
            if (!message.text) {
              return;
            }
            transcript.addMessage(message.role === "assistant" ? "assistant" : "user", message.text);
          });
          (payload.publications || []).forEach(mountPublication);
          if (payload.pending_clarification) {
            mountClarification(payload.pending_clarification);
          } else if (payload.pending_action) {
            mountPreview(payload.pending_action);
          } else {
            transcript.addSuggestions(followUps(), send);
          }
          setConversation(id);
        })
        .catch(function () {
          // The conversation is gone (deleted, or another user's). Forget it
          // rather than leaving the panel pinned to an id the server rejects.
          setConversation(null);
          showIntro();
        });
    }

    // --- History ---------------------------------------------------------

    refs.historyButton.addEventListener("click", function () {
      var existing = refs.panel.querySelector(".ld-history");
      if (existing) {
        existing.parentNode.removeChild(existing);
        return;
      }
      fetch(CONVERSATIONS_URL, { credentials: "same-origin" })
        .then(function (response) {
          return response.json();
        })
        .then(function (payload) {
          renderHistory(payload.conversations || []);
        })
        .catch(function () {
          transcript.addError({ title: "Could not load earlier conversations." });
        });
    });

    function renderHistory(rows) {
      var list = el("div", { class: "ld-history", role: "menu", "aria-label": "Earlier conversations" });
      if (!rows.length) {
        list.appendChild(el("p", { class: "ld-history__empty", text: "No earlier conversations." }));
      }
      rows.forEach(function (row) {
        var entry = el("div", { class: "ld-history__row" }, [
          el("button", {
            type: "button",
            class: "ld-history__open",
            text: row.title || "Untitled conversation",
            onclick: function () {
              list.parentNode.removeChild(list);
              rehydrate(row.id);
            },
          }),
          el("button", {
            type: "button",
            class: "ld-history__delete",
            "aria-label": "Delete this conversation",
            title: "Delete",
            onclick: function () {
              fetch(CONVERSATIONS_URL + row.id + "/", {
                method: "DELETE",
                credentials: "same-origin",
              })
                .then(function () {
                  entry.parentNode.removeChild(entry);
                  if (row.id === state.conversationId) {
                    newConversation();
                  }
                })
                .catch(function () {
                  transcript.addError({ title: "Could not delete that conversation." });
                });
            },
          }, [icon("M6 6l12 12 M18 6L6 18")]),
        ]);
        list.appendChild(entry);
      });
      refs.panel.insertBefore(list, refs.messages);
    }

    // --- Pending work ----------------------------------------------------

    function mountClarification(data) {
      if (state.pendingAsk) {
        state.pendingAsk.settle();
      }
      state.pendingAsk = transcript.addClarification(data, function (answer) {
        // A chip answer is an ordinary message on the same conversation. The
        // server turns it into the tool_result the model's own `ask_user` call
        // is waiting for, so the model resumes rather than seeing a new turn.
        transcript.addMessage("user", answer);
        run({ message: answer });
      });
    }

    function mountCanvas(data) {
      transcript.addCanvas(data);
    }

    function mountPreview(action) {
      if (state.pendingAction) {
        state.pendingAction.card.remove();
      }
      var card = transcript.addActionPreview(action, {
        onApply: function () {
          state.pendingAction = null;
          // BUSY FROM THE CLICK, not from when the turn resumes. Executing an
          // envelope is several HTTP calls to Superset, and the panel used to
          // look completely idle throughout — so a message sent in that window
          // raced the resume and came back 409 "the copilot is still working".
          // Seen in the acceptance run, where it cost a whole dashboard.
          setBusy(true, { stoppable: false });
          setStatus("Applying your change…");
          executeAction(action, transcript).then(function () {
            card.remove();
            // Released immediately before resuming: `run` refuses to start
            // while busy, and there is no gap a user could type into.
            setBusy(false);
            // Report first, then ask the loop to carry on. The outcome becomes
            // the deferred tool_result, which is what lets the model lay out a
            // dashboard using the id the Apply just produced.
            run({ resume: true });
          });
        },
        onCancel: function () {
          state.pendingAction = null;
          setBusy(true, { stoppable: false });
          reportCancelled(action).then(function () {
            card.remove();
            setBusy(false);
            run({ resume: true });
          });
        },
      });
      state.pendingAction = { card: card, action: action };
    }

    function followUps() {
      return FOLLOW_UPS[state.context.page_type] || FOLLOW_UPS.other;
    }

    function mountPublication(data) {
      if (!/^\/lifedata-ai\/dashboards\/[0-9a-f-]{36}\/$/.test(data.url || "")) return;
      window.dispatchEvent(new CustomEvent("lifedata:publication-updated", { detail: data }));
      var card = el("div", { class: "ld-publication" }, [
        el("strong", { text: data.title || "Dashboard published" }),
        el("a", { href: data.url }, [
          icon("M14 5h5v5 M19 5l-9 9 M19 13v5a1 1 0 01-1 1H6a1 1 0 01-1-1V6a1 1 0 011-1h5"),
          document.createTextNode("Open custom dashboard"),
        ]),
      ]);
      refs.messages.appendChild(card);
    }

    function renderAttachments() {
      state.previews.forEach(function (url) { URL.revokeObjectURL(url); });
      state.previews = [];
      refs.attachments.replaceChildren();
      state.files.forEach(function (file, index) {
        var chip = el("div", { class: "ld-attachment" });
        if (/^image\//.test(file.type)) {
          var url = URL.createObjectURL(file);
          state.previews.push(url);
          chip.appendChild(el("img", { src: url, alt: file.name }));
        }
        chip.appendChild(el("span", { text: file.name }));
        var remove = el("button", { type: "button", "aria-label": "Remove " + file.name }, [icon("M6 6l12 12 M18 6L6 18")]);
        remove.addEventListener("click", function () { state.files.splice(index, 1); renderAttachments(); });
        chip.appendChild(remove);
        refs.attachments.appendChild(chip);
      });
    }
    function addFiles(files) {
      Array.from(files).forEach(function (file) {
        if (state.files.length >= 4 || file.size > 5 * 1024 * 1024) {
          transcript.addError({ title: "Attach up to 4 files, each no larger than 5 MB." });
          return;
        }
        state.files.push(file);
      });
      renderAttachments();
    }
    refs.attachButton.addEventListener("click", function () { refs.fileInput.click(); });
    refs.fileInput.addEventListener("change", function () { addFiles(refs.fileInput.files); refs.fileInput.value = ""; });
    refs.composer.addEventListener("dragover", function (event) { event.preventDefault(); });
    refs.composer.addEventListener("drop", function (event) {
      event.preventDefault();
      if (event.dataTransfer) addFiles(event.dataTransfer.files);
    });
    refs.input.addEventListener("paste", function (event) {
      var files = event.clipboardData && event.clipboardData.files;
      if (files && files.length) { event.preventDefault(); addFiles(files); }
    });

    // --- Turn ------------------------------------------------------------

    function submit() {
      if (state.busy) {
        return;
      }
      var text = refs.input.value.trim();
      if (!text && !state.files.length) {
        return;
      }
      text = text || "Please inspect the attached files.";
      var files = state.files.slice();
      state.files = [];
      renderAttachments();
      refs.input.value = "";
      refs.input.placeholder = "Ask for a chart, or a dashboard…";
      autosize();
      send(text, files);
    }

    function send(text, files) {
      if (state.busy) {
        return;
      }
      transcript.clearIntro();
      transcript.addMessage("user", text + (files && files.length ? "\nAttached: " + files.map(function (file) { return file.name; }).join(", ") : ""));
      // A typed message SUPERSEDES a pending proposal. The server does the same
      // thing authoritatively — it resolves the dangling tool_use as superseded
      // and cancels the record — so the card must not sit there looking live.
      if (state.pendingAction) {
        state.pendingAction.card.markSuperseded();
        state.pendingAction = null;
      }
      if (state.pendingAsk) {
        state.pendingAsk.settle();
        state.pendingAsk = null;
      }
      run({ message: text, files: files || [] });
    }

    /** @param {{message?: string, resume?: boolean}} options */
    function run(options) {
      if (state.busy) {
        return;
      }
      state.controller = new AbortController();
      setBusy(true);
      var clock = startStatusClock(options.resume ? "Continuing" : "Working");

      /** @type {{appendText: (t: string) => void, isEmpty: () => boolean}|null} */
      var assistant = null;
      var sawStop = false;
      var sawError = false;
      var stopped = false;
      var awaitingUser = false;

      function assistantBubble() {
        if (!assistant) {
          assistant = transcript.addMessage("assistant", "");
        }
        return assistant;
      }

      /**
       * The SSE contract this panel consumes. Unknown event names are ignored
       * on purpose so the orchestrator can add events without breaking a cached
       * copy of this file.
       * @param {SseFrame} frame
       */
      function onFrame(frame) {
        var data = frame.data || {};
        switch (frame.event) {
          case "message_start":
            if (data.conversation_id) {
              setConversation(data.conversation_id);
            }
            break;

          case "text_delta":
            // `text` is the documented field; `delta` is tolerated because it
            // is the Anthropic wire name and easy to pass through by mistake.
            var delta = typeof data.text === "string" ? data.text : data.delta;
            if (typeof delta === "string" && delta) {
              assistantBubble().appendText(delta);
            }
            break;

          // `tool_use`/`tool_result` carry no UI of their own any more — the
          // single rotating status icon (startStatusClock) is the whole
          // "Vima is working" signal now, regardless of which or how many
          // tools are actually running underneath it.

          case "progress":
            // Emitted from inside the awaited-browser poll. Nine round trips
            // behind a static panel reads as a hang; this is also the SSE
            // keep-alive.
            clock.set(data.message || "Working");
            break;

          // --- Write actions -------------------------------------------
          // The orchestrator has decided what should happen and signed it. It
          // has NOT performed it: we execute the envelope here, with the user's
          // own Superset session and CSRF token, so the copilot can never do
          // anything the signed-in user could not do themselves (spec §1).
          case "action_envelope":
            // Low risk — apply immediately. The loop is waiting for the outcome
            // so it can chain the new object's id into the next step.
            executeAction(data, transcript);
            break;

          case "action_preview":
            // Broad or destructive — the user decides, and the turn ends here.
            // Whatever they do next resolves the model's pending tool call.
            mountPreview(data);
            break;

          case "clarification":
            mountClarification(data);
            break;

          case "published_dashboard":
            mountPublication(data);
            break;

          case "canvas":
            // Phase 7/10 prototype, admin-only (enforced server-side in
            // canvas.py -- this branch never runs for a non-admin because
            // the tool itself refuses before this event is ever emitted).
            mountCanvas(data);
            break;

          case "message_stop":
            sawStop = true;
            awaitingUser = data.stop_reason === "awaiting_user";
            if (data.conversation_id) {
              setConversation(data.conversation_id);
            }
            break;

          case "error":
            sawError = true;
            transcript.addError({
              title: data.message || "Vima could not complete this turn.",
              detail: data.code ? "code: " + data.code : undefined,
              action: retryFor(data.code, options),
            });
            break;

          default:
            break;
        }
      }

      streamChat({
        message: options.message || null,
        files: options.files || [],
        resume: Boolean(options.resume),
        pageContext: state.context,
        conversationId: state.conversationId,
        signal: state.controller.signal,
        onFrame: onFrame,
      })
        .then(function () {
          // A stream that stops without message_stop means the connection was
          // cut mid-turn (proxy timeout, worker restart, or the user's own
          // network dropping). Say so rather than leaving a half-written
          // answer looking complete. If the browser already knows it's
          // offline, this is the same situation showError's "unreachable"
          // branch handles — route it the same way rather than showing a
          // harder-edged error for what is, from here, indistinguishable.
          if (!sawStop && !sawError && !stopped) {
            if (!navigator.onLine) {
              pauseForNetwork(options);
            } else {
              transcript.addError({
                title: "The response ended unexpectedly. The answer above may be incomplete.",
                action: retryFor("stream_cut", options),
              });
            }
          }
        })
        .catch(function (err) {
          if (err && err.name === "AbortError") {
            stopped = true;
            transcript.addToolLine("stopped by you").complete(false);
            return;
          }
          showError(err, options);
        })
        .then(function () {
          // A turn that only ASKS returns no text, and that is correct rather
          // than a failure — guarding this is what stops the clarification flow
          // ending on "The copilot returned no text for this turn."
          if (assistant && assistant.isEmpty() && !sawError && !stopped && !awaitingUser) {
            transcript.addError({ title: "Vima returned no text for this turn." });
          }
          stopStatusClock();
          setStatus("");
          setBusy(false);
          state.controller = null;
          if (!awaitingUser && !state.pendingAction) {
            transcript.addSuggestions(followUps(), send);
          }
          if (state.open) {
            refs.input.focus();
          }
        });
    }

    /**
     * `addError` has always supported an action button and only ever used it
     * for "Reload page". These are the failures a retry genuinely fixes.
     */
    function retryFor(code, options) {
      var retryable = {
        anthropic_error: true,
        anthropic_unreachable: true,
        turn_timeout: true,
        tool_round_limit: true,
        internal_error: true,
        stream_cut: true,
      };
      if (!retryable[code]) {
        return undefined;
      }
      return {
        label: "Try again",
        run: function () {
          run(options);
        },
      };
    }

    /**
     * A dropped connection and a dead backend share the same `kind`
     * ("unreachable") but are not the same situation for the user: one
     * resolves itself the moment they're back online, the other won't no
     * matter how many times they retry. `navigator.onLine` going false is the
     * clear signal; the fetch-level failure message is the fallback for
     * flaky connections a browser doesn't always notice losing (captive
     * portals, VPN drops) before reporting itself online again.
     * @param {any} err
     */
    function isLikelyNetworkDrop(err) {
      if (!navigator.onLine) {
        return true;
      }
      return Boolean(err) && err.kind === "unreachable" && /reach Vima/i.test(err.message || "");
    }

    /**
     * The calm path for a dropped connection: a muted notice instead of an
     * error card, and an automatic retry the moment the browser reports it's
     * back online — the user does not have to notice the reconnect and click
     * something themselves.
     * @param {{message?: string, resume?: boolean}} options
     */
    function pauseForNetwork(options) {
      var resumed = false;
      // Deferred one tick (setTimeout, not a bare call): both triggers below
      // can fire while this turn's own promise chain is still mid-cleanup
      // (setBusy(false) hasn't landed yet), and run() no-ops while state.busy
      // is true. A macrotask always runs after the pending microtask queue
      // drains, so by the time this fires, cleanup is done and the retry is
      // actually allowed to start.
      function retryNow() {
        if (resumed) {
          return;
        }
        resumed = true;
        card.remove();
        window.setTimeout(function () {
          run(options);
        }, 0);
      }
      var card = transcript.addError({
        title: "Paused — waiting for network…",
        detail: "I'll pick this back up as soon as you're reconnected.",
        variant: "network",
        action: { label: "Try again now", run: retryNow },
      });
      window.addEventListener("online", retryNow, { once: true });
    }

    /** @param {any} err */
    function showError(err, options) {
      var kind = err && err.kind ? err.kind : "server";
      var detail = err && err.detail ? err.detail : "";
      if (kind === "session_expired") {
        transcript.addError({
          title: "Your Superset session has expired. Reload the page and sign in again.",
          detail: detail,
          action: {
            label: "Reload page",
            run: function () {
              window.location.reload();
            },
          },
        });
        return;
      }
      if (kind === "unreachable" && isLikelyNetworkDrop(err)) {
        pauseForNetwork(options);
        return;
      }
      transcript.addError({
        title: (err && err.message) || "Something went wrong talking to Vima.",
        detail: detail,
        action: kind === "busy" ? undefined : retryFor("stream_cut", options),
      });
    }

    // --- Route awareness -------------------------------------------------

    window.addEventListener("lifedata:context", refreshContext);

    function refreshContext() {
      state.context = readPageContext();
      refs.contextLabel.textContent = describeContext(state.context);
      var intro = refs.messages.querySelector(".ld-intro");
      if (intro) {
        // The suggestions are page-aware, so a route change must refresh them.
        showIntro();
      }
    }
    refreshContext();
    watchRouteChanges(refreshContext);

    // --- Initial state ---------------------------------------------------

    if (state.conversationId) {
      rehydrate(state.conversationId);
    } else {
      showIntro();
    }

    // Restoring the open state must not steal focus from the page the user just
    // loaded, hence `manageFocus = false`.
    setOpen(stored(OPEN_STATE_KEY) === "1", false);

    window.__lifedataCopilot = {
      open: function () {
        setOpen(true);
      },
      close: function () {
        setOpen(false);
      },
      newConversation: newConversation,
      // Exposed for manual verification and for the browser tests.
      pageContext: function () {
        return readPageContext();
      },
      conversationId: function () {
        return state.conversationId;
      },
    };
  }

  /**
   * Bootstrap, and never take Superset down with us.
   *
   * `start()` runs on DOMContentLoaded inside HTML nginx injected us into — so
   * a throw here happens *inside a page the user came to use for something
   * else*. Depending on where it lands it can abort the rest of the page's
   * scripts, and the user would experience a copilot defect as a Superset
   * defect, which is both unfair and very hard to diagnose.
   *
   * Log and render nothing. The same posture the agent loop takes toward
   * provider errors: a failure the user can do nothing about should be quiet
   * and contained, not loud and load-bearing.
   */
  function bootstrap() {
    try {
      start();
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error("Vima failed to start; Superset is unaffected.", err);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap);
  } else {
    bootstrap();
  }
})();
