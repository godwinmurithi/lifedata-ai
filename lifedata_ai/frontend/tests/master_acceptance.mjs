// Acceptance tests of the live Master dashboard page, ported from docs/design/master/acceptance_tests.js.
//   npm run test:master
// Input: lifedata_ai/tests/fixtures/master_page.json, the server's real output (spec + /query aggregates) for a set of
// filter states, written and kept current by test_master_template.py::test_page_fixture_is_current.
//
// 1. For every state, the page's view model (master-model.ts) fed the SERVER's aggregates shows exactly what the
//    APPROVED DESIGN shows on its own sample data: every number, label, bar width, opacity, line and donut geometry.
// 2. Every chart element is wired to the one shared state, and clicking again clears it (the design's own list).
// 3. Chips, cascade, Clear all, time-point reset, own-dimension rule.
// 4. Coverage: a real instance shows only the slots and tabs that bind, with the registered reason elsewhere.
// 5. The rendered React markup (MasterView, server-rendered) keeps the design's structure.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
import { designVals, MONTH_KEYS } from "./master_oracle.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const M = require("./.build/master-model.cjs");
const V = require("./.build/master-view.cjs");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const PAGE = JSON.parse(fs.readFileSync(path.resolve(here, "../../tests/fixtures/master_page.json"), "utf8"));
const SPEC = PAGE.specs["master-demo"], REAL = PAGE.specs["master-hdss-sites"];
const DATA = Object.fromEntries(PAGE.states.map((s) => [s.name, s]));
let fails = 0, passes = 0;
const ok = (n, c, d = "") => { if (c) passes++; else fails++; console.log((c ? "PASS " : "FAIL ") + n + (d !== "" ? "  [" + d + "]" : "")); };

const pageState = (st, tab = "geo") => ({ tab, geo: { ...st.geo }, cycle: st.cycle, range: st.period, tp: st.time_point, dims: { ...st.dims } });
/** A page whose clicks really change its state (like the React component), always rendered over one server answer. */
function page(S0 = {}, data = DATA["no filter"].data, spec = SPEC) {
  const p = { S: { ...M.initialState(spec), ...S0 } };
  p.o = () => M.renderVals(p.S, data, spec, (s) => { p.S = s; });
  return p;
}

