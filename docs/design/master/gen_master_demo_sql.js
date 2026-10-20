// Build the synthetic Master-dashboard demo datasets from docs/design/master/sample_data.json.
// One packed string literal per entity (Superset parses a long literal cheaply), unpacked by Postgres.
// Usage: node gen_master_demo_sql.js <sample_data.json> <outdir>
const fs = require("fs"), path = require("path");
const R = JSON.parse(fs.readFileSync(process.argv[2], "utf8")), OUT = process.argv[3];
const AGES = ["65+", "55-64", "45-54", "35-44", "25-34", "15-24", "5-14", "0-4"];
const EVN = ["Births", "Deaths", "In-migration", "Out-migration", "Pregnancies"];
const HOUSN = ["Good", "Moderate", "Poor", "Dilapidated"], WATN = ["Safe water", "Improved toilet", "Open defecation"], NETN = ["With bednet", "Without bednet"];
const SYNN = ["Fever", "Respiratory", "Diarrhea", "Rash"], PATHN = ["Malaria", "Respiratory", "Diarrheal", "Measles"];
const WEEKS = ["W15", "W16", "W17", "W18", "W19", "W20", "W21"];
// The design's seven months are April-October 2026 (reference date 2026-10-20); an event in month m is dated the 15th.
const monthDate = (m) => `2026-${String(4 + m).padStart(2, "0")}-15`;
const esc = (s) => String(s).replace(/'/g, "''");
const SEP = "\u001f", REC = "\u001e";
const ROWS = {}; // the same rows as typed JSON, for the server tests (tests/fixtures/master_demo_rows.json)
function packed(name, header, cols, rows) {
  const typed = (x, cast) => (x === null || x === undefined || x === "" ? null : cast === "INTEGER" || cast === "NUMERIC" ? Number(x) : String(x));
  ROWS[name] = rows.map((r) => Object.fromEntries(cols.map(([c, i, cast]) => [c, typed(r[i - 1], cast)])));
  const lit = rows.map((r) => r.map((x) => (x === null || x === undefined ? "" : String(x))).join(SEP)).join(REC);
  const sel = cols.map(([c, i, cast]) => {
    const raw = `NULLIF(c[${i}], '')`;
    return `  ${cast ? `CAST(${raw} AS ${cast})` : raw} AS ${c}`;
  }).join(",\n");
  const sql = `-- ${header} Synthetic (docs/design/master/sample_data.json). Not real people or real surveillance.\n` +
    `SELECT\n${sel}\nFROM (SELECT string_to_array(rec, chr(31)) AS c FROM unnest(string_to_array('${esc(lit)}', chr(30))) AS t(rec)) AS packed`;
  fs.writeFileSync(path.join(OUT, name + ".sql"), sql);
  return sql.length;
}
const V = R.villages;
const sizes = {
  master_demo_villages: packed("master_demo_villages", "Villages.", [["village_key", 1, "INTEGER"], ["country", 2], ["district", 3], ["county", 4], ["subcounty", 5], ["parish", 6], ["village", 7],
    ["latitude", 8, "NUMERIC"], ["longitude", 9, "NUMERIC"], ["risk_tier", 10], ["vulnerability", 11, "NUMERIC"], ["cases", 12, "INTEGER"], ["population", 13, "INTEGER"],
    ["ever_registered", 14, "INTEGER"], ["under_five_ever", 15, "INTEGER"], ["outpatient_visits", 16, "INTEGER"]],
    V.map((v) => [v.id, v.co, v.di, v.cn, v.sc, v.pa, v.vi, v.lat, v.lon, v.tier, v.vuln, v.cases, v.pop, v.ever, v.u5e, v.opd])),
  master_demo_households: packed("master_demo_households", "Households.", [["household_key", 1, "INTEGER"], ["village_key", 2, "INTEGER"], ["housing", 3], ["water_sanitation", 4], ["bednet", 5]],
    R.hh.map((h, i) => [i, h[0], HOUSN[h[1]], WATN[h[2]], NETN[h[3]]])),
  master_demo_persons: packed("master_demo_persons", "Persons (residents).", [["person_key", 1, "INTEGER"], ["village_key", 2, "INTEGER"], ["household_key", 3, "INTEGER"], ["age_band", 4], ["sex", 5]],
    R.per.map((p, i) => [i, p[0], p[1], AGES[p[2]], p[3] === 0 ? "Male" : "Female"])),
  master_demo_events: packed("master_demo_events", "Vital events.", [["event_key", 1, "INTEGER"], ["village_key", 2, "INTEGER"], ["event_type", 3], ["event_date", 4, "DATE"]],
    R.ev.map((e, i) => [i, e[0], EVN[e[2]], monthDate(e[1])])),
  master_demo_sites: packed("master_demo_sites", "Sentinel sites.", [["site_key", 1, "INTEGER"], ["facility", 2], ["village_key", 3, "INTEGER"], ["site_status", 4], ["reports", 5, "INTEGER"], ["rrt_deployments", 6, "INTEGER"], ["reporting_lag_days", 7, "NUMERIC"]],
    R.sites.map((s, i) => [i, s.n, s.vid, s.st, s.rep, s.rrt, s.lag])),
  master_demo_site_weeks: packed("master_demo_site_weeks", "Weekly syndrome counts per site (week i is aligned to month i, as in the design).",
    [["site_key", 1, "INTEGER"], ["week_label", 2], ["period_date", 3, "DATE"], ["syndrome", 4], ["cases", 5, "INTEGER"]],
    R.sites.flatMap((s, i) => s.syn.flatMap((series, k) => series.map((n, w) => [i, WEEKS[w], monthDate(w), SYNN[k], n])))),
  master_demo_site_lab: packed("master_demo_site_lab", "Lab-confirmed counts per site and pathogen.", [["site_key", 1, "INTEGER"], ["pathogen", 2], ["confirmed", 3, "INTEGER"]],
    R.sites.flatMap((s, i) => s.lab.map((n, k) => [i, PATHN[k], n]))),
  master_demo_signals: packed("master_demo_signals", "Sentinel signals (illustrative, not real alerts).", [["signal_key", 1, "INTEGER"], ["site_key", 2, "INTEGER"], ["signal_label", 3], ["syndrome", 4], ["severity", 5], ["detected", 6]],
    R.signals.map((g, i) => [i, g.s, g.syn, g.si >= 0 ? SYNN[g.si] : null, g.sev, g.when])),
  master_demo_clusters: packed("master_demo_clusters", "Spatial clusters (illustrative).", [["village_key", 1, "INTEGER"], ["cases", 2, "INTEGER"], ["vulnerability", 3, "NUMERIC"], ["radius_km", 4, "NUMERIC"], ["p_value", 5, "NUMERIC"]],
    R.clusters.map((c) => [c.id, c.cases, c.vuln, c.rad, c.p])),
};
if (process.argv[4]) fs.writeFileSync(process.argv[4], JSON.stringify(ROWS));
console.log(JSON.stringify(sizes));
