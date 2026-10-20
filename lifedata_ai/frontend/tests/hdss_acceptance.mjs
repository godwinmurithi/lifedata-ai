// Acceptance tests of the live HDSS template, ported from docs/design/hdss/acceptance_tests.js.
//   npm run test:hdss            (builds the model + view with esbuild, then runs these checks)
//   npm run test:hdss -- --write-fixture   also rewrites lifedata_ai/tests/fixtures/hdss_oracle.json
//
// 0. The oracle (hdss_oracle.mjs) equals the approved design's own numbers for every state.
// 1-4. The live view model (hdss-model.ts), fed oracle aggregates, reproduces the design's behaviour:
//      defaults, wiring, one selection filters every card, the own-dimension rule, cascade, chips, empty state.
// 5-6. The rendered React markup (HdssView, server-rendered) keeps the design's structure: real buttons,
//      no nesting, deep-dive links as 48px sibling links, notice placement.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
import { aggregates, oracleStates, ORDER, DESIGN_DIR, rangeBounds, CYCLE_DATES } from "./hdss_oracle.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const M = require("./.build/hdss-model.cjs");
const V = require("./.build/hdss-view.cjs");
let fails = 0, passes = 0;
const ok = (n, c, d = "") => { if (c) passes++; else { fails++; } console.log((c ? "PASS " : "FAIL ") + n + (d !== "" ? "  [" + d + "]" : "")); };
const num = (s) => Number(String(s).replace(/,/g, ""));

// ------------------------------------------------------------------ 0. oracle == the approved design
const html = fs.readFileSync(path.join(DESIGN_DIR, "design_hdss_template.dc.html"), "utf8");
const script = html.split("data-dc-script data-props='")[1].split("'>")[1].split("</script>")[0];
class DCLogic { constructor(p) { this.props = p || {}; this.state = {}; } setState(p) { this.state = Object.assign({}, this.state, p); } }
const Design = new Function("DCLogic", script + "\nreturn Component;")(DCLogic);
const DIDX = { BANDS: ORDER.age_band, EVN: ["Births", "Deaths", "In-migration", "Out-migration"], education: ["None", "Primary", "Secondary", "Tertiary"],
  occupation: ["Student", "None", "Trader", "Farmer", "Employed", "Homemaker", "Other"], marital: ["Single", "Married", "Divorced", "Widowed"],
  migration: ["None", "In-migrant", "Out-migrant", "Visitor"], bednet: ["Yes", "No"], fuel: ["Firewood", "Charcoal", "Gas", "Electricity"],
  water: ["Tap", "Borehole", "Well", "Other"], roof: ["Tiled", "Iron", "Thatched"], floor: ["Cement", "Earth", "Tiled"], toilet: ["Pit latrine", "VIP latrine", "Flush toilet", "None"],
  asset: ["Radio", "TV", "Phone", "Fridge", "Vehicles"] };