// ------------------------------------------------------------------ 1. the page shows what the approved design shows
const pick = (rows, ...keys) => (rows || []).map((r) => keys.map((k) => String(r[k])).join("|"));
const SEGS = (rows) => (rows || []).map((s) => [s.c, s.dash, s.off, s.op].join("|"));
const FIELDS = {
  "overview KPIs": (o) => [o.kRes, o.kHh, o.kU5, o.kBirths, o.kDeaths, o.kNet],
  "stat cards": (o) => [o.sEver, o.sPreg, o.sU5, o.sOpd],
  "vital events lines": (o) => (o.vLines || []).map((l) => [l.d, l.c, l.op].join("|")),
  "vital events points": (o) => pick(o.vDots, "x", "y", "c", "op", "r"),
  "vital events labels": (o) => pick(o.vLabels, "l", "t", "w", "c"),
  "vital events legend": (o) => pick(o.vLegend, "c", "t", "op"),
  "event mix donut": (o) => SEGS(o.mixSegs),
  "event mix legend": (o) => pick(o.mixLegend, "c", "t", "op"),
  "age and gender": (o) => pick(o.pyRows, "a", "m", "f", "mw", "fw", "mo", "fo", "ao"),
  "housing bars": (o) => pick(o.housRows, "l", "n", "w", "op"),
  "water bars": (o) => pick(o.watRows, "l", "n", "w", "op"),
  "bednet bars": (o) => pick(o.netRows, "l", "n", "w", "op"),
  "overview map pins": (o) => pick(o.ovPins, "label", "s", "bg", "bw", "bd").sort(),
  "sentinel KPIs": (o) => [o.sKSites, o.sKReports, o.sKSignals, o.sKRrt, o.sKLab, o.sKLag],
  "syndrome lines": (o) => (o.wLines || []).map((l) => [l.d, l.c, l.op].join("|")),
  "syndrome points": (o) => pick(o.wDots, "x", "y", "c", "op", "r"),
  "syndrome labels": (o) => pick(o.wLabels, "l", "t", "w"),
  "syndrome legend": (o) => pick(o.wLegend, "c", "t", "op"),
  "site pins": (o) => pick(o.sPins, "label", "s", "bg", "bw", "bd", "op").sort(),
  "site status legend": (o) => pick(o.statusItems, "c", "label", "bg", "bd"),
  "pathogen donut": (o) => SEGS(o.pathSegs),
  "pathogen legend": (o) => pick(o.pathLegend, "c", "t", "op"),
  "signals table": (o) => pick(o.sigRows, "fac", "syn", "sev", "bg", "fg", "when", "sbd"),
  "geo KPIs": (o) => [o.gKClusters, o.gKHigh, o.gKPop, o.gKMean, o.gKRad, o.gKP],
  "cluster pins": (o) => pick(o.gPins, "label", "s", "bg", "bw", "bd").sort(),
  "tier legend": (o) => pick(o.tierItems, "label", "c", "bg", "bd"),
  "tier donut": (o) => SEGS(o.tierSegs),
  "vulnerability scatter": (o) => pick(o.gPts, "x", "y", "bw", "label"),
  "hotspot table": (o) => pick(o.gRows, "name", "cases", "tier", "bg", "fg", "w", "score", "bar", "rowbg", "tbd"),
  "empty state": (o) => [o.isEmpty, o.showGeo, o.showOverview, o.showSentinel, o.noSignals, o.noClusters],
};
for (const { name, state, data } of PAGE.states) {
  const d = designVals(state), o = M.renderVals(pageState(state), data, SPEC, () => {});
  // an empty selection shows only the message, so only that is compared
  const bad = Object.entries(FIELDS).filter(([k]) => !d.isEmpty || k === "empty state").filter(([, f]) => JSON.stringify(f(o)) !== JSON.stringify(f(d)));
  ok("page == design: " + name, bad.length === 0, bad.map(([k, f]) => k + ": page " + JSON.stringify(f(o)).slice(0, 160) + " design " + JSON.stringify(f(d)).slice(0, 160)).join(" ; "));
}
ok("defaults are the approved numbers", (() => { const o = page().o(); return o.kRes === "180" && o.sKSites === "24" && o.gKHigh === "14"; })());

// ------------------------------------------------------------------ 2. every chart element is wired, and clicking again clears it
function wired(name, handler, read, want) {
  const p = page(); const h = handler(p.o());
  if (typeof h !== "function") { ok(name, false, "no handler"); return; }
  h(); const set = JSON.stringify(read(p.S)) === JSON.stringify(want);
  const after = read(p.S); handler(p.o())(); const cleared = !read(p.S);
  ok(name, set && cleared, JSON.stringify(after) + " -> " + JSON.stringify(read(p.S)));
}
const dim = (k) => (S) => S.dims[k];
wired("KPI tile: Births", (o) => o.pickBirths, dim("event_type"), "Births");
wired("KPI tile: Deaths", (o) => o.pickDeaths, dim("event_type"), "Deaths");
wired("KPI tile: Under five", (o) => o.pickU5, dim("age_band"), "0-4");
wired("stat card: Currently pregnant", (o) => o.pickPreg, dim("event_type"), "Pregnancies");
wired("KPI tile: High-risk villages", (o) => o.pickHigh, dim("tier"), "High");
SPEC.options.event_type.forEach((t, i) => {
  wired("event legend " + t, (o) => o.vLegend[i].pick, dim("event_type"), t);
  wired("event mix legend " + t, (o) => o.mixLegend.find((g) => g.t.startsWith(t + " (")).pick, dim("event_type"), t);
  ok("event mix slice " + t + " is clickable and dims the others", (() => { const o = page({ dims: { event_type: t } }).o(); return o.mixSegs.filter((s) => s.op === 1).length === 1 && o.mixSegs.every((s) => typeof s.pick === "function"); })());
});
wired("month label Aug", (o) => o.vLabels[4].pick, (S) => S.tp, "2026-08");
ok("month point sets event type AND month", (() => { const p = page(); p.o().vDots[7].pick(); return p.S.dims.event_type === "Deaths" && p.S.tp === MONTH_KEYS[0]; })(), "dot #7 = series 1, month 0");
SPEC.options.age_band.forEach((a, i) => wired("pyramid men " + a, (o) => o.pyRows[i].pm, (S) => S.dims.age_band, a));
ok("pyramid: men bar sets age+sex, age label sets age only, women bar sets age+sex", (() => {
  const p = page(), r = () => p.o().pyRows[3], g = () => [p.S.dims.age_band, p.S.dims.sex || ""].join();
  r().pm(); const a = g(); r().pa(); const b = g(); r().pf(); const c = g(); return a === "35-44,Male" && b === "35-44," && c === "35-44,Female"; })());
