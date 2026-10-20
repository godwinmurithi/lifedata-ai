// Oracle for the Master dashboard template: the APPROVED DESIGN ITSELF.
// Runs the design's own script (docs/design/master/design_master_dashboard.dc.html) on sample_data.json and reads
// the numbers it shows, for any filter state. Three non-semantic patches only:
//   1. its month list (baseIdx) can be given explicitly, so the new Update cycle / Time period filters can be
//      expressed (the design only had All / Last 6 / Last 3 months);
//   2. the vital-events and weekly-syndrome series values are exported (the design only exports their geometry).
// normalise() turns the design's output into the neutral shape the server tests and page tests compare against.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
export const MASTER_DIR = path.resolve(here, "../../../docs/design/master");
const html = fs.readFileSync(path.join(MASTER_DIR, "design_master_dashboard.dc.html"), "utf8");
let script = html.split("data-dc-script data-props='")[1].split("'>")[1].split("</script>")[0];
const patch = (a, b) => { if (script.split(a).length !== 2) throw new Error("design patch anchor: " + a.slice(0, 50)); script = script.replace(a, b); };
patch("const baseIdx = S.period === 'Last 3 months'", "const baseIdx = S.idx ? S.idx : S.period === 'Last 3 months'");
patch("out.vLines = vl.lines;", "out._vPer = C1.per; out._vs = vs.map((s) => s.vals); out.vLines = vl.lines;");
patch("out.wLines = wl.lines;", "out._wPer = C5.per; out._ws = ws.map((s) => s.vals); out.wLines = wl.lines;");
// 3. drawing only: a time window with no months gives an empty line instead of a crash (the design never had one)
patch("function smooth(pts) {", "function smooth(pts) {\n  if (!pts.length) return '';");
class DCLogic { constructor(p) { this.props = p || {}; this.state = {}; } setState(p) { this.state = Object.assign({}, this.state, p); } }
export const Design = new Function("DCLogic", script + "\nreturn Component;")(DCLogic);

export const AGES = ["65+", "55-64", "45-54", "35-44", "25-34", "15-24", "5-14", "0-4"];
export const EVN = ["Births", "Deaths", "In-migration", "Out-migration", "Pregnancies"];
export const HOUSN = ["Good", "Moderate", "Poor", "Dilapidated"], WATN = ["Safe water", "Improved toilet", "Open defecation"], NETN = ["With bednet", "Without bednet"];
export const SYNN = ["Fever", "Respiratory", "Diarrhea", "Rash"], PATHN = ["Malaria", "Respiratory", "Diarrheal", "Measles"];
export const MONTH_KEYS = ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]; // the design's Apr..Oct, 2026
const REF = Date.UTC(2026, 9, 20);
const iso = (ms) => new Date(ms).toISOString().slice(0, 10);
const D = (y, m, d) => Date.UTC(y, m, d);
const CYCLES = { "UC10 (Apr – Sep 2026)": ["2026-04-01", "2026-09-30"], "UC11 (Oct 2026 – Mar 2027)": ["2026-10-01", "2027-03-31"] };
export function rangeBounds(name) {
  const r = new Date(REF), y = r.getUTCFullYear(), m = r.getUTCMonth(), wd = (r.getUTCDay() + 6) % 7, monday = REF - wd * 864e5, q = Math.floor(m / 3) * 3;
  const b = { "This week": [monday, monday + 6 * 864e5], "Last week": [monday - 7 * 864e5, monday - 864e5], "This month": [D(y, m, 1), D(y, m + 1, 0)],
    "Last month": [D(y, m - 1, 1), D(y, m, 0)], "This quarter": [D(y, q, 1), D(y, q + 3, 0)], "Last quarter": [D(y, q - 3, 1), D(y, q, 0)],
    "This year": [D(y, 0, 1), D(y, 11, 31)], "Previous years": [null, D(y - 1, 11, 31)] }[name] || [null, null];
  return b.map((v) => (v === null ? null : iso(v)));
}
// Demo records are dated the 15th of their month, so a month counts when its 15th is inside the window.
export function monthIndexes(st) {
  const [rl, rh] = rangeBounds(st.period || "All"), [cl, ch] = st.cycle && st.cycle !== "All" ? CYCLES[st.cycle] : [null, null];
  const lo = [rl, cl].filter(Boolean).sort().at(-1) || null, hi = [rh, ch].filter(Boolean).sort()[0] || null, ref = iso(REF);
  return MONTH_KEYS.map((k, i) => [k + "-15", i]).filter(([d]) => (!lo || d >= lo) && (!hi || d <= hi) && d <= ref).map(([, i]) => i);
}
export const SITE_DISTRICTS = { "SINCEP Iganga-Mayuge HDSS": ["Iganga", "Mayuge"], Buikwe: ["Buikwe"] };
export function designState(st) {
  const d = {}, dims = st.dims || {};
  for (const [l, v] of Object.entries(st.geo || {})) if (l !== "site") d[l] = v;
  // the design has no Site level: a site is its districts (master_bindings.yaml, master-demo villages.levels.site)
  if (st.geo && st.geo.site) {
    const ds = SITE_DISTRICTS[st.geo.site];
    if (!(d.district && ds.includes(d.district))) { if (ds.length !== 1) throw new Error("the design cannot express site " + st.geo.site + " alone"); d.district = ds[0]; }
  }
  if (dims.tier) d.tier = dims.tier;
  if (dims.event_type) d.evT = EVN.indexOf(dims.event_type);
  if (dims.age_band) d.age = AGES.indexOf(dims.age_band);
  if (dims.sex) d.sex = dims.sex === "Male" ? "M" : "F";
  if (dims.housing) d.hous = HOUSN.indexOf(dims.housing);
  if (dims.water) d.wat = WATN.indexOf(dims.water);
  if (dims.bednet) d.net = NETN.indexOf(dims.bednet);
  if (dims.syndrome) d.syn = SYNN.indexOf(dims.syndrome);
  if (dims.pathogen) d.path = PATHN.indexOf(dims.pathogen);
  if (dims.site_status) d.status = dims.site_status;
  if (dims.signal_severity) d.sev = dims.signal_severity;
  d.idx = monthIndexes(st);
  if (st.time_point) d.tIdx = MONTH_KEYS.indexOf(st.time_point);
  return d;
}
export function designVals(st, extra = {}) { const c = new Design({}); Object.assign(c.state, designState(st), extra); return c.renderVals(); }

