// Brute-force oracle for the HDSS template: computes, from docs/design/hdss/sample_data.json, exactly what
// POST /dashboards/<id>/query must return for a filter state (same JSON shape as the server's `data`).
// Written independently of the server engine and of the view model, in the style of the design's own
// acceptance_tests.js `expected()`. hdss_acceptance.mjs first proves it equals the design's numbers.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
export const DESIGN_DIR = path.resolve(here, "../../../docs/design/hdss");
const R = JSON.parse(fs.readFileSync(path.join(DESIGN_DIR, "sample_data.json"), "utf8"));

const BANDS = ["65+", "45-64", "25-44", "15-24", "5-14", "0-4"];
const EVN = ["Births", "Deaths", "In-migration", "Out-migration"];
const NAMES = {
  education: ["None", "Primary", "Secondary", "Tertiary"], occupation: ["Student", "None", "Trader", "Farmer", "Employed", "Homemaker", "Other"],
  marital: ["Single", "Married", "Divorced", "Widowed"], migration: ["None", "In-migrant", "Out-migrant", "Visitor"], bednet: ["Yes", "No"],
  fuel: ["Firewood", "Charcoal", "Gas", "Electricity"], water: ["Tap", "Borehole", "Well", "Other"], roof: ["Tiled", "Iron", "Thatched"],
  floor: ["Cement", "Earth", "Tiled"], toilet: ["Pit latrine", "VIP latrine", "Flush toilet", "None"], asset: ["Radio", "TV", "Phone", "Fridge", "Vehicles"],
};
// Display order registered for the demo dataset in lifedata_ai/hdss/bindings.yaml.
export const ORDER = {
  age_band: BANDS, sex: ["Male", "Female"], event: EVN,
  education: ["None", "Primary", "Secondary", "Tertiary"], occupation: ["Student", "None", "Trader", "Farmer", "Employed", "Homemaker", "Other"],
  marital: ["Single", "Divorced", "Married", "Widowed"], migration: ["None", "In-migrant", "Visitor", "Out-migrant"], bednet: ["Yes", "No"],
  fuel: ["Firewood", "Charcoal", "Gas", "Electricity"], water: ["Tap", "Borehole", "Well", "Other"], roof: ["Iron", "Thatched", "Tiled"],
  floor: ["Cement", "Earth", "Tiled"], toilet: ["Pit latrine", "VIP latrine", "Flush toilet", "None"], asset: ["Radio", "TV", "Phone", "Fridge", "Vehicles"],
  update_cycle: ["UC09 (Oct 2025 – Mar 2026)", "UC10 (Apr – Sep 2026)", "UC11 (Oct 2026 – Mar 2027)"],
};
export const CYCLE_DATES = { "UC09 (Oct 2025 – Mar 2026)": ["2025-10-01", "2026-03-31"], "UC10 (Apr – Sep 2026)": ["2026-04-01", "2026-09-30"], "UC11 (Oct 2026 – Mar 2027)": ["2026-10-01", "2027-03-31"] };
const LEVELS = ["district", "county", "subcounty", "parish", "village"];
const VKEY = { district: "di", county: "cn", subcounty: "sc", parish: "pa", village: "vi" };
const REF = Date.UTC(2026, 9, 20); // reference date of the demo dataset (bindings.yaml)
const DAY0 = Date.UTC(2025, 10, 1);
const iso = (ms) => new Date(ms).toISOString().slice(0, 10);

export const ROWS = R.per.map((p) => {
  const h = R.hh[p[0]], v = R.villages[h[0]];
  return {
    hh: p[0], v: h[0], path: Object.fromEntries(LEVELS.map((l) => [l, v[VKEY[l]]])), lat: v.lat, lon: v.lon,
    age_band: BANDS[p[1]], sex: p[2] === 0 ? "Male" : "Female", education: NAMES.education[p[3]], occupation: NAMES.occupation[p[4]],
    marital: NAMES.marital[p[5]], migration: NAMES.migration[p[6]], event: p[7] >= 0 ? EVN[p[7]] : null, bednet: NAMES.bednet[p[8]],
    date: iso(DAY0 + p[9] * 864e5), month: p[10],
    fuel: NAMES.fuel[h[1]], water: NAMES.water[h[2]], roof: h[3] >= 0 ? NAMES.roof[h[3]] : null, floor: NAMES.floor[h[4]], toilet: NAMES.toilet[h[5]],
    assets: new Set(NAMES.asset.filter((_, k) => (h[6] >> k) & 1)),
    update_cycle: Object.keys(CYCLE_DATES).find((c) => { const d = iso(DAY0 + p[9] * 864e5); return d >= CYCLE_DATES[c][0] && d <= CYCLE_DATES[c][1]; }),
  };
});