for (const [rows, k] of [["housRows", "housing"], ["watRows", "water"], ["netRows", "bednet"]])
  SPEC.options[k].forEach((v, i) => wired(k + " bar " + v, (o) => o[rows][i].pick, dim(k), v));
ok("overview map pin sets the whole village path", (() => { const p = page(); p.o().ovPins[0].pick(); return SPEC.geography.every((l) => p.S.geo[l]) && p.S.geo.country === "Uganda" && !!p.S.geo.village; })());
["Fever", "Diarrhea", "Respiratory", "Rash"].forEach((n, i) => wired("syndrome legend " + n, (o) => o.wLegend[i].pick, dim("syndrome"), n));
ok("weekly point sets syndrome AND week", (() => { const p = page(); p.o().wDots[9].pick(); return p.S.dims.syndrome === "Respiratory" && p.S.tp === MONTH_KEYS[2]; })(), "dot #9 = syndrome 1, week 2");
wired("week label W19", (o) => o.wLabels[4].pick, (S) => S.tp, "2026-08");
["alert", "watch", "normal"].forEach((st, i) => wired("site status legend " + st, (o) => o.statusItems[i].pick, dim("site_status"), st));
ok("sentinel map pin sets the village path", (() => { const p = page(); p.o().sPins[0].pick(); return !!p.S.geo.village; })());
SPEC.options.pathogen.forEach((k, i) => wired("pathogen legend " + k, (o) => o.pathLegend[i].pick, dim("pathogen"), k));
ok("pathogen slices are clickable and dim the others", (() => { const o = page({ dims: { pathogen: "Malaria" } }).o(); return o.pathSegs.filter((s) => s.op === 1).length === 1 && o.pathSegs.every((s) => typeof s.pick === "function"); })());
ok("signal row: facility sets village, syndrome sets syndrome, severity sets severity", (() => {
  const p = page(), r = p.o().sigRows[0]; r.pick(); const a = !!p.S.geo.village; p.o().sigRows[0].synPick(); const b = p.S.dims.syndrome === "Fever"; p.o().sigRows[0].sevPick(); return a && b && p.S.dims.signal_severity === "High"; })());
for (const t of ["High", "Moderate", "Low"]) {
  wired("tier legend (donut) " + t, (o) => o.tierItems.find((x) => x.label.startsWith(t + " ")).pick, dim("tier"), t);
  wired("tier legend (map) " + t, (o) => o.tierPlain.find((x) => x.label === t).pick, dim("tier"), t);
  ok("tier slice " + t, page({ dims: { tier: t } }).o().tierSegs.filter((s) => s.op === 1).length === 1);
}
ok("geo map pin and scatter point set the village path", (() => { const p = page(); p.o().gPins[5].pick(); const a = !!p.S.geo.village; const q = page(); q.o().gPts[0].pick(); return a && q.S.geo.village === "Nampiima"; })());
ok("hotspot row: village name sets village, tier pill sets tier", (() => { const p = page(); p.o().gRows[3].pick(); const a = p.S.geo.village === "Buwama"; p.o().gRows[3].tierPick(); return a && p.S.dims.tier === "Moderate"; })());
ok("every clickable list item has a handler", ["vLegend", "mixLegend", "pyRows", "housRows", "watRows", "netRows", "wLegend", "statusItems", "pathLegend", "tierItems", "tierPlain",
  "sigRows", "gRows", "vDots", "wDots", "ovPins", "sPins", "gPins", "gPts", "mixSegs", "pathSegs", "tierSegs", "vLabels", "wLabels"].every((k) => { const o = page().o(); return o[k].length > 0 && o[k].every((it) => ["pick", "pm"].some((h) => typeof it[h] === "function")); }));
