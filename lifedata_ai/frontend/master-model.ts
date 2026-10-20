// Master dashboard view model: the approved design's renderVals (docs/design/master/design_master_dashboard.dc.html),
// ported to read SERVER AGGREGATES (POST /lifedata-ai/dashboards/<id>/query) instead of person rows. It returns
// the design's own field names, so master-view.tsx (generated from the design) renders it unchanged.
// Pure functions: frontend/tests/master_acceptance.mjs drives it like the design's own acceptance tests.

export const LEVEL_LABEL: Record<string, string> = { country: "Country", site: "Site", district: "District", county: "County", subcounty: "Subcounty", parish: "Parish", village: "Village" };
const LEVEL_WIDTH: Record<string, string> = { country: "150px", site: "230px", district: "190px", county: "190px", subcounty: "190px", parish: "150px", village: "190px" };
export const RANGES = ["All", "This week", "Last week", "This month", "Last month", "This quarter", "Last quarter", "This year", "Previous years"];
const EVC: Record<string, string> = { Births: "#4A6B77", Deaths: "#6F4A3C", "In-migration": "#A07860", "Out-migration": "#C4A47A", Pregnancies: "#85A88B" };
const SYNC: Record<string, string> = { Fever: "#A9503F", Respiratory: "#A07860", Diarrhea: "#4A6B77", Rash: "#85A88B" };
const PATHC: Record<string, string> = { Malaria: "#4A6B77", Respiratory: "#A07860", Diarrheal: "#C4A47A", Measles: "#A9503F" };
const FALLBACK = ["#4A6B77", "#A07860", "#C4A47A", "#A9503F", "#85A88B", "#6F4A3C"];
const TIERC: Record<string, string> = { High: "#A9503F", Moderate: "#C4A47A", Low: "#85A88B" };
const TIERA: Record<string, string> = { High: "rgba(169,80,63,0.5)", Moderate: "rgba(196,164,122,0.55)", Low: "rgba(133,168,139,0.55)" };
const PILL: Record<string, [string, string]> = { High: ["#F3DDD8", "#8E3B2B"], Moderate: ["#F1E4CE", "#7A5A1E"], Low: ["#E1EBE2", "#3F6B4B"] };
const STAT: Record<string, [string, string]> = { alert: ["#A9503F", "rgba(169,80,63,0.45)"], watch: ["#C4A47A", "rgba(196,164,122,0.5)"], normal: ["#85A88B", "rgba(133,168,139,0.5)"] };
const PERSON_FILTERS = ["housing", "water", "bednet", "age_band", "sex"];
const CHIP_NAMES: [string, string][] = [["tier", "Tier"], ["event_type", "Event"], ["age_band", "Age"], ["sex", "Sex"], ["housing", "Housing"], ["water", "Water"],
  ["bednet", "Bednet"], ["syndrome", "Syndrome"], ["pathogen", "Pathogen"], ["site_status", "Site status"], ["signal_severity", "Severity"]];

export type State = { tab: string; geo: Record<string, string>; cycle: string; range: string; tp: string; dims: Record<string, string> };
export const initialState = (spec: Spec): State => {
  const visible = spec.tabs.filter((t) => t.visible).map((t) => t.id);
  return { tab: visible.includes("overview") ? "overview" : visible[0] || "overview", geo: {}, cycle: "All", range: "All", tp: "", dims: {} }; // opens on Overview (user request)
};
export type Spec = { title: string; status: string; tabs: { id: string; label: string; slots: string[]; visible: boolean }[]; geography: string[]; drill?: string[]; filters: string[];
  options: Record<string, string[]>; tiers: string[]; privacy: { synthetic: boolean; min_cell_size: number }; coverage: { slots: Record<string, Record<string, any>> } };
export type Agg = any; // the server's aggregates (see lifedata_ai/hdss/master.py compute())

const sum = (a: number[]) => a.reduce((x, y) => x + y, 0);
const fmt = (n: number) => Math.round(n).toLocaleString("en-US");
const dimOp = (active: boolean, selected: boolean) => (active && !selected ? 0.3 : 1);
const colour = (map: Record<string, string>, name: string, i: number) => map[name] || FALLBACK[i % FALLBACK.length];