const num = (s) => (s === "–" || s === undefined ? null : Number(String(s).replace(/[,%dkm]/g, "")));
const inParens = (s) => Number(s.match(/\((\d+)\)\s*$/)[1]);
const series = (per, vals, names) => Object.fromEntries(names.map((n, i) => [n, Object.fromEntries(per.map((m, j) => [MONTH_KEYS[m], vals[i][j]]).filter(([, v]) => v))]));
/** The neutral comparison shape (shared with test_master_template.py's normalise()). */
export function normalise(o) {
  return {
    empty: o.isEmpty,
    overview: o.isEmpty ? null : {
      residents: num(o.kRes), households: num(o.kHh), under_five: num(o.kU5), births: num(o.kBirths), deaths: num(o.kDeaths), net_migration: Number(o.kNet),
      ever_registered: num(o.sEver), currently_pregnant: num(o.sPreg), under_five_ever: num(o.sU5), outpatient_visits: num(o.sOpd),
      vital: series(o._vPer, o._vs, EVN), mix: Object.fromEntries(o.mixLegend.map((l) => [l.t.replace(/ \(\d+\)$/, ""), inParens(l.t)])),
      pyramid: { Male: o.pyRows.map((r) => r.m), Female: o.pyRows.map((r) => r.f) },
      housing: Object.fromEntries(o.housRows.map((r) => [r.l, r.n])), water: Object.fromEntries(o.watRows.map((r) => [r.l, r.n])),
      bednet: Object.fromEntries(o.netRows.map((r) => [r.l, r.n])),
      pins: o.ovPins.map((p) => [p.label.split(",")[0], Number(p.label.match(/\((\d+) residents\)/)[1])]).sort(),
    },
    sentinel: o.isEmpty ? null : {
      sites: num(o.sKSites), reports: num(o.sKReports), signals: num(o.sKSignals), rrt: num(o.sKRrt), lab: num(o.sKLab), lag: num(o.sKLag),
      weekly: series(o._wPer, o._ws, SYNN), site_pins: o.sPins.map((p) => p.label).sort(),
      status_counts: Object.fromEntries(o.statusItems.map((s) => [s.label.split(" ")[0], inParens(s.label)])),
      pathogen: Object.fromEntries(o.pathLegend.map((l) => [l.t.replace(/ \(\d+\)$/, ""), inParens(l.t)])),
      signal_rows: o.sigRows.map((r) => [r.fac, r.syn, r.sev]),
    },
    geo: o.isEmpty ? null : {
      tier_counts: Object.fromEntries(o.tierItems.map((t) => [t.label.split(" ")[0], inParens(t.label)])), clusters: num(o.gKClusters), high_risk: num(o.gKHigh),
      pop_at_risk: num(o.gKPop), mean_vulnerability: num(o.gKMean), radius: num(o.gKRad), p_value: num(o.gKP),
      pins: o.gPins.map((p) => p.label).sort(), scatter: o.gPts.map((p) => p.label), hotspots: o.gRows.map((r) => [r.name, r.cases, r.tier]),
    },
  };
}

