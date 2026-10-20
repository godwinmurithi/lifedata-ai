// Live check of a deployed HDSS template demo against the oracle (no browser needed).
//   LIFEDATA_SESSION=<superset session cookie> node tests/hdss_live.mjs <base-url> <publication-id>
// Posts every oracle state to POST /lifedata-ai/dashboards/<id>/query and requires the aggregates to equal
// the oracle exactly; checks that refused inputs are refused; and scans every response body for
// person- or household-level data (identifiers, row arrays).
import { aggregates, oracleStates } from "./hdss_oracle.mjs";

const [base, id] = process.argv.slice(2);
const cookie = process.env.LIFEDATA_SESSION;
if (!base || !id || !cookie) { console.error("usage: LIFEDATA_SESSION=... node tests/hdss_live.mjs <base-url> <publication-id>"); process.exit(2); }
let fails = 0, passes = 0;
const ok = (n, c, d = "") => { if (c) passes++; else fails++; console.log((c ? "PASS " : "FAIL ") + n + (d ? "  [" + d + "]" : "")); };
const bodies = [];
async function call(path, init = {}) {
  const r = await fetch(base + path, { ...init, headers: { Cookie: "session=" + cookie, Origin: base, Referer: base + "/lifedata-ai/dashboards/", "Content-Type": "application/json", ...(init.headers || {}) } });
  const text = await r.text(); bodies.push(text);
  let json = null; try { json = JSON.parse(text); } catch {}
  return { status: r.status, json, text };
}
const query = (state) => call(`/lifedata-ai/dashboards/${id}/query`, { method: "POST", body: JSON.stringify({ state }) });
// key order differs between Python and JS objects; compare canonically
const canon = (v) => Array.isArray(v) ? v.map(canon) : v && typeof v === "object" ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, canon(v[k])])) : v;
const sortMap = (d) => canon({ ...d, map: [...d.map].map((m) => ({ ...m, lat: +m.lat.toFixed(6), lon: +m.lon.toFixed(6) })).sort((a, b) => a.path.village.localeCompare(b.path.village)),
  geography: { ...d.geography, paths: [...d.geography.paths].sort((a, b) => a.village.localeCompare(b.village)) } });

for (const [name, state] of oracleStates()) {
  const r = await query(state);
  const got = r.json && r.json.data ? JSON.stringify(sortMap(r.json.data)) : r.text.slice(0, 200), want = JSON.stringify(sortMap(aggregates(state)));
  ok("live = oracle: " + name, r.status === 200 && got === want, r.status === 200 && got !== want ? firstDiff(JSON.parse(got), JSON.parse(want)) : r.status === 200 ? "" : "HTTP " + r.status + " " + got);
}
const spec = await call(`/lifedata-ai/api/hdss/dashboards/${id}/spec/`);
ok("spec: draft or published, synthetic, every slot bound, no layout or HTML", spec.status === 200 && ["draft", "published"].includes(spec.json.status) && spec.json.privacy.synthetic === true &&
  spec.json.slots.length === 21 && !("layout" in spec.json) && !/<[a-z]/i.test(JSON.stringify(spec.json)));
for (const [label, state, status] of [["filter on a personal/key column", { dims: { record_id: "1" } }, 400], ["filter on household_id", { dims: { household_id: "HH-001" } }, 400],
  ["SQL in a filter value", { dims: { fuel: "Firewood' OR '1'='1" } }, 400], ["unknown geography", { geo: { village: "Atlantis" } }, 400], ["bad date", { from: "2026-02-30" }, 400]]) {
  const r = await query(state); ok("refused: " + label, r.status === status, "HTTP " + r.status + " " + (r.json?.error || ""));
}
const unregistered = await call("/lifedata-ai/api/hdss/dashboards/", { method: "POST", body: JSON.stringify({ dataset: "farmers", title: "x" }) });
ok("refused: unregistered dataset", unregistered.status === 400 && /not a registered HDSS dataset/.test(unregistered.json?.error || ""), unregistered.json?.error);
const noCookie = await fetch(`${base}/lifedata-ai/dashboards/${id}/query`, { method: "POST", headers: { Origin: base, "Content-Type": "application/json" }, body: "{}" });
ok("refused: no session", noCookie.status === 401, "HTTP " + noCookie.status);
// Identifier VALUES must appear nowhere; identifier KEYS and row arrays nowhere in aggregate responses.
// (The spec legitimately names columns in its role map, e.g. "household_id": "household_id".)
const leaks = bodies.filter((t) => /HH-\d{3}/.test(t) || (t.includes('"data"') && /"record_id"|"household_id"|"individual_id"|"rows"\s*:/.test(t)));
ok("no person- or household-level data in any of " + bodies.length + " responses", leaks.length === 0, leaks.length + " leaking");
console.log(fails ? `\nFAILED: ${fails} (passed ${passes})` : `\nALL PASSED (${passes})`);
process.exit(fails ? 1 : 0);

function firstDiff(a, b, path = "") {
  if (JSON.stringify(a) === JSON.stringify(b)) return "";
  if (a && b && typeof a === "object" && typeof b === "object") {
    for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) { const d = firstDiff(a[k], b[k], path + "." + k); if (d) return d; }
  }
  return path + ": got " + JSON.stringify(a)?.slice(0, 80) + " want " + JSON.stringify(b)?.slice(0, 80);
}