// ------------------------------------------------------------------ state transitions (the design's handlers)
export function setLevel(S: State, levels: string[], level: string, value: string): State {
  const geo = { ...S.geo };
  if (value === "All") delete geo[level]; else geo[level] = value;
  levels.slice(levels.indexOf(level) + 1).forEach((l) => delete geo[l]);
  return { ...S, geo };
}
export const pickPath = (S: State, path: Record<string, string>): State => ({ ...S, geo: Object.fromEntries(Object.entries(path).filter(([, v]) => v != null)) });
export function toggle(S: State, dim: string, value: string): State {
  const dims = { ...S.dims };
  if (dims[dim] === value) delete dims[dim]; else dims[dim] = value;
  return { ...S, dims };
}
const setDim = (dims: Record<string, string>, k: string, v: string) => { if (v) dims[k] = v; else delete dims[k]; };
/** design pair(): set both, or clear both when both are already set to these values. Time point uses key "tp". */
export function pair(S: State, k1: string, v1: string, k2: string, v2: string): State {
  const get = (k: string) => (k === "tp" ? S.tp : S.dims[k] || "");
  const dims = { ...S.dims }; let tp = S.tp;
  const both = get(k1) === v1 && get(k2) === v2;
  for (const [k, v] of [[k1, both ? "" : v1], [k2, both ? "" : v2]]) { if (k === "tp") tp = v; else setDim(dims, k, v); }
  return { ...S, dims, tp };
}
export const pyPick = (S: State, band: string, side: string) => pair(S, "age_band", band, "sex", side);
export const toggleTp = (S: State, key: string): State => ({ ...S, tp: S.tp === key ? "" : key });
export const clearAll = (S: State): State => ({ ...S, geo: {}, cycle: "All", range: "All", tp: "", dims: {} });
export const serverState = (S: State) => ({ geo: { ...S.geo }, cycle: S.cycle, period: S.range, time_point: S.tp, dims: { ...S.dims } });

// ------------------------------------------------------------------ geometry (the design's helpers, unchanged)
function smooth(pts: number[][]) {
  if (!pts.length) return "";
  if (pts.length === 1) return "M" + pts[0][0].toFixed(1) + "," + pts[0][1].toFixed(1);
  let d = "M" + pts[0][0].toFixed(1) + "," + pts[0][1].toFixed(1);
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || p2;
    const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6], c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
    d += " C" + c1[0].toFixed(1) + "," + c1[1].toFixed(1) + " " + c2[0].toFixed(1) + "," + c2[1].toFixed(1) + " " + p2[0].toFixed(1) + "," + p2[1].toFixed(1);
  }
  return d;
}
function donutSegs(parts: any[], R: number, gap: number) {
  const circ = 2 * Math.PI * R, tot = sum(parts.map((p) => p.n)); let start = 0; const out: any[] = [];
  if (!tot) return out;
  parts.forEach((p) => { if (p.n <= 0) return; const len = p.n / tot * circ;
    out.push({ ...p, dash: Math.max(len - gap, 0.5).toFixed(1) + " " + circ.toFixed(1), off: (-start).toFixed(1) }); start += len; });
  return out;
}
function lineSet(series: any[], keys: string[], labels: Record<string, string>, X0: number, XSPAN: number, Y0: number, YSPAN: number, VBW: number, sel: string,
  onDot: (si: number, k: string) => () => void, onLabel: (k: string) => () => void) {
  const vmax = Math.max(1, ...series.map((s) => Math.max(0, ...s.vals)));
  const n = keys.length, dx = n > 1 ? XSPAN / (n - 1) : 0, x0 = n > 1 ? X0 : X0 + XSPAN / 2;
  const lines: any[] = [], dots: any[] = [];
  series.forEach((s, si) => {
    const pts = s.vals.map((v: number, i: number) => [x0 + i * dx, Y0 - v / vmax * YSPAN]);
    lines.push({ d: smooth(pts), c: s.c, op: s.op });
    // the design's sizes for its 7 months; long real timelines (e.g. 100 years) get smaller points
    const big = n > 24 ? 4.5 : 6, small = n > 24 ? 2.5 : 4;
    pts.forEach((p: number[], i: number) => dots.push({ x: p[0].toFixed(1), y: p[1].toFixed(1), c: s.c, op: s.op, r: keys[i] === sel ? big : small, pick: onDot(si, keys[i]),
      tip: s.n + ", " + labels[keys[i]] + ": " + (s.tips ? s.tips[i] : fmt(s.vals[i])) }));
  });
  // long timelines get at most 7 axis labels (every point stays hoverable and clickable); the selected point is
  // always labelled. Up to 12 points (the design's months and weeks) every point is labelled.
  const step = n > 12 ? Math.ceil(n / 7) : 1;
  const labs = keys.map((k, i) => ({ l: ((x0 + i * dx) / VBW * 100).toFixed(2) + "%", t: labels[k], pr: k === sel, w: k === sel ? 700 : 500, c: k === sel ? "#1A1A1A" : "#66615A", pick: onLabel(k) }))
    .filter((_, i) => i % step === 0 || keys[i] === sel);
  return { lines, dots, labs };
}

