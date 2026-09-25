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
  var UNDO_URL = ORCHESTRATOR + "/api/undo/";
  var CONVERSATIONS_URL = ORCHESTRATOR + "/api/conversations/";
  var STYLESHEET_URL = ORCHESTRATOR + "/static/panel/panel.css";
  var MARKDOWN_URL = ORCHESTRATOR + "/static/panel/markdown.js";

  /**
   * Superset's CSRF endpoint. VERIFIED FINDING (docs/superset-integration-
   * findings.md §1): this returns 401 when the session has expired, not a
   * retryable error. It is treated as terminal for the turn.
   */
  var CSRF_URL = "/api/v1/security/csrf_token/";

  var OPEN_STATE_KEY = "lifedata-ai.panel.open";
  var CONVERSATION_KEY = "lifedata-ai.panel.conversation";
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
      "Add a row of headline numbers",
      "Add a date filter",
      "Review this dashboard's design",
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
    dashboard: [
      "Review this dashboard's design",
      "Add a date filter",
      "Make the trend full width",
    ],
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

  /** Human phrasing for a tool name the server did not summarise. */
  function toolLabel(data) {
    // The server sends a specific summary on every tool_use frame ("inspecting
    // dataset 2", "building chart Patients by facility"). The panel used to
    // recompute a coarse label from its own table instead — a byte-for-byte
    // fork of the server's, which labelled none of the twelve write tools and
    // held a dead `review_dashboard_design` entry. Preferring the server's is
    // one line and fixes all of that.
    return data.summary || String(data.name || "working").replace(/_/g, " ");
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
   * @param {{action_id: string, action_type: string, envelope: string,
   *          summary: string, calls: Array<any>}} action
   * @param {any} transcript
   * @returns {Promise<{ok: boolean}>}
   */
  function executeAction(action, transcript) {
    var calls = (action && action.calls) || [];
    if (!calls.length) {
      transcript.addError({
        title: "This change carried nothing to apply.",
        detail: "The copilot proposed an action with no Superset calls in it.",
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
          var body = encodeBody(substituteNewId(call.body, createdId), call.json_encode);
          return fetch(call.path, {
            method: call.method,
            credentials: "same-origin",
            headers: { "Content-Type": "application/json", "X-CSRFToken": token },
            body: call.method === "GET" ? undefined : JSON.stringify(body || {}),
          })
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

      return fetch(CHAT_URL, {
        method: "POST",
        credentials: "same-origin",
        headers: headers,
        body: JSON.stringify(body),
        signal: options.signal,
      })
        .catch(function (err) {
          if (err && err.name === "AbortError") {
            throw err;
          }
          throw new PanelError("unreachable", "Could not reach the LifeData copilot.", String(err));
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
            throw new PanelError("server", "The copilot returned a response the browser cannot stream.");
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
        "The copilot is still working on your previous message. Give it a moment.",
        detail
      );
    }
    if (status === 502 || status === 503 || status === 504) {
      return new PanelError(
        "unreachable",
        "The copilot could not reach Superset (HTTP " + status + "). Try again shortly.",
        detail
      );
    }
    return new PanelError("server", "The copilot returned an error (HTTP " + status + ").", detail);
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
    var toggle = el(
      "button",
      {
        type: "button",
        class: "ld-scope ld-toggle",
        id: "lifedata-ai-toggle",
        "aria-label": "Open the LifeData AI copilot (Alt+L)",
        "aria-expanded": "false",
        "aria-controls": "lifedata-ai-panel",
      },
      [
        (function () {
          var glyph = icon("M21 11.5a8.4 8.4 0 0 1-9 8.4L3 21l1.1-4.1A8.4 8.4 0 1 1 21 11.5Z");
          glyph.setAttribute("class", "ld-toggle__icon");
          return glyph;
        })(),
        el("span", { class: "ld-toggle__label", text: "Ask LifeData" }),
      ]
    );

    var contextLabel = el("div", { class: "ld-context", id: "lifedata-ai-context" });
    var historyButton = el("button", {
      type: "button",
      class: "ld-iconbtn",
      "aria-label": "Show earlier conversations",
      title: "Earlier conversations",
      text: "☰",
    });
    var newButton = el("button", {
      type: "button",
      class: "ld-iconbtn",
      "aria-label": "Start a new conversation",
      title: "New conversation",
      text: "＋",
    });
    var closeButton = el("button", {
      type: "button",
      class: "ld-iconbtn",
      "aria-label": "Close the LifeData AI copilot",
      text: "×",
    });

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

    // Two minutes of silence reads as a hang. This says what is happening and
    // for how long; it is also the one place activity is announced, so the
    // transcript can stop re-announcing tool lines.
    var status = el("div", {
      class: "ld-status",
      id: "lifedata-ai-status",
      role: "status",
      "aria-live": "polite",
    });

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

    var composer = el("form", { class: "ld-composer" }, [
      el("label", {
        class: "ld-visually-hidden",
        for: "lifedata-ai-input",
        text: "Message the LifeData copilot",
      }),
      input,
      send,
    ]);

    var panel = el(
      "aside",
      {
        class: "ld-scope ld-panel",
        id: "lifedata-ai-panel",
        role: "complementary",
        "aria-label": "LifeData AI copilot",
      },
      [
        el("header", { class: "ld-panel__header" }, [
          el("div", { class: "ld-brand" }, [
            document.createTextNode("LifeData copilot"),
            el("span", { class: "ld-badge", text: "your session" }),
          ]),
          contextLabel,
          historyButton,
          newButton,
          closeButton,
        ]),
        messages,
        actions,
        status,
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

    return {
      toggle: toggle,
      panel: panel,
      contextLabel: contextLabel,
      historyButton: historyButton,
      newButton: newButton,
      closeButton: closeButton,
      messages: messages,
      actions: actions,
      status: status,
      composer: composer,
      input: input,
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
          el("span", { class: "ld-msg__role", text: role === "user" ? "You" : "Copilot" }),
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
              text: "⧉",
              onclick: function (event) {
                var button = event.currentTarget;
                // Copy `raw`, not the rendered DOM: what the user wants in
                // their document is the markdown, not a flattened paragraph.
                copyText(raw).then(function (ok) {
                  button.textContent = ok ? "✓" : "×";
                  window.setTimeout(function () {
                    button.textContent = "⧉";
                  }, 1200);
                });
              },
            })
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

      /** @param {{title: string, detail?: string, action?: {label: string, run: () => void}}} spec */
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
        // No role="alert". The card is already inside an aria-live region, and
        // nesting one inside the other announces the same text twice.
        append(el("div", { class: "ld-error" }, children));
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
        var card = el("div", { class: "ld-ask", role: "group", "aria-label": "The copilot needs to know" });
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
            items.map(function (text) {
              return el("button", {
                type: "button",
                class: "ld-suggestion",
                text: text,
                onclick: function () {
                  onPick(text);
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
      steps: 0,
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

    function setStatus(text) {
      refs.status.textContent = text || "";
      refs.status.classList.toggle("is-visible", Boolean(text));
    }

    function startStatusClock(label) {
      state.turnStarted = Date.now();
      state.steps = 0;
      stopStatusClock();
      var current = label || "Working";
      statusTimer = window.setInterval(function () {
        var seconds = Math.round((Date.now() - state.turnStarted) / 1000);
        var parts = [current];
        if (state.steps) {
          parts.push("step " + state.steps);
        }
        parts.push(seconds + "s");
        setStatus(parts.join(" · "));
      }, 1000);
      setStatus(current);
      return {
        set: function (label) {
          current = label;
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
      return suggestionsFor(state.context.page_type).map(function (text) {
        return el("button", {
          type: "button",
          class: "ld-suggestion",
          text: text,
          // Submit on click. Filling the box and making the user press Send
          // again turns a menu back into a blank page.
          onclick: function () {
            send(text);
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
            text: "×",
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
          }),
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

    // --- Turn ------------------------------------------------------------

    function submit() {
      if (state.busy) {
        return;
      }
      var text = refs.input.value.trim();
      if (!text) {
        return;
      }
      refs.input.value = "";
      refs.input.placeholder = "Ask for a chart, or a dashboard…";
      autosize();
      send(text);
    }

    function send(text) {
      if (state.busy) {
        return;
      }
      transcript.clearIntro();
      transcript.addMessage("user", text);
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
      run({ message: text });
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
      /** @type {Record<string, {complete: (isError?: boolean) => void}>} */
      var toolLines = {};
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

          case "tool_use":
            state.steps += 1;
            var label = toolLabel(data);
            clock.set(label.charAt(0).toUpperCase() + label.slice(1));
            toolLines[data.id || data.tool_use_id || data.name] = transcript.addToolLine(label);
            break;

          case "tool_result":
            var resultKey = data.tool_use_id || data.id || data.name;
            var line = toolLines[resultKey];
            if (line) {
              line.complete(Boolean(data.is_error));
              delete toolLines[resultKey];
            }
            break;

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
              title: data.message || "The copilot could not complete this turn.",
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
        resume: Boolean(options.resume),
        pageContext: state.context,
        conversationId: state.conversationId,
        signal: state.controller.signal,
        onFrame: onFrame,
      })
        .then(function () {
          // A stream that stops without message_stop means the connection was
          // cut mid-turn (proxy timeout, worker restart). Say so rather than
          // leaving a half-written answer looking complete.
          if (!sawStop && !sawError && !stopped) {
            transcript.addError({
              title: "The response ended unexpectedly. The answer above may be incomplete.",
              action: retryFor("stream_cut", options),
            });
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
          Object.keys(toolLines).forEach(function (key) {
            toolLines[key].complete(true);
          });
          // A turn that only ASKS returns no text, and that is correct rather
          // than a failure — guarding this is what stops the clarification flow
          // ending on "The copilot returned no text for this turn."
          if (assistant && assistant.isEmpty() && !sawError && !stopped && !awaitingUser) {
            transcript.addError({ title: "The copilot returned no text for this turn." });
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
      transcript.addError({
        title: (err && err.message) || "Something went wrong talking to the copilot.",
        detail: detail,
        action: kind === "busy" ? undefined : retryFor("stream_cut", options),
      });
    }

    // --- Route awareness -------------------------------------------------

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
      console.error("LifeData copilot failed to start; Superset is unaffected.", err);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap);
  } else {
    bootstrap();
  }
})();