// Every dimension alone, the design's four combinations, geography, and the new time filters.
export function oracleStates() {
  const s = (dims = {}, extra = {}) => ({ geo: {}, cycle: "All", period: "All", time_point: "", dims, ...extra });
  const out = [["no filter", s()]];
  EVN.forEach((t) => out.push(["event " + t, s({ event_type: t })]));
  AGES.forEach((a) => out.push(["age " + a, s({ age_band: a })]));
  out.push(["men aged 25-34", s({ age_band: "25-34", sex: "Male" })], ["women only", s({ sex: "Female" })]);
  HOUSN.forEach((x) => out.push(["housing " + x, s({ housing: x })])); WATN.forEach((x) => out.push(["water " + x, s({ water: x })]));
  NETN.forEach((x) => out.push(["bednet " + x, s({ bednet: x })])); SYNN.forEach((x) => out.push(["syndrome " + x, s({ syndrome: x })]));
  PATHN.forEach((x) => out.push(["pathogen " + x, s({ pathogen: x })]));
  ["alert", "watch", "normal"].forEach((x) => out.push(["site status " + x, s({ site_status: x })]));
  ["High", "Moderate", "Low"].forEach((x) => out.push(["signal severity " + x, s({ signal_severity: x })]));
  ["High", "Moderate", "Low"].forEach((x) => out.push(["tier " + x, s({ tier: x })]));
  out.push(["combined: poor housing + under-fives", s({ housing: "Poor", age_band: "0-4" })]);
  out.push(["combined: with bednet + births villages", s({ bednet: "With bednet", event_type: "Births" })]);
  out.push(["combined: syndrome Fever + status alert", s({ syndrome: "Fever", site_status: "alert" })]);
  out.push(["combined: tier High + deaths", s({ tier: "High", event_type: "Deaths" })]);
  out.push(["district Iganga", s({}, { geo: { country: "Uganda", district: "Iganga" } })]);
  out.push(["site Buikwe", s({}, { geo: { country: "Uganda", site: "Buikwe" } })]);
  out.push(["district Mayuge + site SINCEP", s({}, { geo: { country: "Uganda", district: "Mayuge", site: "SINCEP Iganga-Mayuge HDSS" } })]);
  out.push(["village Mutukula", s({}, { geo: { country: "Uganda", district: "Iganga", village: "Mutukula" } })]);
  out.push(["empty: tier High in a Low-tier village", s({ tier: "High" }, { geo: { country: "Uganda", district: "Iganga", village: "Mutukula" } })]);
  for (const c of Object.keys(CYCLES)) out.push(["update cycle " + c.slice(0, 4), s({}, { cycle: c })]);
  for (const p of ["This week", "Last week", "This month", "Last month", "This quarter", "Last quarter", "This year", "Previous years"])
    out.push(["time period " + p, s({}, { period: p })]);
  out.push(["time point Jun", s({}, { time_point: "2026-06" })], ["time point Jun + Births", s({ event_type: "Births" }, { time_point: "2026-06" })]);
  out.push(["UC10 + Last quarter + Fever", s({ syndrome: "Fever" }, { cycle: Object.keys(CYCLES)[0], period: "Last quarter" })]);
  return out;
}

if (process.argv[1] && process.argv[1].endsWith("master_oracle.mjs") && process.argv[2]) {
  const fixture = oracleStates().map(([name, state]) => ({ name, state, expected: normalise(designVals(state)) }));
  fs.writeFileSync(process.argv[2], JSON.stringify(fixture, null, 1) + "\n");
  console.log("wrote " + fixture.length + " states");
}