const DKEY = { education: "edu", occupation: "occ", marital: "mar", migration: "mig", bednet: "bed", fuel: "fuel", water: "water", roof: "roof", floor: "floor", toilet: "toilet", asset: "ast" };
function designState(st) {
  // the design has no Time range: give it the range's dates (intersected with explicit dates)
  const [rl, rh] = rangeBounds(st.range || "All");
  const [cl, ch] = st.dims.update_cycle ? CYCLE_DATES[st.dims.update_cycle] : [null, null];
  const d = { period: st.period, from: [st.from, rl, cl].filter(Boolean).sort().at(-1) || "", to: [st.to, rh, ch].filter(Boolean).sort()[0] || "" };
  st = { ...st, dims: Object.fromEntries(Object.entries(st.dims).filter(([k]) => k !== "update_cycle")) };
  for (const [l, v] of Object.entries(st.geo)) d[l] = v;
  for (const [k, v] of Object.entries(st.dims)) {
    if (k === "age_band") d.age = DIDX.BANDS.indexOf(v); else if (k === "event") d.ev = DIDX.EVN.indexOf(v);
    else if (k === "sex") d.sex = v === "Male" ? "M" : "F"; else d[DKEY[k]] = DIDX[k].indexOf(v);
  }
  return d;
}
const designVals = (st, extra = {}) => { const c = new Design({}); Object.assign(c.state, designState(st), extra); return c.renderVals(); };
const byLabel = (labels, counts) => Object.fromEntries(labels.map((l, i) => [l, counts[i]]));
for (const [name, st] of oracleStates()) {
  const d = designVals(st), a = aggregates(st), bad = [];
  if (num(d.fPersons) !== a.totals.persons || num(d.fHouseholds) !== a.totals.households || num(d.fVillages) !== a.totals.villages) bad.push("totals");
  d.kpis.forEach((k, i) => { const want = i < 4 ? a.cards.event.counts[i] : a.cards.age.counts[5]; if (num(k.value) !== want) bad.push("kpi " + k.label); });
  for (const [rows, dim] of [["fuelRows", "fuel"], ["waterRows", "water"], ["eduRows", "education"], ["occRows", "occupation"], ["assetRows", "asset"]]) {
    const want = byLabel(a.cards[dim].labels, a.cards[dim].counts);
    d[rows].forEach((r) => { if (want[r.l] !== r.n) bad.push(dim + " " + r.l); });
  }
  for (const [labels, dim, extra] of [["roofLabels", "roof", {}], ["netLabels", "bednet", {}], ["migLabels", "migration", {}], ["marLabels", "marital", {}],
    ["roofLabels", "floor", { roofVar: "floor" }], ["netLabels", "toilet", { netVar: "toilet" }], ["migLabels", "water", { migVar: "water" }]]) {
    const dd = Object.keys(extra).length ? designVals(st, extra) : d, want = byLabel(a.cards[dim].labels, a.cards[dim].counts);
    dd[labels].forEach((g) => { if (want[g.name] !== g.n) bad.push(dim + " " + g.name); });
  }
  d.ageRows.forEach((r, i) => { const m = Number(r.mlabel.split(": ")[1]), f = Number(r.flabel.split(": ")[1]);
    if (m !== a.cards.age_sex.counts.Male[i] || f !== a.cards.age_sex.counts.Female[i]) bad.push("age " + r.a); });
  if (d.mapPins.length !== a.map.length) bad.push("map pins");
  ok("oracle = design: " + name, bad.length === 0, bad.join(", "));
}

// ------------------------------------------------------------------ helpers for the live model
function spec(deep = {}) {
  const slots = ["header.count", "kpi.births", "kpi.deaths", "kpi.in_migration", "kpi.out_migration", "kpi.under_five", "card.persons_households", "card.age_gender",
    "card.coordinates", "card.cooking_fuel", "card.water_source", "card.education", "card.occupation", "card.roof_floor", "card.bednet_toilet", "card.migration_water",
    "card.marital", "card.lifeconnect", "card.population_households", "card.assets", "footer.counts"];
  const filters = ["district", "county", "subcounty", "parish", "village", "time_period", "time_range", ...M.DIMS];
  return { title: "HDSS template", status: "draft", slots, filters, privacy: { synthetic: true, min_cell_size: 0 }, population_reference: 530,
    deep_dive: { Births: null, Deaths: null, "In-migration": null, "Out-migration": null, "Under five": null, ...deep }, labels: ORDER,
    coverage: { slots: Object.fromEntries(slots.map((s) => [s, "bound"])), filters: Object.fromEntries(filters.map((f) => [f, "bound"])) } };
}
// A page: a state holder whose setter replaces the state, like React's setState.
function page(state = {}, sp = spec()) {
  const h = { S: { ...M.DEFAULTS, dims: {}, ...state } };
  const set = (s) => { h.S = s; };
  h.o = () => M.renderVals(h.S, aggregates(M.serverState(h.S)), sp, set);
  return h;
}
const vals = (state, sp) => page(state, sp).o();
const o0 = vals();

