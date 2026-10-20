// HDSS template view model: the approved design's filter model (docs/design/hdss/design_hdss_template.dc.html,
// script block), ported to work from SERVER AGGREGATES instead of person rows. Pure functions, no DOM, so the
// acceptance tests (frontend/tests/hdss_acceptance.mjs) drive it exactly like the design's own tests do.
//
// State values are category LABELS (not indexes), so URLs, chips and the server all speak the same names.

export const LEVELS = ["district", "county", "subcounty", "parish", "village"] as const;
export type Level = (typeof LEVELS)[number];
export const LABEL: Record<Level, string> = { district: "District", county: "County", subcounty: "Subcounty", parish: "Parish", village: "Village" };
export const EVENTS = ["Births", "Deaths", "In-migration", "Out-migration"];
export const DIMS = ["age_band", "sex", "event", "fuel", "water", "education", "occupation", "marital", "migration", "bednet", "roof", "floor", "toilet", "asset", "update_cycle"] as const;
export type Dim = (typeof DIMS)[number];
const CHIP_NAME: Record<string, string> = { event: "Event", age_band: "Age", sex: "Sex", fuel: "Cooking fuel", water: "Water source", education: "Education",
  occupation: "Occupation", roof: "Roof", floor: "Floor", bednet: "Bednet", toilet: "Toilet", migration: "Migration", marital: "Marital status", asset: "Asset", update_cycle: "Update cycle" };
// Query-string keys of the design's deep-dive links (kept exactly).
const URL_KEY: Record<string, string> = { event: "event", age_band: "age", sex: "sex", fuel: "fuel", water: "water", education: "edu", occupation: "occ",
  marital: "mar", migration: "mig", bednet: "bed", roof: "roof", floor: "floor", toilet: "toilet", asset: "ast", update_cycle: "cycle" };
const URL_ORDER: Dim[] = ["update_cycle", "event", "age_band", "sex", "fuel", "water", "education", "occupation", "marital", "migration", "bednet", "roof", "floor", "toilet", "asset"];
const CHIP_ORDER: Dim[] = ["update_cycle", "event", "age_band", "sex", "fuel", "water", "education", "occupation", "roof", "floor", "bednet", "toilet", "migration", "marital", "asset"];
export const MAROON = "#7B2A3D";
const C = { slate: "#6E7F96", blue: "#3C5FA8", green: "#5FA48A", tan: "#B99F4F", rose: "#C46A72", olive: "#76A12A", red: "#D9606A", teal: "#4FA08A", sand: "#C6A04A" };
// Donut colours by display position, per variable (the design's colour/order pairs, made positional).
const DONUT_COLORS: Record<string, string[]> = {
  roof: [C.blue, C.sand, C.teal, C.slate, C.rose], floor: [C.blue, C.sand, C.teal, C.slate, C.rose],
  bednet: [C.olive, C.red, C.slate], toilet: [C.blue, C.green, C.sand, C.red, C.slate, C.rose],
  migration: [C.slate, C.blue, C.green, C.tan, C.rose], water: [C.blue, C.green, C.tan, C.slate, C.rose, C.sand, C.teal],
  marital: [MAROON, C.tan, C.green, C.rose, C.slate, C.blue, C.sand],
};
const KC: [string, string][] = [["#A58B72", "g0"], ["#B35A5A", "g1"], ["#5D7191", "g2"], ["#7A6E66", "g3"], ["#4F7A62", "g4"]];
export const GAUGE_PATH = "M 34.82 127.36 A 80 80 0 1 1 185.18 127.36";

export type State = {
  district: string; county: string; subcounty: string; parish: string; village: string;
  period: string; range: string; from: string; to: string; fromD: string; toD: string;
  dims: Partial<Record<Dim, string>>;
  unit: "Persons" | "Households"; roofVar: "roof" | "floor"; netVar: "bednet" | "toilet"; migVar: "migration" | "water"; note: string;
};
export const DEFAULTS: State = { district: "All", county: "All", subcounty: "All", parish: "All", village: "All", period: "All", range: "All", from: "", to: "", fromD: "", toD: "",
  dims: {}, unit: "Persons", roofVar: "roof", netVar: "bednet", migVar: "migration", note: "" };

