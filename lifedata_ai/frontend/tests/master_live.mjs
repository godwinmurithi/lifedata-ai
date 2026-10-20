// Live check of deployed Master dashboards (no browser needed).
//   LIFEDATA_SESSION=<superset session cookie> node tests/master_live.mjs <base-url> <demo-publication-id> [<real-publication-id>]
// Demo: posts every oracle state to POST /lifedata-ai/dashboards/<id>/query and requires the answer to equal the
// APPROVED DESIGN's numbers (master_oracle.mjs). Both: refused inputs are refused, small cells are suppressed, and no
// response body carries person- or household-level data.
import { designVals, normalise, oracleStates } from "./master_oracle.mjs";

const [base, demo, real] = process.argv.slice(2);
const cookie = process.env.LIFEDATA_SESSION;
if (!base || !demo || !cookie) { console.error("usage: LIFEDATA_SESSION=... node tests/master_live.mjs <base-url> <demo-id> [<real-id>]"); process.exit(2); }
let fails = 0, passes = 0;
const ok = (n, c, d = "") => { if (c) passes++; else fails++; console.log((c ? "PASS " : "FAIL ") + n + (d ? "  [" + d + "]" : "")); };
const bodies = [];
async function call(path, init = {}) {
  const r = await fetch(base + path, { ...init, headers: { Cookie: "session=" + cookie, Origin: base, Referer: base + "/lifedata-ai/dashboards/", "Content-Type": "application/json", ...(init.headers || {}) } });
  const text = await r.text(); bodies.push(text);
  let json = null; try { json = JSON.parse(text); } catch {}
  return { status: r.status, json, text };
}
const query = (id, state) => call(`/lifedata-ai/dashboards/${id}/query`, { method: "POST", body: JSON.stringify({ state }) });
const canon = (v) => Array.isArray(v) ? v.map(canon) : v && typeof v === "object" ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, canon(v[k])])) : v;
const js = (x) => Number(x);
/** Server output -> the oracle's neutral shape (as test_master_template.py normalise()). */
function serverShape(o) {
  if (o.empty) return { empty: true, overview: null, sentinel: null, geo: null };
  const ov = o.overview, sen = o.sentinel, geo = o.geo;
  const nz = (points, series) => Object.fromEntries(Object.entries(series).map(([n, vals]) => [n, Object.fromEntries(points.map((p, i) => [p, vals[i]]).filter(([, v]) => v))]));
  const pick = (x, keys) => Object.fromEntries(keys.map((k) => [k, x[k]]));
  return {
    empty: false,
    overview: { ...pick(ov, ["residents", "households", "under_five", "births", "deaths", "net_migration", "ever_registered", "currently_pregnant", "under_five_ever", "outpatient_visits"]),
      vital: nz(ov.vital.points, ov.vital.series), mix: ov.mix, pyramid: ov.pyramid.counts, housing: ov.housing, water: ov.water, bednet: ov.bednet,
      pins: ov.pins.map((p) => [p.village, p.residents]).sort() },
    sentinel: { ...pick(sen, ["sites", "reports", "signals", "rrt", "lab", "lag"]), weekly: nz(sen.weekly.points, sen.weekly.series),
      site_pins: sen.site_pins.map((p) => `${p.name} (${p.status})`).sort(), status_counts: sen.status_counts, pathogen: sen.pathogen,
      signal_rows: sen.signal_rows.map((r) => [r.facility, r.label, r.severity]) },
    geo: { ...pick(geo, ["tier_counts", "clusters", "high_risk", "pop_at_risk", "mean_vulnerability", "radius", "p_value"]),
      pins: geo.pins.map((p) => `${p.village}: ${js(p.cases)} cases, ${p.tier} tier`).sort(),
      scatter: geo.scatter.map((p) => `${p.village}: ${js(p.cases)} cases, vulnerability ${js(p.vulnerability)}`),
      hotspots: geo.hotspots.map((p) => [p.village, js(p.cases), p.tier]) },
  };
}
const sortPins = (x) => (x.overview ? { ...x, overview: { ...x.overview, pins: [...x.overview.pins].sort((a, b) => String(a).localeCompare(String(b))) } } : x);