ok("the page opens on the Overview tab", M.initialState(SPEC).tab === "overview" && page().S.tab === "overview");
ok("tabs switch without touching the filters", (() => { const p = page({ dims: { sex: "Female" } }); p.o().tabs[2].pick(); return p.S.tab === "geo" && p.S.dims.sex === "Female"; })());

// ------------------------------------------------------------------ 3. chips, cascade, Clear all, time-point reset, own-dimension rule
const busy = { geo: { country: "Uganda", district: "Buikwe" }, cycle: "UC10 (Apr – Sep 2026)", range: "Last quarter", tp: "2026-06",
  dims: { event_type: "Deaths", age_band: "35-44", sex: "Male", housing: "Poor", water: "Safe water", bednet: "With bednet", syndrome: "Diarrhea", pathogen: "Respiratory", site_status: "alert", signal_severity: "High", tier: "High" } };
ok("every active filter has a removable chip", (() => { const o = page(busy).o(); return o.chips.length === 2 + 3 + 11 && o.chips.every((c) => typeof c.clear === "function"); })(), page(busy).o().chips.map((c) => c.label).join(", "));
ok("removing a chip removes only that filter", (() => { const p = page({ dims: { event_type: "Deaths", age_band: "35-44" } }); p.o().chips.find((x) => x.label.startsWith("Event")).clear(); return !p.S.dims.event_type && p.S.dims.age_band === "35-44"; })());
ok("removing a geography chip clears the levels below it", (() => { const p = page({ geo: { country: "Uganda", district: "Buikwe", site: "Buikwe", village: "Buwama" } });
  p.o().chips.find((x) => x.label.startsWith("Site")).clear(); return !p.S.geo.site && !p.S.geo.village && p.S.geo.district === "Buikwe"; })());
ok("a clicked village shows as a removable chip, with no village dropdown", (() => { const p = page(); p.o().gRows[3].pick(); const o = p.o();
  return o.chips.some((c) => c.label === "Village: Buwama") && !o.geoSelects.some((g) => g.label === "Village"); })());
ok("clear all resets every filter (and keeps the tab)", (() => { const p = page({ ...busy, tab: "sentinel" }); p.o().clearAll(); return JSON.stringify(M.serverState(p.S)) === JSON.stringify(M.serverState(M.initialState(SPEC))) && p.S.tab === "sentinel"; })());
ok("the filter bar is Country, District, Site", page().o().geoSelects.map((g) => g.label).join() === "Country,District,Site");
ok("dropdown options cascade", (() => { const o = page({ geo: { country: "Uganda", district: "Mayuge" } }).o(); return o.geoSelects.find((g) => g.label === "Site").options.map((x) => x.v).join() === "All,SINCEP Iganga-Mayuge HDSS"; })());
ok("choosing a level clears the levels below it", (() => { const p = page({ geo: { country: "Uganda", district: "Buikwe", site: "Buikwe", village: "Buwama" } }); p.o().geoSelects[1].onChange({ target: { value: "Mayuge" } }); return p.S.geo.district === "Mayuge" && !p.S.geo.site && !p.S.geo.village; })());
ok("changing the update cycle or the time period clears a selected time point", (() => {
  const p = page({ tp: "2026-06" }); p.o().onCycle({ target: { value: "UC10 (Apr – Sep 2026)" } }); const a = p.S.tp === "" && p.S.cycle.startsWith("UC10");
  const q = page({ tp: "2026-06" }); q.o().onRange({ target: { value: "This year" } }); return a && q.S.tp === "" && q.S.range === "This year"; })());