console.log("--- 1. defaults reproduce every number in the screenshot");
ok("totals (gauge, villages, map)", o0.gaugeValue === "420" && [o0.fPersons, o0.fHouseholds, o0.fVillages].join() === "420,84,6" && o0.mapCount === "6 locations");
ok("KPI cards", o0.kpis.map((k) => k.label + " " + k.value).join() === "Births 19,Deaths 20,In-migration 63,Out-migration 49,Under five 78");
ok("gauge shows 420, full", o0.gaugeValue === "420" && o0.gaugeDash.startsWith("100.00"));
ok("cooking fuel", o0.fuelRows.map((r) => r.l + " " + r.n).join() === "Firewood 217,Charcoal 136,Gas 47,Electricity 20");
ok("water source", o0.waterRows.map((r) => r.l + " " + r.n).join() === "Tap 75,Borehole 91,Well 73,Other 181");
ok("education (ranked)", o0.eduRows.map((r) => r.l + " " + r.n).join() === "None 155,Secondary 97,Tertiary 91,Primary 77");
ok("occupation (top six, ranked)", o0.occRows.map((r) => r.l + " " + r.n).join() === "Student 146,None 78,Trader 50,Farmer 42,Employed 40,Homemaker 33");
ok("roof donut: 158 recorded", o0.roofTotal === "158" && o0.roofLabels.map((l) => l.name + " " + l.n).join() === "Iron 50,Thatched 58,Tiled 50");
ok("bednet donut", o0.netTotal === "420" && o0.netLabels.map((l) => l.name + " " + l.n).join() === "Yes 308,No 112");
ok("migration donut", o0.migTotal === "420" && o0.migLabels.map((l) => l.name + " " + l.n).join() === "None 258,In-migrant 63,Visitor 50,Out-migrant 49");
ok("marital donut", o0.marTotal === "420" && o0.marLabels.map((l) => l.name + " " + l.n).join() === "Single 245,Divorced 68,Married 60,Widowed 47");
ok("population and household bars", o0.popBars.map((b) => b.l + " " + b.n).join() === "Population 420,Households 84");
ok("population bars use the approved 530 reference", o0.popRef === 530 && o0.popBars[0].h === (420 / 530 * 100).toFixed(1));
ok("household assets", o0.assetRows.map((r) => r.l + " " + r.n).join() === "Radio 275,TV 169,Phone 239,Fridge 46,Vehicles 42");
ok("age and gender: 6 bands, under five = 78", o0.ageRows.length === 6 && o0.ageRows[5].alabel === "Age 0-4: 78");
ok("illustrative data line shown for the synthetic dataset", o0.illustrative === true);

console.log("--- 2. every element is wired: click sets the shared filter, click again clears it");
function wired(name, get, check) {
  const p = page(); get(p.o())(); const set = check(p.S); get(p.o())(); const cleared = !check(p.S) && Object.keys(p.S.dims).length === 0;
  ok(name, set && cleared, JSON.stringify(p.S.dims));
}
["Births", "Deaths", "In-migration", "Out-migration"].forEach((n, k) => wired("KPI card " + n, (o) => o.kpis[k].pick, (S) => S.dims.event === n));
wired("KPI card Under five", (o) => o.kpis[4].pick, (S) => S.dims.age_band === "0-4" && !S.dims.sex);
for (let b = 0; b < 6; b++) wired("age pill (men) band " + b, (o) => o.ageRows[b].pm, (S) => S.dims.age_band === ORDER.age_band[b] && S.dims.sex === "Male");
ok("age: men pill = band+M, label = band, women pill = band+F", (() => { const p = page(); p.o().ageRows[2].pm(); const a = [p.S.dims.age_band, p.S.dims.sex].join();
  p.o().ageRows[2].pa(); const b = [p.S.dims.age_band, p.S.dims.sex || ""].join(); p.o().ageRows[2].pf(); const c = [p.S.dims.age_band, p.S.dims.sex].join();
  return a === "25-44,Male" && b === "25-44," && c === "25-44,Female"; })());
for (const [dim, list, n] of [["fuel", "fuelRows", 4], ["water", "waterRows", 4], ["education", "eduRows", 4], ["occupation", "occRows", 6]])
  for (let i = 0; i < n; i++) ok(dim + " row " + i + " toggles", (() => { const p = page(); const row = p.o()[list][i]; row.pick(); const a = p.S.dims[dim] === row.l; p.o()[list][i].pick(); return a && !p.S.dims[dim]; })());
for (const [label, dim, prefix] of [["roof", "roof", "roof"], ["bednet", "bednet", "net"], ["migration", "migration", "mig"], ["marital", "marital", "mar"]])
  ok(label + " donut: every label and slice is clickable and sets the filter", (() => { let good = true; const n = vals()[prefix + "Labels"].length;
    for (let j = 0; j < n; j++) { const p = page(); const g = p.o()[prefix + "Labels"][j]; g.pick(); good = good && p.S.dims[dim] === g.name; }
    for (let j = 0; j < vals()[prefix + "Segs"].length; j++) { const p = page(); p.o()[prefix + "Segs"][j].pick(); good = good && !!p.S.dims[dim]; }
    return good && n > 0; })());