// Time range options (calendar periods relative to the reference date; weeks start Monday).
const D = (y, m, d) => Date.UTC(y, m, d);
export function rangeBounds(name) {
  const r = new Date(REF), y = r.getUTCFullYear(), m = r.getUTCMonth(), wd = (r.getUTCDay() + 6) % 7, monday = REF - wd * 864e5;
  const q = Math.floor(m / 3) * 3;
  const b = { "This week": [monday, monday + 6 * 864e5], "Last week": [monday - 7 * 864e5, monday - 864e5],
    "This month": [D(y, m, 1), D(y, m + 1, 0)], "Last month": [D(y, m - 1, 1), D(y, m, 0)],
    "This quarter": [D(y, q, 1), D(y, q + 3, 0)], "Last quarter": [D(y, q - 3, 1), D(y, q, 0)],
    "This year": [D(y, 0, 1), D(y, 11, 31)], "Previous years": [null, D(y - 1, 11, 31)] }[name] || [null, null];
  return b.map((v) => (v === null ? null : iso(v)));
}
export function bounds(st) {
  const lows = [], highs = [];
  if (st.from) lows.push(st.from); if (st.to) highs.push(st.to);
  const days = st.period === "Last 6 months" ? 182 : st.period === "Last 3 months" ? 91 : null;
  if (days !== null) lows.push(iso(REF - days * 864e5));
  const [rl, rh] = rangeBounds(st.range); if (rl) lows.push(rl); if (rh) highs.push(rh);
  return [lows.length ? lows.sort().at(-1) : null, highs.length ? highs.sort()[0] : null];
}
// Trend window and buckets: the displayed time window (else first record .. reference date).
export function sparkBuckets(st) {
  const [lo, hi] = bounds(st);
  const all = ROWS.map((r) => r.date).sort(), dmin = all[0], dmax = all.at(-1), ref = iso(REF);
  const first = lo || dmin;
  let last = hi ? (hi < ref ? hi : ref) : ref; if (!hi && dmax > last) last = dmax; if (last < first) last = first;
  const days = Math.round((Date.parse(last) - Date.parse(first)) / 864e5) + 1;
  const unit = days <= 31 ? "day" : days <= 124 ? "week" : days <= 1100 ? "month" : "year";
  const trunc = (s) => { const t = new Date(s + "T00:00:00Z");
    if (unit === "week") return iso(t.getTime() - ((t.getUTCDay() + 6) % 7) * 864e5);
    if (unit === "month") return iso(D(t.getUTCFullYear(), t.getUTCMonth(), 1));
    if (unit === "year") return iso(D(t.getUTCFullYear(), 0, 1)); return s; };
  const next = (s) => { const t = new Date(s + "T00:00:00Z");
    if (unit === "day") return iso(t.getTime() + 864e5); if (unit === "week") return iso(t.getTime() + 7 * 864e5);
    if (unit === "month") return iso(D(t.getUTCFullYear(), t.getUTCMonth() + 1, 1)); return iso(D(t.getUTCFullYear() + 1, 0, 1)); };
  const buckets = []; for (let d = trunc(first); d <= last; d = next(d)) buckets.push(d);
  return { unit, buckets, trunc };
}
function pass(r, st, skip = [], useTime = true) {
  for (const [l, v] of Object.entries(st.geo || {})) if (r.path[l] !== v) return false;
  if (useTime) { const [lo, hi] = bounds(st); if ((lo && r.date < lo) || (hi && r.date > hi)) return false; }
  for (const [d, label] of Object.entries(st.dims || {})) {
    if (!label || skip.includes(d)) continue;
    if (d === "asset" ? !r.assets.has(label) : r[d] !== label) return false;
  }
  return true;
}
const countBy = (rows, d) => ORDER[d].map((label) => rows.filter((r) => (d === "asset" ? r.assets.has(label) : r[d] === label)).length);