ok("Update cycle lists the source's cycles; Time period lists the categories", (() => { const o = page().o();
  return o.optCycle.map((x) => x.v).join() === "All,UC10 (Apr – Sep 2026),UC11 (Oct 2026 – Mar 2027)" && o.optRange.map((x) => x.v).join() === "All,This week,Last week,This month,Last month,This quarter,Last quarter,This year,Previous years"; })());
ok("the request carries only the filter state", JSON.stringify(Object.keys(M.serverState(page(busy).S))) === JSON.stringify(["geo", "cycle", "period", "time_point", "dims"]));
const poor = M.renderVals(pageState(DATA["housing Poor"].state), DATA["housing Poor"].data, SPEC, () => {});
ok("own dimension: housing keeps all four categories with the unfiltered counts", poor.housRows.map((r) => r.n).join("-") === "13-21-16-5" && poor.housRows[2].op === 1 && poor.housRows[0].op === 0.3);
ok("the other charts follow the housing selection", poor.watRows.reduce((a, r) => a + Number(r.n), 0) === 16 && poor.netRows.reduce((a, r) => a + Number(r.n), 0) === 16);
const tpj = M.renderVals(pageState(DATA["time point Jun + Births"].state), DATA["time point Jun + Births"].data, SPEC, () => {});
ok("a selected month filters event KPIs but the line chart keeps all months", tpj.kBirths === "6" && tpj.vLabels.length === 7 && tpj.vLabels[2].w === 700, "births in Jun = " + tpj.kBirths);
ok("a long timeline (100 years) keeps every point but labels at most 7, always including the selected one", (() => {
  const base = DATA["no filter"].data, years = Array.from({ length: 100 }, (_, i) => String(1927 + i));
  const data = { ...base, points: years.map((y) => ({ key: y, label: y })), overview: { ...base.overview, vital: { points: years, series: Object.fromEntries(SPEC.options.event_type.map((t) => [t, years.map((_, i) => i % 7)])) } } };
  const o = M.renderVals({ ...M.initialState(SPEC), tp: "1990" }, data, SPEC, () => {});
  return o.vDots.length === 100 * SPEC.options.event_type.length && o.vLabels.length <= 8 && o.vLabels.some((l) => l.t === "1990" && l.w === 700) && o.vDots.every((d) => d.r <= 4.5);
})());
ok("a person or household filter shows the village-scope help", page({ dims: { housing: "Poor" } }).o().scopeHelp === true && page({ dims: { tier: "High" } }).o().scopeHelp === false);

// ------------------------------------------------------------------ 4. coverage: what binds shows, the rest says why
ok("demo: all three tabs and every slot bind", SPEC.tabs.every((t) => t.visible) && Object.values(SPEC.coverage.slots).every((t) => Object.values(t).every((v) => v === "bound")));
const real = page({}, DATA["no filter"].data, REAL).o();
ok("real instance: only the Overview tab is offered, and it opens on it", real.tabs.map((t) => t.label).join() === "Overview" && M.initialState(REAL).tab === "overview");
ok("real instance: geography is Country, District and Site", real.geoSelects.map((g) => g.label).join() === "Country,District,Site");
ok("real instance: Update cycle and Time period are offered", real.showCycle && real.showPeriod);
const realSlots = REAL.coverage.slots.overview, unboundSlots = Object.keys(realSlots).filter((k) => realSlots[k] !== "bound");
ok("real instance: unbound Overview slots carry their registered reason", unboundSlots.length > 0 && unboundSlots.every((k) => !real.slot(k) && /^(No data source registered|Needs definition)/.test(real.slotReason(k))),
  unboundSlots.map((k) => k + " = " + real.slotReason(k)).join(" ; "));
ok("real instance: hidden tabs say why", REAL.tabs.filter((t) => !t.visible).every((t) => Object.values(REAL.coverage.slots[t.id]).every((v) => v !== "bound" && v.reason)));