ok("the other variables of each switch card filter too (floor, toilet, water)", (() => { let g = true;
  for (const [sv, v, dim, prefix] of [["roofVar", "floor", "floor", "roof"], ["netVar", "toilet", "toilet", "net"], ["migVar", "water", "water", "mig"]]) {
    const p = page({ [sv]: v }); const lab = p.o()[prefix + "Labels"][0]; lab.pick(); g = g && p.S.dims[dim] === lab.name; }
  return g; })());
ok("switch chips change the card's variable", (() => { const p = page(); p.o().roofChips[1].pick(); const a = p.S.roofVar === "floor"; p.o().netChips[1].pick(); const b = p.S.netVar === "toilet";
  p.o().migChips[1].pick(); return a && b && p.S.migVar === "water"; })());
ok("switching variable shows its own categories", (() => { const o = vals({ roofVar: "floor", netVar: "toilet", migVar: "water" });
  return o.roofLabels.map((l) => l.name).sort().join() === "Cement,Earth,Tiled" && o.netLabels.length === 4 && o.migLabels.map((l) => l.name).join() === "Tap,Borehole,Well,Other"; })());
ok("household assets bars toggle", [0, 1, 2, 3, 4].every((k) => { const p = page(); p.o().assetRows[k].pick(); const a = p.S.dims.asset === ORDER.asset[k]; p.o().assetRows[k].pick(); return a && !p.S.dims.asset; }));
ok("population / household bars switch the gauge unit", (() => { const p = page(); p.o().popBars[1].pick(); const a = p.S.unit === "Households"; p.o().popBars[0].pick(); return a && p.S.unit === "Persons"; })());
ok("map pin sets the whole village path", (() => { const p = page(); p.o().mapPins[2].pick(); return M.LEVELS.every((l) => p.S[l] !== "All"); })());
ok("geography dropdowns cascade and reset lower levels", (() => { const p = page(); p.o().onDistrict({ target: { value: "Mayuge" } });
  const a = p.o().optVillage.length === 4 && p.o().optCounty.length === 2; p.o().onCounty({ target: { value: "Bunya" } }); p.o().onVillage({ target: { value: "Kabaale" } });
  p.o().onDistrict({ target: { value: "Iganga" } }); return a && p.S.county === "All" && p.S.village === "All"; })());
ok("time range dropdown offers the calendar periods and filters through the server", (() => { const p = page();
  const opts = p.o().optRange.map((x) => x.v).join("|"); p.o().onRange({ target: { value: "Last month" } });
  return opts === "All|This week|Last week|This month|Last month|This quarter|Last quarter|This year|Previous years" && M.serverState(p.S).range === "Last month"
    && p.o().chips.some((c) => c.label === "Time period: Last month") && M.filterQuery(p.S).includes("range=Last%20month"); })());
ok("clear all resets everything including drafts", (() => { const p = page({ dims: { event: "Deaths", age_band: "25-44", sex: "Male", fuel: "Gas" }, district: "Iganga", fromD: "2026-01-01", from: "2026-01-01", unit: "Households", note: "Births" });
  p.o().clearAll(); return JSON.stringify(p.S) === JSON.stringify({ ...M.DEFAULTS, dims: {} }); })());

console.log("--- 3. one selection filters every card: numbers match the independent calculation");
for (const [name, st] of oracleStates()) {
  const S = { ...M.DEFAULTS, ...st.geo, period: st.period, from: st.from, to: st.to, dims: st.dims };
  const o = M.renderVals(S, aggregates(st), spec(), () => {}), a = aggregates(st);
  const got = [o.fPersons, o.fHouseholds, o.fVillages, o.gaugeValue].map(num).join(), want = [a.totals.persons, a.totals.households, a.totals.villages, a.totals.persons].join();
  ok(name, got === want, "persons, households, villages, gauge = " + got + (got === want ? "" : "  expected " + want));
}