// ---- demo == the approved design, for every state
for (const [name, state] of oracleStates()) {
  const r = await query(demo, state);
  if (r.status !== 200) { ok("demo == design: " + name, false, "HTTP " + r.status + " " + r.text.slice(0, 200)); continue; }
  const got = JSON.stringify(canon(sortPins(serverShape(r.json.data)))), want = JSON.stringify(canon(sortPins(normalise(designVals(state)))));
  ok("demo == design: " + name, got === want, got === want ? "" : "got " + got.slice(0, 300) + " want " + want.slice(0, 300));
}
const d0 = (await query(demo, {})).json;
ok("demo defaults: 180 residents, 24 sentinel sites, 14 high-risk villages", d0.data.overview.residents === 180 && d0.data.sentinel.sites === 24 && d0.data.geo.high_risk === 14);
ok("demo answers are flagged illustrative", d0.illustrative === true);
const spec = (await call(`/lifedata-ai/api/hdss/dashboards/${demo}/spec/`)).json;
ok("demo spec: draft, three tabs, no layout", spec.status === "draft" && spec.tabs.every((t) => t.visible) && !("layout" in spec));

// ---- refusals
for (const [label, state, code] of [["a person key as a filter", { dims: { person_key: "1" } }, 400], ["a household key as a filter", { dims: { household_key: "1" } }, 400],
  ["an injected value", { dims: { housing: "Poor' OR 1=1 --" } }, 400], ["an unknown village", { geo: { village: "Atlantis" } }, 400],
  ["an unknown period", { period: "Last 6 months" }, 400], ["a malformed time point", { time_point: "2026-06'; drop" }, 400]]) {
  const r = await query(demo, state); ok("refuses " + label, r.status === code, "HTTP " + r.status);
}
const anon = await fetch(base + `/lifedata-ai/dashboards/${demo}/query`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
ok("refuses a request with no session", [302, 401, 403].includes(anon.status), "HTTP " + anon.status);

// ---- the real instance: Overview only, Country and Site, small cells suppressed
if (real) {
  const rs = (await call(`/lifedata-ai/api/hdss/dashboards/${real}/spec/`)).json;
  ok("real: draft, Overview only, geography Country and Site", rs.status === "draft" && rs.tabs.filter((t) => t.visible).map((t) => t.id).join() === "overview" && rs.geography.join() === "country,site");
  const r0 = await query(real, {});
  ok("real: answers", r0.status === 200, "HTTP " + r0.status + " " + r0.text.slice(0, 200));
  const k = rs.privacy.min_cell_size, small = [];
  const walk = (v, p) => { if (typeof v === "number" && v > 0 && v < k && /residents|households|under_five|births|deaths|mix|pyramid|series/.test(p) && !/net_migration/.test(p)) small.push(p + "=" + v);
    else if (v && typeof v === "object") for (const [kk, vv] of Object.entries(v)) walk(vv, p + "." + kk); };
  walk(r0.json?.data?.overview, "overview");
  ok("real: no count below the minimum cell size (" + k + ") is sent", small.length === 0, small.slice(0, 8).join(", "));
  const ov = r0.json?.data?.overview || {};
  console.log("real instance, no filter: residents " + ov.residents + ", households " + ov.households + ", under five " + ov.under_five + ", births " + ov.births + ", deaths " + ov.deaths + ", net migration " + ov.net_migration);
  for (const site of new Set((r0.json?.data?.paths || []).map((p) => p.site).filter(Boolean))) {
    const r = await query(real, { geo: { country: (r0.json.data.paths.find((p) => p.site === site) || {}).country, site } });
    ok("real: site " + site + " answers", r.status === 200, "HTTP " + r.status + " " + r.text.slice(0, 160));
  }
}

// ---- no person- or household-level data in any body
// refusals name the key they refused; only answered bodies are scanned
const answered = bodies.filter((b) => !/^\{"error": /.test(b));
const leak = answered.filter((b) => /household_key|person_key|"hh"|"per"|"rows"/.test(b));
ok("no answer carries person- or household-level fields (" + answered.length + " bodies scanned)", leak.length === 0 && answered.length > 60, leak.length + " bodies");
console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