// ------------------------------------------------------------------ the view model
export function renderVals(S: State, A: Agg, spec: Spec, set: (s: State) => void) {
  const out: any = {}, k = spec.privacy.min_cell_size, D = S.dims, levels = spec.geography;
  const allLevels = levels.concat(spec.drill || []); // drill levels (village) are set by clicking a chart, not by a dropdown
  const cell = (n: number | null | undefined) => (n == null ? "<" + k : fmt(n));
  const go = (f: (s: State) => State) => () => set(f(S));
  const on = (d: string) => !!D[d];
  const bound = (slot: string) => Object.values(spec.coverage.slots).some((t) => t[slot] === "bound");
  out.slot = bound;
  out.slotReason = (slot: string) => { const why = Object.values(spec.coverage.slots).map((t) => t[slot]).find((v) => v && v !== "bound");
    return why ? (why.status === "needs_definition" ? "Needs definition" : "No data source registered") + (why.reason ? ": " + why.reason : "") : ""; };
  const pointLabel: Record<string, string> = {}, weekLabel: Record<string, string> = {};
  (A.points || []).forEach((p: any) => { pointLabel[p.key] = p.label; if (p.week) weekLabel[p.key] = p.week; });

  // ---- shell: tabs, filter bar, chips
  out.tabs = spec.tabs.filter((t) => t.visible).map((t) => ({ label: t.label, pick: () => set({ ...S, tab: t.id }), line: S.tab === t.id ? "#7FA98B" : "transparent",
    weight: S.tab === t.id ? 600 : 500, color: S.tab === t.id ? "#FFFFFF" : "#B9B9B9", pressed: S.tab === t.id }));
  const paths: Record<string, string>[] = A.paths || [];
  const geoMatch = (p: Record<string, string>, upto: number) => levels.slice(0, upto).every((l) => !S.geo[l] || p[l] === S.geo[l]);
  out.geoSelects = levels.map((l, i) => ({ label: LEVEL_LABEL[l] || l, width: LEVEL_WIDTH[l] || "190px", value: S.geo[l] || "All",
    options: ["All"].concat(Array.from(new Set(paths.filter((p) => geoMatch(p, i)).map((p) => p[l]).filter((v) => v != null))).sort()).map((v) => ({ v })),
    onChange: (e: { target: { value: string } }) => set(setLevel(S, allLevels, l, e.target.value)) }));
  out.showCycle = spec.filters.includes("cycle"); out.fCycle = S.cycle; out.optCycle = ["All"].concat(A.cycles || []).map((v) => ({ v }));
  out.onCycle = (e: { target: { value: string } }) => set({ ...S, cycle: e.target.value, tp: "" }); // changing the timeline clears a selected time point
  out.showPeriod = spec.filters.includes("period"); out.fRange = S.range; out.optRange = RANGES.map((v) => ({ v }));
  out.onRange = (e: { target: { value: string } }) => set({ ...S, range: e.target.value, tp: "" });
  out.clearAll = () => set(clearAll(S));
  const chips: any[] = [];
  const chip = (cond: boolean, label: string, f: (s: State) => State) => { if (cond) chips.push({ label, clear: go(f) }); };
  allLevels.forEach((l) => chip(!!S.geo[l], (LEVEL_LABEL[l] || l) + ": " + S.geo[l], (s) => setLevel(s, allLevels, l, "All")));
  chip(on("tier"), "Tier: " + D.tier, (s) => toggle(s, "tier", D.tier));
  chip(S.cycle !== "All", "Update cycle: " + S.cycle, (s) => ({ ...s, cycle: "All", tp: "" }));
  chip(S.range !== "All", "Time period: " + S.range, (s) => ({ ...s, range: "All", tp: "" }));
  chip(!!S.tp, "Time: " + (pointLabel[S.tp] || S.tp) + (weekLabel[S.tp] ? " / " + weekLabel[S.tp] : ""), (s) => ({ ...s, tp: "" }));
  CHIP_NAMES.filter(([d]) => d !== "tier").forEach(([d, name]) => chip(on(d), name + ": " + D[d], (s) => toggle(s, d, D[d])));
  out.chips = chips; out.hasChips = chips.length > 0;
  out.isEmpty = !!A.empty;
  out.showOverview = S.tab === "overview" && !out.isEmpty; out.showSentinel = S.tab === "sentinel" && !out.isEmpty; out.showGeo = S.tab === "geo" && !out.isEmpty;
  out.illustrative = spec.privacy.synthetic;
  out.scopeHelp = PERSON_FILTERS.some(on);
  out.scopeHelpText = "Events, sentinel sites and clusters are village-level: with a person or household filter they follow the villages where that selection exists.";
  const tile = (key: string, active: boolean, fn: () => void) => { out["pick" + key] = fn; out["pr" + key] = active; out["bd" + key] = active ? "#1A1A1A" : "#E6E1D7"; };
  tile("Births", D.event_type === "Births", go((s) => toggle(s, "event_type", "Births")));
  tile("Deaths", D.event_type === "Deaths", go((s) => toggle(s, "event_type", "Deaths")));
  tile("Preg", D.event_type === "Pregnancies", go((s) => toggle(s, "event_type", "Pregnancies")));
  tile("U5", D.age_band === "0-4" && !D.sex, go((s) => pyPick(s, "0-4", "")));
  tile("High", D.tier === (spec.tiers[0] || "High"), go((s) => toggle(s, "tier", spec.tiers[0] || "High")));

  // ================================ overview
  const ov = A.overview || {};
  out.kRes = cell(ov.residents); out.kHh = cell(ov.households); out.kU5 = cell(ov.under_five);
  out.kBirths = cell(ov.births); out.kDeaths = cell(ov.deaths); out.kNet = ov.net_migration == null ? "<" + k : String(ov.net_migration);
  out.sEver = cell(ov.ever_registered); out.sPreg = cell(ov.currently_pregnant); out.sU5 = cell(ov.under_five_ever); out.sOpd = cell(ov.outpatient_visits);
  out.vTitle = A.unit === "year" ? "Vital events by year" : "Vital events by month";
  const types: string[] = spec.options.event_type || [];
  if (ov.vital) {
    const keys: string[] = ov.vital.points;
    const vs = types.map((t, i) => ({ n: t, c: colour(EVC, t, i), op: dimOp(on("event_type"), D.event_type === t), vals: (ov.vital.series[t] || []).map((v: number | null) => v || 0),
      tips: (ov.vital.series[t] || []).map((v: number | null) => cell(v)) })); // a suppressed month shows "<k", not 0
    const vl = lineSet(vs, keys, pointLabel, 22, 276, 112, 98, 320, S.tp, (si, key) => go((s) => pair(s, "event_type", types[si], "tp", key)), (key) => go((s) => toggleTp(s, key)));
    out.vLines = vl.lines; out.vDots = vl.dots; out.vLabels = vl.labs;
    out.vLegend = types.map((t, i) => ({ c: colour(EVC, t, i), t, op: dimOp(on("event_type"), D.event_type === t), pr: D.event_type === t, pick: go((s) => toggle(s, "event_type", t)) }));
  } else { out.vLines = []; out.vDots = []; out.vLabels = []; out.vLegend = []; }
  const mixN = (t: string) => (ov.mix || {})[t];
  const mixPart = (t: string, i: number) => ({ c: colour(EVC, t, i), n: mixN(t) || 0, op: dimOp(on("event_type"), D.event_type === t), pick: go((s) => toggle(s, "event_type", t)) });
  const byOrder = (order: string[]) => order.filter((t) => types.includes(t)).concat(types.filter((t) => !order.includes(t)));
  out.mixSegs = donutSegs(byOrder(["Births", "Pregnancies", "Deaths", "Out-migration", "In-migration"]).map((t) => mixPart(t, types.indexOf(t))), 78, 2.5);
  out.mixLegend = byOrder(["Births", "In-migration", "Out-migration", "Deaths", "Pregnancies"]).map((t) => ({ c: colour(EVC, t, types.indexOf(t)), t: t + " (" + cell(mixN(t)) + ")",
    op: dimOp(on("event_type"), D.event_type === t), pr: D.event_type === t, pick: go((s) => toggle(s, "event_type", t)) }));
  if (ov.pyramid) {
    const [male, female] = ov.pyramid.sexes, pm: (number | null)[] = ov.pyramid.counts[male], pf: (number | null)[] = ov.pyramid.counts[female];
    const pmax = Math.max(1, ...pm.map((v) => v || 0), ...pf.map((v) => v || 0));
    out.pyRows = ov.pyramid.bands.map((a: string, i: number) => {
      const rowOn = on("age_band") && D.age_band !== a;
      const w = (v: number | null) => (v ? Math.max(5, Math.round(v / pmax * 96)) : 0);
      return { a, m: cell(pm[i]), f: cell(pf[i]), mw: w(pm[i]), fw: w(pf[i]),
        mo: rowOn || (on("sex") && D.sex === female) ? 0.3 : 1, fo: rowOn || (on("sex") && D.sex === male) ? 0.3 : 1, ao: rowOn ? 0.3 : 1,
        mpr: D.age_band === a && D.sex === male, apr: D.age_band === a && !D.sex, fpr: D.age_band === a && D.sex === female,
        mlabel: "Men aged " + a + ": " + cell(pm[i]), alabel: "Age " + a + ": " + (pm[i] == null || pf[i] == null ? "<" + k : fmt((pm[i] || 0) + (pf[i] || 0))),
        flabel: "Women aged " + a + ": " + cell(pf[i]), pm: go((s) => pyPick(s, a, male)), pa: go((s) => pyPick(s, a, "")), pf: go((s) => pyPick(s, a, female)) };
    });
  } else out.pyRows = [];
  const bars = (dim: string) => {
    const counts: Record<string, number | null> = ov[dim] || {}, names = spec.options[dim] || [];
    const mx = Math.max(1, ...names.map((l) => counts[l] || 0));
    return names.map((l) => { const n = counts[l]; return { l, n: cell(n), w: n ? Math.max(3, Math.round(n / mx * 80)) : 0, op: dimOp(on(dim), D[dim] === l), pr: D[dim] === l,
      label: l + ": " + cell(n) + " households", pick: go((s) => toggle(s, dim, l)) }; });
  };
  out.housRows = bars("housing"); out.watRows = bars("water"); out.netRows = bars("bednet");
  const selVillage = S.geo.village;
  out.ovPins = (ov.pins || []).map((p: any) => ({ lat: p.lat, lon: p.lon, s: 18, bg: "#9C4F3C", bw: selVillage === p.village ? 4 : 2, op: 1, bd: selVillage === p.village ? "#1A1A1A" : "#C98C78",
    label: p.village + ", " + p.sub + " (" + cell(p.residents) + " residents)", pick: go((s) => pickPath(s, p.path)) }));

  // ================================ sentinel
  const sen = A.sentinel || {};
  out.sKSites = String(sen.sites ?? "–"); out.sKReports = sen.reports == null ? "–" : fmt(sen.reports); out.sKSignals = String(sen.signals ?? "–");
  out.sKRrt = String(sen.rrt ?? "–"); out.sKLab = String(sen.lab ?? "–"); out.sKLag = sen.lag == null ? "–" : Number(sen.lag).toFixed(1) + "d";
  const syns: string[] = spec.options.syndrome || [];
  if (sen.weekly) {
    const keys: string[] = sen.weekly.points;
    const labels: Record<string, string> = Object.fromEntries(keys.map((key) => [key, weekLabel[key] || pointLabel[key]]));
    const ws = syns.map((n, i) => ({ n, c: colour(SYNC, n, i), op: dimOp(on("syndrome"), D.syndrome === n), vals: sen.weekly.series[n] || [] }));
    const wl = lineSet(ws, keys, labels, 26, 306, 168, 140, 360, S.tp, (si, key) => go((s) => pair(s, "syndrome", syns[si], "tp", key)), (key) => go((s) => toggleTp(s, key)));
    out.wLines = wl.lines; out.wDots = wl.dots; out.wLabels = wl.labs;
  } else { out.wLines = []; out.wDots = []; out.wLabels = []; }
  out.wLegend = ["Fever", "Diarrhea", "Respiratory", "Rash"].filter((n) => syns.includes(n)).concat(syns.filter((n) => !["Fever", "Diarrhea", "Respiratory", "Rash"].includes(n)))
    .map((n) => ({ c: colour(SYNC, n, syns.indexOf(n)), t: n, op: dimOp(on("syndrome"), D.syndrome === n), pr: D.syndrome === n, pick: go((s) => toggle(s, "syndrome", n)) }));
  out.sPins = (sen.site_pins || []).map((p: any) => { const st = STAT[p.status] || STAT.normal;
    return { lat: p.lat, lon: p.lon, s: 22, bg: st[1], bw: selVillage === p.village ? 5 : 3, op: dimOp(on("site_status"), D.site_status === p.status),
      bd: selVillage === p.village ? "#1A1A1A" : st[0], label: p.name + " (" + p.status + ")", pick: go((s) => pickPath(s, p.path)) }; });
  out.statusItems = (spec.options.site_status || []).map((st) => ({ c: (STAT[st] || STAT.normal)[0], label: st + " (" + ((sen.status_counts || {})[st] ?? 0) + ")", pressed: D.site_status === st,
    bg: D.site_status === st ? "#EFE8DC" : "transparent", bd: D.site_status === st ? "#8A857E" : "transparent", pick: go((s) => toggle(s, "site_status", st)) }));
  const paths_: string[] = spec.options.pathogen || [], lab = sen.pathogen || {};
  const pp = (p: string) => ({ c: colour(PATHC, p, paths_.indexOf(p)), n: lab[p] || 0, op: dimOp(on("pathogen"), D.pathogen === p), pick: go((s) => toggle(s, "pathogen", p)) });
  out.pathSegs = donutSegs(["Malaria", "Measles", "Diarrheal", "Respiratory"].filter((p) => paths_.includes(p)).concat(paths_.filter((p) => !["Malaria", "Measles", "Diarrheal", "Respiratory"].includes(p))).map(pp), 90, 3.5);
  out.pathLegend = paths_.map((p) => ({ c: colour(PATHC, p, paths_.indexOf(p)), t: p + " (" + (lab[p] || 0) + ")", op: dimOp(on("pathogen"), D.pathogen === p), pr: D.pathogen === p, pick: go((s) => toggle(s, "pathogen", p)) }));
  out.sigRows = (sen.signal_rows || []).map((g: any) => ({ fac: g.facility, syn: g.label, sev: g.severity, bg: (PILL[g.severity] || PILL.Low)[0], fg: (PILL[g.severity] || PILL.Low)[1], when: g.when,
    pr: D.signal_severity === g.severity, sbd: D.signal_severity === g.severity ? "#1A1A1A" : "transparent",
    synPick: () => { if (g.syndrome) set(toggle(S, "syndrome", g.syndrome)); }, rowbg: selVillage === g.path?.village ? "#EFE8DC" : "transparent",
    pick: go((s) => pickPath(s, g.path)), sevPick: go((s) => toggle(s, "signal_severity", g.severity)) }));
  out.noSignals = (sen.signal_rows || []).length === 0;

  // ================================ geospatial clusters
  const geo = A.geo || {}, tc = geo.tier_counts || {}, tiers = spec.tiers.length ? spec.tiers : ["High", "Moderate", "Low"];
  out.gKClusters = String(geo.clusters ?? "–"); out.gKHigh = String(geo.high_risk ?? "–"); out.gKPop = geo.pop_at_risk == null ? "–" : fmt(geo.pop_at_risk);
  out.gKMean = geo.mean_vulnerability == null ? "–" : geo.mean_vulnerability + "%"; out.gKRad = geo.radius == null ? "–" : Number(geo.radius).toFixed(1) + "km";
  out.gKP = geo.p_value == null ? "–" : Number(geo.p_value).toFixed(2);
  out.gPins = (geo.pins || []).map((p: any) => { const s = Math.round(12 + Math.min(26, Math.sqrt(p.cases) * 4.2));
    return { lat: p.lat, lon: p.lon, s, bg: TIERA[p.tier], bw: selVillage === p.village ? 4 : 2.5, op: 1, bd: selVillage === p.village ? "#1A1A1A" : TIERC[p.tier],
      label: p.village + ": " + p.cases + " cases, " + p.tier + " tier", pick: go((s2) => pickPath(s2, p.path)) }; });
  const tierBtn = (t: string, withCount: boolean) => ({ label: withCount ? t + " (" + (tc[t] ?? 0) + ")" : t, c: TIERC[t], pressed: D.tier === t, bg: D.tier === t ? "#EFE8DC" : "transparent",
    bd: D.tier === t ? "#8A857E" : "transparent", pick: go((s) => toggle(s, "tier", t)) });
  out.tierItems = tiers.map((t) => tierBtn(t, true)); out.tierPlain = tiers.map((t) => tierBtn(t, false));
  const tp = (t: string) => ({ c: TIERC[t], n: tc[t] || 0, op: dimOp(on("tier"), D.tier === t), pick: go((s) => toggle(s, "tier", t)) });
  out.tierSegs = donutSegs(["High", "Low", "Moderate"].filter((t) => tiers.includes(t)).map(tp), 90, 3);
  out.gPts = (geo.scatter || []).map((c: any) => ({ x: (c.cases / 40 * 100).toFixed(1) + "%", y: (100 - c.vulnerability).toFixed(1) + "%", s: 20, m: "-10px 0 0 -10px",
    bw: selVillage === c.village ? 4 : 0, bd: "#1A1A1A", label: c.village + ": " + c.cases + " cases, vulnerability " + c.vulnerability, pick: go((s) => pickPath(s, c.path)) }));
  out.gRows = (geo.hotspots || []).map((c: any) => ({ name: c.village, cases: c.cases, tier: c.tier, bg: (PILL[c.tier] || PILL.Low)[0], fg: (PILL[c.tier] || PILL.Low)[1], w: c.vulnerability,
    score: c.vulnerability, bar: TIERC[c.tier], rowbg: selVillage === c.village ? "#EFE8DC" : "transparent", tpr: D.tier === c.tier, tbd: D.tier === c.tier ? "#1A1A1A" : "transparent",
    pick: go((s) => pickPath(s, c.path)), tierPick: go((s) => toggle(s, "tier", c.tier)) }));
  out.noClusters = (geo.hotspots || []).length === 0;
  return out;
}