console.log("--- 4. a card that is itself a filter keeps showing its alternatives; the others follow");
const fw = vals({ dims: { fuel: "Firewood" } });
ok("cooking fuel keeps all four rows with unfiltered counts; selected highlighted, others dimmed", fw.fuelRows.map((r) => r.n).join() === "217,136,47,20" && fw.fuelRows[0].op === 1 && fw.fuelRows[1].op === 0.35);
ok("water source follows the fuel selection", fw.waterRows.reduce((s, r) => s + num(r.n), 0) === 217);
const ev = vals({ dims: { event: "Births" } });
ok("event cards keep all four values while Births is selected; other cards narrow", ev.kpis.slice(0, 4).map((k) => k.value).join() === "19,20,63,49" && ev.kpis[0].op === 1 && ev.kpis[1].op === 0.35 && num(ev.fPersons) === 19);
const ag = vals({ dims: { age_band: "0-4" } });
ok("age pills keep every band while one is selected; persons shows only that band", ag.ageRows.length === 6 && ag.ageRows[0].mw > 0 && ag.gaugeValue === "78" && ag.ageRows[5].ao === 1 && ag.ageRows[0].ao === 0.3);
ok("the Under five card stays 78 under another band on its own dimension", vals({ dims: { age_band: "65+" } }).kpis[4].value === "78");
ok("households unit: gauge counts households", (() => { const o = vals({ unit: "Households", dims: { fuel: "Electricity" } }); return o.gaugeLabel === "Households" && num(o.gaugeValue) === aggregates({ dims: { fuel: "Electricity" } }).totals.households; })());
ok("a filter narrows the totals (20 of 420)", vals({ dims: { fuel: "Electricity" } }).gaugeValue === "20" && vals({ dims: { fuel: "Electricity" } }).gaugeDash.startsWith((20 / 420 * 100).toFixed(2)));
ok("the map zooms to the selection", vals({ district: "Mayuge" }).mapPins.length === 3 && vals({ district: "Mayuge" }).mapCount === "3 locations");
ok("empty selection shows the message", (() => { const o = vals({ dims: { event: "Births", age_band: "65+" } }); return o.isEmpty === true && o.showBody === false; })());
ok("trend lines follow the displayed time window and add up to their KPI", (() => {
  const sum = (k) => k.points.reduce((s, p) => s + num(p.value), 0);
  const all = vals(), q = vals({ range: "Last quarter" }), w = vals({ range: "This week" }), m = vals({ range: "This month", period: "Last 3 months" });
  const adds = [all, q, w, m].every((o) => o.kpis.every((k) => sum(k) === num(k.value)));
  return adds && /Nov 2025/.test(all.kpis[0].points[0].label) && /Oct 2026/.test(all.kpis[0].points.at(-1).label)
    && q.kpis[0].points.every((p) => /^Week of/.test(p.label)) && w.kpis[0].points.every((p) => /^\d+ Oct 2026$/.test(p.label));
})());
ok("every trend point is hoverable with its time and figure", vals().kpis.every((k) => k.points.length >= 2 && k.points.every((p) => p.label && p.value !== undefined && p.x >= 0 && p.x <= 300)));
ok("every active dimension has a removable chip", (() => { const p = page({ dims: { event: "Deaths", age_band: "15-24", sex: "Male", fuel: "Gas", water: "Borehole", education: "Secondary", occupation: "Farmer", marital: "Married",
  migration: "Out-migrant", bednet: "Yes", roof: "Iron", floor: "Cement", toilet: "Flush toilet", asset: "Fridge" }, district: "Iganga", period: "Last 6 months", from: "2026-01-01" });
  const chips = p.o().chips; if (chips.length !== 17) return false; chips.forEach(() => p.o().chips[0].clear());
  return Object.keys(p.S.dims).length === 0 && p.S.district === "All" && p.S.period === "All" && !p.S.from; })());
ok("removing a geography chip resets the levels below it", (() => { const p = page({ district: "Iganga", county: "Kigulu South", village: "Kisoko" }); p.o().chips[0].clear(); return p.S.county === "All" && p.S.village === "All"; })());