export type Count = number | null; // null = suppressed below min_cell_size
export type Aggregates = {
  overall: { persons: number; households: number };
  totals: { persons: Count; households: Count; villages: number | null };
  cards: Record<string, { labels: string[]; counts: Count[] }> & { age_sex?: { bands: string[]; sexes: string[]; counts: Record<string, Count[]> } };
  spark: { unit?: string; buckets?: string[]; event?: Record<string, Count[]>; under_five?: Count[] };
  map: { path: Partial<Record<Level, string>>; n: number; lat: number; lon: number }[];
  geography?: { levels: Level[]; paths: Partial<Record<Level, string>>[] };
  suppression: { min_cell_size: number; applied: boolean };
};
export type Spec = {
  title: string; slots: string[]; filters: string[]; deep_dive: Record<string, string | null>;
  privacy: { synthetic: boolean; min_cell_size: number }; population_reference?: number | null;
  labels: Record<string, string[]>; coverage: { slots: Record<string, any>; filters: Record<string, any> };
};

export const sum = (a: number[]) => a.reduce((x, y) => x + y, 0);
export const fmt = (n: number) => Math.round(n).toLocaleString("en-US");
const dimOp = (active: boolean, selected: boolean) => (active && !selected ? 0.35 : 1);
const showDate = (s: string) => s.split("-").reverse().join("/");
const val = (n: Count) => (n == null ? 0 : n);

// ---------------------------------------------------------------- state transitions (the design's handlers)
export function setLevel(S: State, level: Level, value: string): State {
  const p: State = { ...S }; p[level] = value;
  for (let k = LEVELS.indexOf(level) + 1; k < LEVELS.length; k++) p[LEVELS[k]] = "All";
  return p;
}
export function pickPath(S: State, path: Partial<Record<Level, string>>): State {
  const p = { ...S };
  LEVELS.forEach((l) => { p[l] = path[l] ?? "All"; });
  return p;
}
export function toggle(S: State, dim: Dim, label: string): State {
  const dims = { ...S.dims };
  if (dims[dim] === label) delete dims[dim]; else dims[dim] = label;
  return { ...S, dims };
}
/** Age pyramid: men pill = band + first sex, label = band, women pill = band + second sex. Same pick again clears both. */
export function pair(S: State, band: string, sex: string): State {
  const dims = { ...S.dims };
  if (dims.age_band === band && (dims.sex || "") === sex) { delete dims.age_band; delete dims.sex; }
  else { dims.age_band = band; if (sex) dims.sex = sex; else delete dims.sex; }
  return { ...S, dims };
}
export const clearAll = (): State => ({ ...DEFAULTS, dims: {} });
/** What the server sees: only committed filters (drafts, units and switches are presentation). */
export function serverState(S: State) {
  const geo: Record<string, string> = {};
  LEVELS.forEach((l) => { if (S[l] !== "All") geo[l] = S[l]; });
  return { geo, period: S.period, range: S.range, from: S.from, to: S.to, dims: { ...S.dims } };
}

export function filterQuery(S: State): string {
  const q: string[] = [], add = (k: string, v: string) => q.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
  LEVELS.forEach((l) => { if (S[l] !== "All") add(l, S[l]); });
  if (S.period !== "All") add("period", S.period);
  if (S.range !== "All") add("range", S.range);
  if (S.from) add("from", S.from); if (S.to) add("to", S.to);
  URL_ORDER.forEach((d) => { const v = S.dims[d]; if (v) add(URL_KEY[d], v); });
  return q.join("&");
}
export function deepDiveLink(base: string | null | undefined, S: State, label: string): string {
  if (!base) return "#";
  const qs = filterQuery(S);
  return base + (base.indexOf("?") >= 0 ? "&" : "?") + qs + (qs ? "&" : "") + "card=" + encodeURIComponent(label);
}