// ------------------------------------------------------------------ 5. rendered markup
const html = (S0, spec = SPEC, data = DATA["no filter"].data) => renderToStaticMarkup(React.createElement(V.MasterView, { o: page(S0, data, spec).o() }));
const pages = { overview: html({ tab: "overview" }), sentinel: html({ tab: "sentinel" }), geo: html({ tab: "geo" }) };
for (const [tab, h] of Object.entries(pages)) {
  ok(tab + ": no illustrative notice (removed at the user's request)", !h.includes("Illustrative data"));
  ok(tab + ": the fit-one-window classes are on the layout", ["mf-root", "mf-page", "mf-filters", "mf-body", "mf-kpis", "mf-row", "mf-card"].every((c) => h.includes(c)));
  let depth = 0, nested = 0; for (const m of h.matchAll(/<button\b|<\/button>/g)) { if (m[0] === "</button>") depth--; else { depth++; if (depth > 1) nested++; } }
  ok(tab + ": no button inside another button", nested === 0 && depth === 0, "nested " + nested);
  ok(tab + ": every option has a value and a label", (h.match(/<option /g) || []).length > 0 && (h.match(/<option [^>]*>/g) || []).every((x) => /value="/.test(x)) && !/<option[^>]*><\/option>/.test(h));
  ok(tab + ": no unresolved placeholder", !/\{\{|undefined|NaN|\[object Object\]/.test(h));
  ok(tab + ": Update cycle and Time period selects", h.includes(">Update cycle<") && h.includes(">Time period<") && !/Last 3 months|Last 6 months/.test(h));
}
ok("overview shows the overview cards, sentinel and geo show theirs", pages.overview.includes("Vital events") && pages.sentinel.includes("Pathogen") && pages.geo.includes("Hotspot"));
ok("trend points show series, time point and count on hover", (() => { const o = page().o(), h = html({ tab: "overview" }), s = html({ tab: "sentinel" });
  const want = SPEC.options.event_type[0] + ", Apr: " + DATA["no filter"].data.overview.vital.series[SPEC.options.event_type[0]][0];
  return o.vDots[0].tip === want && o.wDots[0].tip.startsWith("Fever, W15: ") && (h.match(/<title>/g) || []).length === o.vDots.length && (s.match(/<title>/g) || []).length === o.wDots.length; })(),
  page().o().vDots[0].tip + " / " + page().o().wDots[0].tip);
ok("the vital events title follows the server's time unit", html({ tab: "overview" }).includes(">Vital events by month</h2>") &&
  renderToStaticMarkup(React.createElement(V.MasterView, { o: M.renderVals({ ...M.initialState(SPEC), tab: "overview" }, { ...DATA["no filter"].data, unit: "year" }, SPEC, () => {}) })).includes(">Vital events by year</h2>"));
ok("village-scope help appears with a person filter", html({ tab: "overview", dims: { housing: "Poor" } }).includes("follow the villages where that selection exists"));
const emptyHtml = renderToStaticMarkup(React.createElement(V.MasterView, { o: M.renderVals(pageState(DATA["empty: tier High in a Low-tier village"].state), DATA["empty: tier High in a Low-tier village"].data, SPEC, () => {}) }));
ok("age and gender bars are a share of the card, not fixed pixels", (() => { const h = pages.overview; return !/width:\s*100px/.test(h) && /mf-pyrow/.test(h); })());
ok("an empty selection shows the message, not blank cards", emptyHtml.includes("No villages match this selection") && !emptyHtml.includes("Hotspot"));
const realHtml = html({ tab: "overview" }, REAL);
ok("real instance renders unbound cards with their reason", unboundSlots.every((k) => realHtml.includes(real.slotReason(k).replace(/&/g, "&amp;").replace(/'/g, "&#x27;").replace(/"/g, "&quot;").slice(0, 40))) && /Needs definition|No data source registered/.test(realHtml));
ok("no person- or household-level identifiers reach the page", !/household_key|person_key|"hh"/.test(JSON.stringify(PAGE.states.map((s) => s.data))));
ok("the committed view is what the converter makes from the design (else: npm run gen:master)",
  fs.readFileSync(path.join(here, "../master-view.tsx"), "utf8") === fs.readFileSync(path.join(here, ".build/master-view.gen.tsx"), "utf8"));

console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