console.log("--- 5/6. rendered markup: real buttons, deep dives, notice");
const React = require("react"), { renderToStaticMarkup } = require("react-dom/server");
const markup = (state = {}, sp = spec()) => { const S = { ...M.DEFAULTS, dims: {}, ...state };
  return renderToStaticMarkup(React.createElement(V.HdssView, { spec: sp, data: aggregates(M.serverState(S)), S, setS: () => {}, busy: false, error: "", canEdit: true, publicationId: "x", onStatus: () => {} })); };
const m0 = markup();
let depth = 0, nested = 0; for (const t of m0.matchAll(/<button\b|<\/button>|<a\b|<\/a>/g)) { if (t[0][1] === "/") depth--; else { depth++; if (depth > 1) nested++; } }
ok("no link or button sits inside another link or button", nested === 0 && depth === 0, "nested " + nested);
const links = [...m0.matchAll(/<a [^>]*aria-label="Deep dive: ([^"]+)"[^>]*>(.*?)<\/a>/g)];
ok("five deep-dive links, each labelled with its card", links.length === 5 && links.map((l) => l[1].split(" (")[0]).join() === "Births,Deaths,In-migration,Out-migration,Under five");
ok("deep-dive control shows the supplied icon (30px) from the module's static assets", links.every((l) => /src="\/lifedata-ai\/static\/dashboards\/hdss\/deepdive_icon\.png"/.test(l[2]) && /width:30px;height:30px/.test(l[2])));
ok("the deep-dive link is a sibling of the card button, not inside it", /<\/button><a href="#"/.test(m0) && !/<button[^>]*>(?:(?!<\/button>)[\s\S])*<a /.test(m0));
ok("dimming a card never dims its deep-dive link", (() => { const m = markup({ dims: { event: "Births" } }); return /opacity:0.35/.test(m) && !/<a [^>]*opacity/.test(m); })());
ok("unlinked cards: href is # and the label says so", o0.kpis.every((k) => k.link === "#" && /link not set yet/.test(k.dlabel)));
ok("clicking an unlinked deep dive shows a notice and does not navigate", (() => { const p = page(); let prevented = false; p.o().kpis[1].dive({ preventDefault: () => { prevented = true; } }); const o = p.o();
  return prevented && o.hasNote && /Deaths/.test(o.noteText); })());
ok("the notice is part of the page, between the cards and the grid", (() => { const m = markup({ note: "Deaths" }); const i = m.indexOf('role="status"'); return i > m.lastIndexOf("Deep dive: Under five") && i < m.indexOf('class="hdss-grid"'); })());
const L1 = vals({ district: "Mayuge", dims: { fuel: "Firewood" } }, spec({ Births: "https://example.org/births", Deaths: "https://example.org/dive?x=1" })).kpis;
const L0 = vals({}, spec({ Births: "https://example.org/births", Deaths: "https://example.org/dive?x=1" })).kpis;
const L2 = vals({ dims: { age_band: "25-44", sex: "Female" }, period: "Last 3 months", from: "2026-02-01" }, spec({ Births: "https://example.org/births" })).kpis;
ok("linked card: address + the card name, no stray separators when nothing is filtered", L0[0].link === "https://example.org/births?card=Births", L0[0].link);
ok("linked card carries the current filters to the other dashboard", L1[0].link === "https://example.org/births?district=Mayuge&fuel=Firewood&card=Births", L1[0].link);
ok("an address that already has a query keeps it", L1[1].link === "https://example.org/dive?x=1&district=Mayuge&fuel=Firewood&card=Deaths", L1[1].link);
ok("age, sex, period and dates are passed too", L2[0].link === "https://example.org/births?period=Last%203%20months&from=2026-02-01&age=25-44&sex=Female&card=Births", L2[0].link);
ok("a linked deep dive navigates normally (no notice, default not prevented)", (() => { const p = page({}, spec({ Births: "https://example.org/births" })); let prevented = false;
  p.o().kpis[0].dive({ preventDefault: () => { prevented = true; } }); return !prevented && !p.S.note; })());