// ---------------------------------------------------------------- geometry helpers (the design's, unchanged)
function smooth(pts: number[][]) {
  let d = "M" + pts[0][0].toFixed(1) + "," + pts[0][1].toFixed(1);
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || p2;
    const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6], c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
    d += " C" + c1[0].toFixed(1) + "," + c1[1].toFixed(1) + " " + c2[0].toFixed(1) + "," + c2[1].toFixed(1) + " " + p2[0].toFixed(1) + "," + p2[1].toFixed(1);
  }
  return d;
}
/** Time range dropdown (calendar periods relative to the dataset's reference date). */
export const RANGES = ["All", "This week", "Last week", "This month", "Last month", "This quarter", "Last quarter", "This year", "Previous years"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** Readable label for a trend bucket (ISO start date + unit). */
export function bucketLabel(isoDay: string, unit?: string): string {
  const [y, m, d] = isoDay.split("-").map(Number);
  if (unit === "year") return String(y);
  if (unit === "month") return MON[m - 1] + " " + y;
  if (unit === "week") return "Week of " + d + " " + MON[m - 1] + " " + y;
  return d + " " + MON[m - 1] + " " + y;
}
export function spark(vals: number[]) {
  if (vals.length === 1) vals = [vals[0], vals[0]];
  const ymax = Math.max(1, ...vals) * 1.12, pts = vals.map((v, i) => [i * 300 / (vals.length - 1), 56 - v / ymax * 50]);
  const line = smooth(pts);
  return { line, area: line + " L300,60 L0,60 Z", pts };
}
type DonutPart = { name: string; n: Count; c: string; op: number; pr: boolean; pick: () => void };
function donut(parts: DonutPart[], cellText: (n: Count) => string) {
  const R = 27, SW = 13, CX = 80, CY = 75, tot = sum(parts.map((p) => val(p.n)));
  const segs: any[] = [], labels: any[] = [], leaders: any[] = [];
  let start = 0;
  parts.forEach((p) => {
    if (val(p.n) <= 0) return;
    const len = val(p.n) / tot * 100, mid = (start + len / 2) / 100 * 2 * Math.PI, sx = Math.sin(mid), sy = -Math.cos(mid);
    segs.push({ c: p.c, dash: Math.max(len - 0.8, 0.4).toFixed(2) + " " + (100 - Math.max(len - 0.8, 0.4)).toFixed(2), off: (-start).toFixed(2), op: p.op, pick: p.pick });
    const r1 = R + SW / 2 + 1, r2 = R + SW / 2 + 9, rl = R + SW / 2 + 12, dx = rl * sx, dy = rl * sy;
    leaders.push({ d: "M" + (CX + r1 * sx).toFixed(1) + "," + (CY + r1 * sy).toFixed(1) + " L" + (CX + r2 * sx).toFixed(1) + "," + (CY + r2 * sy).toFixed(1), c: p.c, op: p.op });
    labels.push({ name: p.name, n: cellText(p.n), c: p.c, op: p.op, dx: dx.toFixed(1), dy: dy.toFixed(1),
      tr: dx < -6 ? "translate(-100%, -50%)" : dx > 6 ? "translate(0, -50%)" : "translate(-50%, " + (dy < 0 ? "-100%" : "0") + ")",
      pr: p.pr, pick: p.pick, label: p.name + ": " + cellText(p.n) });
    start += len;
  });
  return { segs, labels, leaders, total: tot };
}

// ---------------------------------------------------------------- the view model (the design's renderVals)
export function renderVals(S: State, A: Aggregates, spec: Spec, set: (next: State) => void) {
  const out: any = {}, k = A.suppression.min_cell_size;
  const cellText = (n: Count) => (n == null ? "<" + k : fmt(n));
  const has = (slot: string) => spec.slots.includes(slot);
  const on = (d: Dim) => !!S.dims[d];
  const go = (f: (s: State) => State) => () => set(f(S));

  // ---- header, filter bar, chips
  out.hasNote = !!S.note;
  out.noteText = "Deep dive for " + S.note + " is not linked yet. Add that dashboard's address to this dashboard's deep-dive settings and this card will open it with the current filters.";
  out.closeNote = go((s) => ({ ...s, note: "" }));
  const N = A.overall.persons, persons = A.totals.persons;
  const paths = A.geography?.paths || [];
  const geoMatch = (p: Partial<Record<Level, string>>, upto: number) => LEVELS.slice(0, upto).every((l) => S[l] === "All" || p[l] === S[l]);
  LEVELS.forEach((l, i) => {
    const K = LABEL[l];
    out["f" + K] = S[l];
    out["opt" + K] = ["All"].concat(Array.from(new Set(paths.filter((p) => geoMatch(p, i)).map((p) => p[l]).filter((v): v is string => !!v))).sort()).map((v) => ({ v }));
    out["on" + K] = (e: { target: { value: string } }) => set(setLevel(S, l, e.target.value));
    out["show" + K] = spec.filters.includes(l);
  });
  out.showTime = spec.filters.includes("time_period"); out.showRange = spec.filters.includes("time_range");
  out.fPeriod = S.period; out.optPeriod = ["All", "Last 6 months", "Last 3 months"].map((v) => ({ v }));
  out.onPeriod = (e: { target: { value: string } }) => set({ ...S, period: e.target.value });
  // Update cycle: the dataset's HDSS rounds (a filter-only dimension; every card follows it)
  out.showCycle = spec.filters.includes("update_cycle") && !!(spec.labels || {}).update_cycle;
  out.fCycle = S.dims.update_cycle || "All";
  out.optCycle = ["All"].concat((spec.labels || {}).update_cycle || []).map((v) => ({ v }));
  out.onCycle = (e: { target: { value: string } }) => { const dims = { ...S.dims }; if (e.target.value === "All") delete dims.update_cycle; else dims.update_cycle = e.target.value; set({ ...S, dims }); };
  out.fRange = S.range; out.optRange = RANGES.map((v) => ({ v }));
  out.onRange = (e: { target: { value: string } }) => set({ ...S, range: e.target.value });
  out.fromD = S.fromD; out.toD = S.toD;
  out.onFromD = (e: { target: { value: string } }) => set({ ...S, fromD: e.target.value });
  out.onToD = (e: { target: { value: string } }) => set({ ...S, toD: e.target.value });
  out.applyNow = go((s) => ({ ...s, from: s.fromD, to: s.toD })); out.clearAll = () => set(clearAll());
  const chips: { label: string; clear: () => void }[] = [];
  LEVELS.forEach((l) => { if (S[l] !== "All") chips.push({ label: LABEL[l] + ": " + S[l], clear: go((s) => setLevel(s, l, "All")) }); });
  if (S.period !== "All") chips.push({ label: "Period: " + S.period, clear: go((s) => ({ ...s, period: "All" })) });
  if (S.range !== "All") chips.push({ label: "Time period: " + S.range, clear: go((s) => ({ ...s, range: "All" })) });
  if (S.from || S.to) chips.push({ label: "Dates: " + (S.from ? showDate(S.from) : "…") + " – " + (S.to ? showDate(S.to) : "…"), clear: go((s) => ({ ...s, from: "", to: "", fromD: "", toD: "" })) });
  CHIP_ORDER.forEach((d) => { const v = S.dims[d]; if (v) chips.push({ label: CHIP_NAME[d] + ": " + v, clear: go((s) => { const dims = { ...s.dims }; delete dims[d]; return { ...s, dims }; }) }); });
  out.chips = chips; out.hasChips = chips.length > 0; out.isEmpty = persons === 0; out.showBody = persons !== 0;

  // ---- KPI cards with sparklines (Births..Out-migration = event; Under five = age band 0-4)
  const ev = A.cards.event, age = A.cards.age, u5i = age ? age.labels.indexOf("0-4") : -1;
  const kpiSlots = ["kpi.births", "kpi.deaths", "kpi.in_migration", "kpi.out_migration", "kpi.under_five"];
  out.kpis = EVENTS.concat(["Under five"]).map((label, i) => {
    const isU5 = i === 4, bound = has(kpiSlots[i]);
    const value: Count = !bound ? null : isU5 ? (age ? age.counts[u5i] : null) : (ev ? ev.counts[ev.labels.indexOf(label)] : null);
    const series = (isU5 ? A.spark.under_five : A.spark.event?.[label]) || null;
    const sp = series && series.length ? spark(series.map(val)) : null;
    // hover readout: one point per time bucket (x in the 300-wide sparkline box)
    const points = sp && series ? series.map((v, j) => ({ x: series.length > 1 ? j * 300 / (series.length - 1) : 150, y: sp.pts[Math.min(j, sp.pts.length - 1)][1],
      label: bucketLabel((A.spark.buckets || [])[j] || "", A.spark.unit), value: cellText(v) })) : [];
    const active = isU5 ? S.dims.age_band === "0-4" && !S.dims.sex : S.dims.event === label;
    const base = spec.deep_dive[label];
    return { label, bound, reason: bound ? "" : slotReason(spec, kpiSlots[i]),
      link: deepDiveLink(base, S, label), dlabel: "Deep dive: " + label + (base ? "" : " (link not set yet)"),
      dive: (e?: { preventDefault?: () => void }) => { if (!base) { if (e && e.preventDefault) e.preventDefault(); set({ ...S, note: label }); } },
      value: bound ? cellText(value) : "–", hasSpark: !!sp, points, line: sp ? sp.line : "", area: sp ? sp.area : "", stroke: KC[i][0], fill: "url(#" + KC[i][1] + ")",
      pr: active, bd: active ? MAROON : "#E2E6EC", op: isU5 ? 1 : dimOp(on("event"), S.dims.event === label),
      pick: () => set(isU5 ? pair(S, "0-4", "") : toggle(S, "event", label)) };
  });

  // ---- persons / households gauge
  const cur = S.unit === "Persons" ? persons : A.totals.households, den = S.unit === "Persons" ? A.overall.persons : A.overall.households;
  out.unitChips = (["Persons", "Households"] as const).map((u) => ({ label: u, bg: S.unit === u ? MAROON : "#EEF0F3", color: S.unit === u ? "#FFFFFF" : "#6B7280", pr: S.unit === u, pick: () => set({ ...S, unit: u }) }));
  out.gaugeDash = (den ? val(cur) / den * 100 : 0).toFixed(2) + " 100"; out.gaugeValue = cellText(cur); out.gaugeLabel = S.unit;

  // ---- age and gender
  const ag = A.cards.age_sex;
  out.hasAge = !!ag && has("card.age_gender");
  out.ageSexes = ag ? ag.sexes : [];
  if (ag) {
    const [male, female] = ag.sexes, pm = ag.counts[male], pf = ag.counts[female];
    const pmax = Math.max(1, ...pm.map(val), ...pf.map(val));
    const width = (n: Count) => (val(n) ? Math.max(12, Math.round(val(n) / pmax * 100)) : 0);
    out.ageRows = ag.bands.map((a, i) => {
      const rowOff = on("age_band") && S.dims.age_band !== a;
      return { a, mw: width(pm[i]), fw: width(pf[i]), mo: rowOff || S.dims.sex === female ? 0.3 : 1, fo: rowOff || S.dims.sex === male ? 0.3 : 1, ao: rowOff ? 0.3 : 1,
        mpr: S.dims.age_band === a && S.dims.sex === male, apr: S.dims.age_band === a && !S.dims.sex, fpr: S.dims.age_band === a && S.dims.sex === female,
        mn: cellText(pm[i]), fn: cellText(pf[i]),
        mlabel: "Men aged " + a + ": " + cellText(pm[i]), alabel: "Age " + a + ": " + cellText(pm[i] == null || pf[i] == null ? null : val(pm[i]) + val(pf[i])), flabel: "Women aged " + a + ": " + cellText(pf[i]),
        pm: () => set(pair(S, a, male)), pa: () => set(pair(S, a, "")), pf: () => set(pair(S, a, female)) };
    });
  } else out.ageRows = [];

  // ---- coordinates map (one pin per village in the current selection)
  out.hasMap = has("card.coordinates");
  out.mapPins = A.map.map((m) => {
    const s = Math.round(16 + Math.sqrt(m.n) * 1.5), selected = S.village !== "All" && S.village === m.path.village;
    return { lat: m.lat, lon: m.lon, s, bg: "rgba(123,42,61,0.55)", bw: selected ? 4 : 2, bd: selected ? "#1F2430" : MAROON, op: 1,
      label: (m.path.village || "") + (m.path.subcounty ? ", " + m.path.subcounty : "") + ": " + fmt(m.n) + " persons", pick: () => set(pickPath(S, m.path)) };
  });
  out.mapCount = out.mapPins.length + (out.mapPins.length === 1 ? " location" : " locations");

  // ---- ranked / listed cards (pill rows)
  const pillRows = (d: Dim, color: string, sorted: boolean, limit?: number) => {
    const card = A.cards[d]; if (!card) return [];
    const c = card.counts;
    let idx = card.labels.map((_, i) => i); if (sorted) idx.sort((a, b) => val(c[b]) - val(c[a]) || a - b);
    const sel = card.labels.indexOf(S.dims[d] || "");
    if (limit) { const top = idx.slice(0, limit); if (sel >= 0 && top.indexOf(sel) < 0) top[limit - 1] = sel; idx = top; }
    const mx = Math.max(1, ...idx.map((i) => val(c[i])));
    return idx.map((i) => ({ l: card.labels[i], n: cellText(c[i]), h: val(c[i]) ? Math.max(8, Math.round(val(c[i]) / mx * 100)) : 0, c: color,
      op: dimOp(on(d), S.dims[d] === card.labels[i]), pr: S.dims[d] === card.labels[i], label: card.labels[i] + ": " + cellText(c[i]), pick: () => set(toggle(S, d, card.labels[i])) }));
  };
  out.fuelRows = pillRows("fuel", "#CC6A73", false); out.waterRows = pillRows("water", "#3F5FA9", false);
  out.eduRows = pillRows("education", "#B5995A", true); out.occRows = pillRows("occupation", "#5FA48A", true, 6);

  // ---- donut cards with a switch between two variables (only bound variables get a chip)
  const donutFor = (d: Dim) => {
    const card = A.cards[d]; const colors = DONUT_COLORS[d];
    return donut(card ? card.labels.map((name, i) => ({ name, n: card.counts[i], c: colors[i % colors.length], op: dimOp(on(d), S.dims[d] === name), pr: S.dims[d] === name, pick: () => set(toggle(S, d, name)) })) : [], cellText);
  };
  const switchCard = (prefix: string, stateKey: "roofVar" | "netVar" | "migVar", pairs: [Dim, string][]) => {
    const bound = pairs.filter(([d]) => !!A.cards[d]);
    const current = bound.find(([d]) => d === S[stateKey])?.[0] || bound[0]?.[0];
    out[prefix + "Chips"] = bound.map(([d, label]) => ({ label, bg: current === d ? MAROON : "#EEF0F3", color: current === d ? "#FFFFFF" : "#6B7280", pr: current === d,
      pick: () => set({ ...S, [stateKey]: d } as State) }));
    out[prefix + "Bound"] = bound.length > 0;
    const dn = current ? donutFor(current) : donut([], cellText);
    out[prefix + "Segs"] = dn.segs; out[prefix + "Labels"] = dn.labels; out[prefix + "Leaders"] = dn.leaders; out[prefix + "Total"] = fmt(dn.total);
  };
  switchCard("roof", "roofVar", [["roof", "Roof type"], ["floor", "Floor type"]]);
  switchCard("net", "netVar", [["bednet", "Bednet use"], ["toilet", "Toilet type"]]);
  switchCard("mig", "migVar", [["migration", "Migration type"], ["water", "Water source"]]);
  const mar = donutFor("marital"); out.marBound = !!A.cards.marital;
  out.marSegs = mar.segs; out.marLabels = mar.labels; out.marLeaders = mar.leaders; out.marTotal = fmt(mar.total);

  // ---- population and household bars, household assets
  const ref = spec.population_reference || null, scale = ref || Math.max(1, A.overall.persons);
  out.popRef = ref;
  out.popBars = ([["Population", persons, "#DDB765", "Persons"], ["Households", A.totals.households, "#F3EF8B", "Households"]] as const).map(([l, n, c, u]) => ({
    l, n: cellText(n), h: (val(n) / scale * 100).toFixed(1), c, pr: S.unit === u, pick: () => set({ ...S, unit: u }), label: l + ": " + cellText(n) }));
  const as = A.cards.asset;
  out.assetRows = as ? (() => { const amx = Math.max(1, ...as.counts.map(val)); return as.labels.map((a, i) => ({ l: a, n: cellText(as.counts[i]),
    w: val(as.counts[i]) ? Math.max(4, Math.round(val(as.counts[i]) / amx * 100)) : 0, op: dimOp(on("asset"), S.dims.asset === a), pr: S.dims.asset === a,
    label: a + ": " + cellText(as.counts[i]), pick: () => set(toggle(S, "asset", a)) })); })() : [];

  // ---- footer
  out.fPersons = cellText(persons); out.fVillages = A.totals.villages == null ? "–" : String(A.totals.villages); out.fHouseholds = cellText(A.totals.households);
  out.illustrative = spec.privacy.synthetic;
  out.suppressed = A.suppression.applied ? "Counts below " + k + " are shown as <" + k + "." : "";
  return out;
}

export function slotReason(spec: Spec, slot: string): string {
  const v = spec.coverage?.slots?.[slot];
  if (!v || v === "bound") return "";
  const first = v.reason ? v : Object.values(v.parts || v.hidden || {})[0] as any;
  return first && first.status === "needs_definition" ? "Needs definition" : "No data source registered";
}