export function aggregates(st) {
  st = { geo: {}, period: "All", range: "All", from: "", to: "", dims: {}, ...st };
  const all = ROWS.filter((r) => pass(r, st));
  const card = (d, skip = [d]) => ({ labels: ORDER[d], counts: countBy(ROWS.filter((r) => pass(r, st, skip)), d) });
  const cards = {};
  for (const d of ["event", "fuel", "water", "education", "occupation", "marital", "migration", "bednet", "roof", "floor", "toilet", "asset"]) cards[d] = card(d);
  cards.age = { labels: BANDS, counts: countBy(ROWS.filter((r) => pass(r, st, ["age_band"])), "age_band") };
  const ag = ROWS.filter((r) => pass(r, st, ["age_band", "sex"]));
  cards.age_sex = { bands: BANDS, sexes: ["Male", "Female"], counts: Object.fromEntries(["Male", "Female"].map((s) => [s, BANDS.map((b) => ag.filter((r) => r.sex === s && r.age_band === b).length)])) };
  const villages = [...new Set(all.map((r) => r.v))].sort((a, b) => a - b);
  const map = villages.map((i) => { const rows = all.filter((r) => r.v === i); return { path: rows[0].path, n: rows.length, lat: rows[0].lat, lon: rows[0].lon }; });
  const sb = sparkBuckets(st);
  const series = (rows) => { const v = new Array(sb.buckets.length).fill(0); rows.forEach((r) => { const i = sb.buckets.indexOf(sb.trunc(r.date)); if (i >= 0) v[i]++; }); return v; };
  const evRows = ROWS.filter((r) => pass(r, st, ["event"]));
  return {
    overall: { persons: ROWS.length, households: new Set(ROWS.map((r) => r.hh)).size },
    totals: { persons: all.length, households: new Set(all.map((r) => r.hh)).size, villages: villages.length },
    cards,
    spark: {
      unit: sb.unit, buckets: sb.buckets,
      event: Object.fromEntries(EVN.map((e) => [e, series(evRows.filter((r) => r.event === e))])),
      under_five: series(ROWS.filter((r) => pass(r, st, ["age_band"]) && r.age_band === "0-4")),
    },
    map,
    geography: { levels: LEVELS, paths: R.villages.map((v) => Object.fromEntries(LEVELS.map((l) => [l, v[VKEY[l]]]))) },
    suppression: { min_cell_size: 0, applied: false },
  };
}

// The filter states every check runs on: each dimension alone, geography, time, and combinations
// (the same list as the design's acceptance tests, section 3, in label form).
export function oracleStates() {
  const s = (dims = {}, extra = {}) => ({ geo: {}, period: "All", range: "All", from: "", to: "", dims, ...extra });
  const out = [["no filter", s()]];
  EVN.forEach((e) => out.push(["event " + e, s({ event: e })]));
  BANDS.forEach((b) => out.push(["age band " + b, s({ age_band: b })]));
  out.push(["men only", s({ sex: "Male" })], ["women 25-44", s({ age_band: "25-44", sex: "Female" })]);
  for (const d of ["fuel", "water", "education", "marital", "migration", "toilet", "occupation", "roof", "floor", "bednet", "asset"])
    ORDER[d].forEach((label) => out.push([d + " " + label, s({ [d]: label })]));
  out.push(["district Iganga", s({}, { geo: { district: "Iganga" } })], ["district Mayuge", s({}, { geo: { district: "Mayuge" } })]);
  out.push(["village Kisoko", s({}, { geo: { district: "Iganga", county: "Kigulu South", subcounty: "Namungalwe", parish: "Bukanga", village: "Kisoko" } })]);
  out.push(["Last 6 months", s({}, { period: "Last 6 months" })], ["Last 3 months", s({}, { period: "Last 3 months" })]);
  out.push(["date range", s({}, { from: "2026-01-01", to: "2026-03-31" })]);
  for (const r of ["This week", "Last week", "This month", "Last month", "This quarter", "Last quarter", "This year", "Previous years"])
    out.push(["time range " + r, s({}, { range: r })]);
  for (const c of ORDER.update_cycle) out.push(["update cycle " + c.slice(0, 4), s({ update_cycle: c })]);
  out.push(["update cycle + time period + fuel: UC10 + This year + Firewood", s({ update_cycle: ORDER.update_cycle[1], fuel: "Firewood" }, { range: "This year" })]);
  out.push(["time range + period: This year + Last 3 months", s({ event: "Deaths" }, { range: "This year", period: "Last 3 months" })]);
  out.push(["time range + filters: Last quarter + Firewood + Iganga", s({ fuel: "Firewood" }, { range: "Last quarter", geo: { district: "Iganga" } })]);
  out.push(["combined: fuel + education + district", s({ fuel: "Firewood", education: "Secondary" }, { geo: { district: "Iganga" } })]);
  out.push(["combined: roof + bednet + age", s({ roof: "Iron", bednet: "Yes", age_band: "5-14" })]);
  out.push(["combined: asset + marital + period", s({ asset: "Phone", marital: "Single" }, { period: "Last 6 months" })]);
  out.push(["combined: event + occupation + sex", s({ event: "In-migration", occupation: "Employed", sex: "Female" })]);
  out.push(["empty: Births + 65+", s({ event: "Births", age_band: "65+" })]);
  return out;
}