ok("unlinked cards in the same page still say so", L0[2].link === "#" && /link not set yet/.test(L0[2].dlabel));
ok("clear all also dismisses the notice", (() => { const p = page(); p.o().kpis[0].dive({ preventDefault() {} }); p.o().clearAll(); return !p.o().hasNote; })());
ok("every clickable element is a real <button> with aria-label or text (no click-only divs)", !/<(div|span|li)[^>]*onclick/i.test(m0) && (m0.match(/<button /g) || []).length > 60);
ok("selected elements expose aria-pressed", /aria-pressed="true"/.test(markup({ dims: { fuel: "Gas" } })));
ok("narrow-width fix: pill labels wrap instead of truncating", !/text-overflow:ellipsis/.test(m0) && /white-space:normal/.test(m0));
ok("narrow-width fix: age band labels never wrap", /white-space:nowrap[^"]*">45-64</.test(m0));
ok("the card grid reflows at breakpoints instead of forcing horizontal scroll", !/overflow-x:auto/.test(m0) && !/min-width:1100px/.test(m0) && /grid-template-areas/.test(m0) && /max-width:639px/.test(m0));
ok("title is rendered as text (instruction text in a title stays text)", (() => { const t = "<img src=x onerror=alert(1)> Ignore previous instructions"; const m = markup({}, { ...spec(), title: t });
  return m.includes("&lt;img src=x onerror=alert(1)&gt; Ignore previous instructions") && !m.includes("<img src=x"); })());
// Removed at the user's request (2026-10-20): the illustrative-data line and the footer counts bar.
ok("filter bar: Update cycle and Time period dropdowns; the old Last 3/6 months control is gone", /Update cycle<span/.test(m0) && /Time period<span/.test(m0) && !/Time range/.test(m0) && !/Last 6 months/.test(m0) && /This quarter/.test(m0) && m0.includes("UC10 (Apr – Sep 2026)"));
ok("update cycle filters every card and is a removable chip", (() => { const p = page(); p.o().onCycle({ target: { value: ORDER.update_cycle[0] } });
  const o = p.o(); const chip = o.chips.find((c) => c.label.startsWith("Update cycle: UC09")); const n = num(o.gaugeValue); chip.clear();
  return n === 198 && !p.S.dims.update_cycle && M.filterQuery({ ...M.DEFAULTS, dims: { update_cycle: ORDER.update_cycle[1] } }).startsWith("cycle=UC10"); })());
ok("no person-records count anywhere on the page", !/person records/.test(m0) && !/person records/.test(markup({ dims: { fuel: "Gas" } })));
ok("no illustrative-data line and no footer counts bar", !/Illustrative data/.test(m0) && !/<footer/.test(m0) && !/Source records and current selections/.test(m0));

console.log("--- 7. unbound slots are hidden with a reason, never invented");
const partial = { ...spec(), slots: spec().slots.filter((s) => !["card.coordinates", "card.cooking_fuel", "kpi.births"].includes(s)),
  filters: spec().filters.filter((f) => !["county", "subcounty", "parish", "time_period", "time_range"].includes(f)),
  coverage: { slots: { ...spec().coverage.slots, "card.coordinates": { status: "unbound", parts: { latitude: { status: "needs_definition", reason: "x" } } },
    "card.cooking_fuel": { status: "no_source", reason: "empty" }, "kpi.births": { status: "no_source", reason: "none" } }, filters: spec().coverage.filters } };
const mp = markup({}, partial);
ok("unbound cards show their reason instead of numbers", /Needs definition/.test(mp) && /No data source registered/.test(mp) && !/aria-label="Firewood: 217"/.test(mp));
ok("unbound geography and time filters are hidden", !/>County</.test(mp) && !/>Time period</.test(mp) && !/From date/.test(mp) && />District</.test(mp));
ok("an unbound KPI shows no number but keeps its deep-dive link", /aria-label="Deep dive: Births/.test(mp) && !/aria-label="[^"]*"[^>]*><span[^>]*><span[^>]*>Births<\/span><span[^>]*>19</.test(mp));

// ------------------------------------------------------------------ fixture for the server tests
if (process.argv.includes("--write-fixture")) {
  const fixture = oracleStates().map(([name, st]) => ({ name, state: st, expected: aggregates(st) }));
  const target = path.resolve(here, "../../tests/fixtures/hdss_oracle.json");
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, JSON.stringify(fixture, null, 1) + "\n");
  console.log("wrote " + target + " (" + fixture.length + " states)");
}
console.log(fails ? `\nFAILED: ${fails} (passed ${passes})` : `\nALL PASSED (${passes})`);
process.exit(fails ? 1 : 0);
