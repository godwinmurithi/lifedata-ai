// Acceptance tests for the HDSS template's filter model and links. Run: node acceptance_tests.js   (Node 18+)
// They load the reference design and sample data next to this file, call every click handler, and compare the results
// with an independent calculation from the raw data. Port them to the live template as the oracle.
const fs = require("fs");
const html = fs.readFileSync(require("path").join(__dirname, "design_hdss_template.dc.html"), "utf8");
const script = html.split('data-dc-script data-props=\'')[1].split("'>")[1].split("</script>")[0];
const markup = html.split("<x-dc>")[1].split("</x-dc>")[0];
const R = JSON.parse(fs.readFileSync(require("path").join(__dirname, "sample_data.json"), "utf8"));
class DCLogic { constructor(p) { this.props = p || {}; this.state = {}; } setState(p) { this.state = Object.assign({}, this.state, p); } }
const Component = new Function("DCLogic", script + "\nreturn Component;")(DCLogic);
let fails = 0; const ok = (n, c, d = "") => { console.log((c ? "PASS " : "FAIL ") + n + (d !== "" ? "  [" + d + "]" : "")); if (!c) fails++; };
const mk = (st) => { const c = new Component({}); Object.assign(c.state, st || {}); return c; };
const num = (s) => Number(String(s).replace(/,/g, ""));
const o0 = mk().renderVals();

// ------------------------------------------------ independent raw-data calculation (written differently on purpose)
const V = R.villages, H = R.hh, P = R.per;
const asDay = (s) => Math.round((Date.parse(s + "T00:00:00Z") - Date.UTC(2025, 10, 1)) / 86400000);
function expected(f) {   // f: dim -> value, plus geo names and from/to/period
  const vOk = (i) => ["district", "county", "subcounty", "parish", "village"].every(l => f[l] === undefined || f[l] === "All" || V[i][{ district: "di", county: "cn", subcounty: "sc", parish: "pa", village: "vi" }[l]] === f[l]);
  const lo = Math.max(f.from ? asDay(f.from) : -1e9, f.period === "Last 3 months" ? 353 - 91 : f.period === "Last 6 months" ? 353 - 182 : -1e9), hi = f.to ? asDay(f.to) : 1e9;
  const rows = P.filter(r => {
    const h = H[r[0]]; const p = { a: r[1], s: r[2], edu: r[3], occ: r[4], mar: r[5], mig: r[6], ev: r[7], bed: r[8], dd: r[9] };
    if (!vOk(h[0]) || p.dd < lo || p.dd > hi) return false;
    if (f.sex && p.s !== (f.sex === "M" ? 0 : 1)) return false;
    for (const k of ["age", "ev", "edu", "occ", "mar", "mig", "bed"]) { if (f[k] === undefined || f[k] < 0) continue; const val = { age: p.a, ev: p.ev, edu: p.edu, occ: p.occ, mar: p.mar, mig: p.mig, bed: p.bed }[k]; if (val !== f[k]) return false; }
    for (const [k, i] of [["fuel", 1], ["water", 2], ["roof", 3], ["floor", 4], ["toilet", 5]]) if (f[k] !== undefined && f[k] >= 0 && h[i] !== f[k]) return false;
    if (f.ast !== undefined && f.ast >= 0 && !((h[6] >> f.ast) & 1)) return false;
    return true;
  });
  return { rows, persons: rows.length, hh: new Set(rows.map(r => r[0])).size, villages: new Set(rows.map(r => H[r[0]][0])).size };
}
function compare(name, f) {
  const o = mk(f).renderVals(), e = expected(f);
  const got = [o.fPersons, o.fHouseholds, o.fVillages, o.gaugeValue].map(num).join(), want = [e.persons, e.hh, e.villages, e.persons].join();
  ok(name, got === want, "persons, households, villages, gauge = " + got + (got === want ? "" : "  expected " + want));
}

console.log("--- 1. defaults reproduce every number in the screenshot");
ok("header, footer", o0.hdrCount === "420 person records" && [o0.fPersons, o0.fHouseholds, o0.fVillages].join() === "420,84,6" && o0.mapCount === "6 locations");
ok("KPI cards", o0.kpis.map(k => k.label + " " + k.value).join() === "Births 19,Deaths 20,In-migration 63,Out-migration 49,Under five 78");
ok("gauge shows 420, full", o0.gaugeValue === "420" && o0.gaugeDash.startsWith("100.00"));
ok("cooking fuel", o0.fuelRows.map(r => r.l + " " + r.n).join() === "Firewood 217,Charcoal 136,Gas 47,Electricity 20");
ok("water source", o0.waterRows.map(r => r.l + " " + r.n).join() === "Tap 75,Borehole 91,Well 73,Other 181");
ok("education (ranked)", o0.eduRows.map(r => r.l + " " + r.n).join() === "None 155,Secondary 97,Tertiary 91,Primary 77");
ok("occupation (top six, ranked)", o0.occRows.map(r => r.l + " " + r.n).join() === "Student 146,None 78,Trader 50,Farmer 42,Employed 40,Homemaker 33");
ok("roof donut: 158 recorded", o0.roofTotal === "158" && o0.roofLabels.map(l => l.name + " " + l.n).join() === "Iron 50,Thatched 58,Tiled 50");
ok("bednet donut", o0.netTotal === "420" && o0.netLabels.map(l => l.name + " " + l.n).join() === "Yes 308,No 112");
ok("migration donut", o0.migTotal === "420" && o0.migLabels.map(l => l.name + " " + l.n).join() === "None 258,In-migrant 63,Visitor 50,Out-migrant 49");
ok("marital donut", o0.marTotal === "420" && o0.marLabels.map(l => l.name + " " + l.n).join() === "Single 245,Divorced 68,Married 60,Widowed 47");
ok("population and household bars", o0.popBars.map(b => b.l + " " + b.n).join() === "Population 420,Households 84");
ok("household assets", o0.assetRows.map(r => r.l + " " + r.n).join() === "Radio 275,TV 169,Phone 239,Fridge 46,Vehicles 42");
ok("age and gender: 6 bands, under five = 78", o0.ageRows.length === 6 && o0.ageRows[5].alabel === "Age 0-4: 78");

console.log("--- 2. every element is wired: click sets the shared filter, click again clears it");
function wired(name, getHandler, key, want, none) {
  const c = mk(); const h = getHandler(c.renderVals()); if (typeof h !== "function") { ok(name, false, "no handler"); return; }
  h(); const set = JSON.stringify(c.state[key]) === JSON.stringify(want); getHandler(c.renderVals())(); const cleared = JSON.stringify(c.state[key]) === JSON.stringify(none);
  ok(name, set && cleared, key + " -> " + JSON.stringify(c.state[key] === none ? want : c.state[key]) + " -> cleared");
}
[["Births", 0], ["Deaths", 1], ["In-migration", 2], ["Out-migration", 3]].forEach(([n, k]) => wired("KPI card " + n, o => o.kpis[k].pick, "ev", k, -1));
wired("KPI card Under five", o => o.kpis[4].pick, "age", 5, -1);
for (let b = 0; b < 6; b++) { wired("age pill (men) band " + b, o => o.ageRows[b].pm, "age", b, -1); }
ok("age: men pill = band+M, label = band, women pill = band+F", (() => { const c = mk(); c.renderVals().ageRows[2].pm(); const a = [c.state.age, c.state.sex].join(); c.renderVals().ageRows[2].pa(); const b = [c.state.age, c.state.sex].join(); c.renderVals().ageRows[2].pf(); const d = [c.state.age, c.state.sex].join(); return a === "2,M" && b === "2," && d === "2,F"; })());
["fuel:fuelRows:4", "water:waterRows:4", "edu:eduRows:4", "occ:occRows:6"].forEach(s => { const [key, list, n] = s.split(":"); for (let i = 0; i < +n; i++) ok(key + " row " + i + " toggles", (() => { const c = mk(); const row = c.renderVals()[list][i]; row.pick(); const idx = c.state[key]; const lbl = c.renderVals()[list].find(r => r.pr); row.pick; return idx >= 0 && !!lbl && lbl.l === row.l; })()); });
for (const [label, key, n] of [["roof", "roof", 3], ["bednet", "bed", 2], ["migration", "mig", 4], ["marital", "mar", 4]]) {
  const prefix = { roof: "roof", bed: "net", mig: "mig", mar: "mar" }[key];
  ok(label + " donut: every label and slice is clickable and sets the filter", (() => { let good = true; for (let j = 0; j < n; j++) { const c = mk(); const o = c.renderVals(); o[prefix + "Labels"][j].pick(); good = good && c.state[key] >= 0; const c2 = mk(); c2.renderVals()[prefix + "Segs"][j].pick(); good = good && c2.state[key] === c.state[key]; } return good; })());
}
ok("the other variables of each switch card filter too (floor, toilet, water)", (() => { let g = true; for (const [sv, v, key, prefix] of [["roofVar", "floor", "floor", "roof"], ["netVar", "toilet", "toilet", "net"], ["migVar", "water", "water", "mig"]]) { const p = {}; p[sv] = v; const c = mk(p); c.renderVals()[prefix + "Labels"][0].pick(); g = g && c.state[key] >= 0; } return g; })());
ok("switch chips change the card's variable", (() => { const c = mk(); c.renderVals().roofChips[1].pick(); const a = c.state.roofVar === "floor"; c.renderVals().netChips[1].pick(); const b = c.state.netVar === "toilet"; c.renderVals().migChips[1].pick(); const d = c.state.migVar === "water"; c.renderVals().unitChips[1].pick(); return a && b && d && c.state.unit === "Households"; })());
ok("switching variable shows its own categories", (() => { const o = mk({ roofVar: "floor", netVar: "toilet", migVar: "water" }).renderVals(); return o.roofLabels.map(l => l.name).sort().join() === "Cement,Earth,Tiled" && o.netLabels.length === 4 && o.migLabels.map(l => l.name).sort().join() === "Borehole,Other,Tap,Well"; })());
ok("household assets bars toggle", [0, 1, 2, 3, 4].every(k => { const c = mk(); c.renderVals().assetRows[k].pick(); const a = c.state.ast === k; c.renderVals().assetRows[k].pick(); return a && c.state.ast === -1; }));
ok("population / household bars switch the gauge unit", (() => { const c = mk(); c.renderVals().popBars[1].pick(); const a = c.state.unit === "Households"; c.renderVals().popBars[0].pick(); return a && c.state.unit === "Persons"; })());
ok("map pin sets the whole village path", (() => { const c = mk(); c.renderVals().mapPins[2].pick(); return ["district", "county", "subcounty", "parish", "village"].every(l => c.state[l] !== "All"); })());
ok("geography dropdowns cascade and reset lower levels", (() => { const c = mk(); c.renderVals().onDistrict({ target: { value: "Mayuge" } }); const a = c.renderVals().optVillage.length === 4 && c.renderVals().optCounty.length === 2; c.renderVals().onSubcounty({ target: { value: "Bukooma" } }); c.renderVals().onDistrict({ target: { value: "Iganga" } }); return a && c.state.subcounty === "All" && c.state.village === "All"; })());
ok("time period and date range (Apply commits the range)", (() => { const c = mk(); c.renderVals().onFromD({ target: { value: "2026-05-01" } }); const before = c.state.from === ""; c.renderVals().applyNow(); return before && c.state.from === "2026-05-01"; })());
ok("clear all resets everything including drafts", (() => { const c = mk({ ev: 1, age: 2, sex: "M", fuel: 0, water: 1, edu: 2, occ: 3, mar: 1, mig: 2, bed: 0, roof: 1, ast: 3, district: "Iganga", fromD: "2026-01-01", from: "2026-01-01", unit: "Households", roofVar: "floor" }); c.renderVals().clearAll(); return JSON.stringify(c.state) === JSON.stringify(mk().state); })());

console.log("--- 3. one selection filters every card: numbers match an independent calculation from the raw data");
compare("no filter", {});
for (let k = 0; k < 4; k++) compare("event " + ["Births", "Deaths", "In-migration", "Out-migration"][k], { ev: k });
for (let b = 0; b < 6; b++) compare("age band " + b, { age: b }); compare("men only", { sex: "M" }); compare("women 25-44", { age: 2, sex: "F" });
for (let k = 0; k < 4; k++) { compare("fuel " + k, { fuel: k }); compare("water " + k, { water: k }); compare("education " + k, { edu: k }); compare("marital " + k, { mar: k }); compare("migration " + k, { mig: k }); compare("toilet " + k, { toilet: k }); }
for (let k = 0; k < 7; k++) compare("occupation " + k, { occ: k });
for (let k = 0; k < 3; k++) { compare("roof " + k, { roof: k }); compare("floor " + k, { floor: k }); }
for (let k = 0; k < 2; k++) compare("bednet " + k, { bed: k }); for (let k = 0; k < 5; k++) compare("asset " + k, { ast: k });
for (const d of ["Iganga", "Mayuge"]) compare("district " + d, { district: d }); compare("village Kisoko", { district: "Iganga", county: "Kigulu South", subcounty: "Namungalwe", parish: "Bukanga", village: "Kisoko" });
compare("Last 6 months", { period: "Last 6 months" }); compare("Last 3 months", { period: "Last 3 months" }); compare("date range", { from: "2026-01-01", to: "2026-03-31" });
compare("combined: fuel + education + district", { fuel: 0, edu: 2, district: "Iganga" }); compare("combined: roof + bednet + age", { roof: 1, bed: 0, age: 4 });
compare("combined: asset + marital + period", { ast: 2, mar: 0, period: "Last 6 months" }); compare("combined: event + occupation + sex", { ev: 2, occ: 4, sex: "F" });

console.log("--- 4. a card that is itself a filter keeps showing its alternatives; the others follow");
const fw = mk({ fuel: 0 }).renderVals();
ok("cooking fuel keeps all four rows with unfiltered counts; selected highlighted, others dimmed", fw.fuelRows.map(r => r.n).join() === "217,136,47,20" && fw.fuelRows[0].op === 1 && fw.fuelRows[1].op === 0.35);
ok("water source follows the fuel selection", fw.waterRows.reduce((a, r) => a + r.n, 0) === 217, "persons in Firewood households = " + fw.waterRows.reduce((a, r) => a + r.n, 0));
const ev = mk({ ev: 0 }).renderVals();
ok("event cards keep all four values while Births is selected; other cards narrow to birth records", ev.kpis.map(k => k.value).join() === "19,20,63,49,78".replace("78", String(ev.kpis[4].value)) && ev.kpis[0].op === 1 && ev.kpis[1].op === 0.35 && ev.gaugeValue === "19" && ev.kpis[4].value === "19");
const ag = mk({ age: 5 }).renderVals();
ok("age pills keep every band while one is selected; persons shows only that band", ag.ageRows.length === 6 && ag.ageRows[0].mw > 0 && ag.gaugeValue === "78" && ag.ageRows[5].ao === 1 && ag.ageRows[0].ao === 0.3);
ok("the Under five card stays 78 under a non-age filter on its own dimension", mk({ age: 0 }).renderVals().kpis[4].value === "78");
ok("households unit: gauge counts households", (() => { const o = mk({ unit: "Households", fuel: 3 }).renderVals(); return o.gaugeLabel === "Households" && num(o.gaugeValue) === expected({ fuel: 3 }).hh; })());
ok("header shows 'n of 420' when filtered", mk({ fuel: 3 }).renderVals().hdrCount === "20 of 420 person records");
ok("the map zooms to the selection", mk({ district: "Mayuge" }).renderVals().mapPins.length === 3 && mk({ district: "Mayuge" }).renderVals().mapCount === "3 locations");
ok("empty selection shows the message", (() => { const o = mk({ ev: 0, age: 0 }).renderVals(); return o.isEmpty === true && o.showBody === false; })());
ok("a time filter narrows the KPI values but the sparklines keep all 12 months", (() => { const o = mk({ period: "Last 3 months" }).renderVals(); return num(o.kpis[2].value) < 63 && o.kpis.every(k => (k.line.match(/C/g) || []).length === 11); })());
ok("every active dimension has a removable chip", mk({ ev: 1, age: 3, sex: "M", fuel: 2, water: 1, edu: 2, occ: 3, mar: 1, mig: 2, bed: 0, roof: 1, floor: 0, toilet: 2, ast: 3, district: "Iganga", period: "Last 6 months", from: "2026-01-01", to: "2026-05-01" }).renderVals().chips.length === 17);

console.log("--- 5. markup safety (from the runtime source)");
const holes = [...markup.matchAll(/\{\{\s*([A-Za-z0-9_.$]+)\s*\}\}/g)].map(m => m[1]);
const loopVars = {}; [...markup.matchAll(/<sc-for list="\{\{\s*(\w+)\s*\}\}" as="(\w+)"/g)].forEach(m => { loopVars[m[2]] = m[1]; });
const missing = [...new Set(holes)].filter(h => { const [r, f] = h.split("."); if (["true", "false"].includes(r)) return false; if (loopVars[r] && f) return false; return !(r in o0) && !loopVars[r]; });
ok("every top-level placeholder has a value", missing.length === 0, missing.join(", "));
const withChips = mk({ district: "Iganga" }).renderVals(); const bad = [];
for (const [, list, as_, inner] of [...markup.matchAll(/<sc-for list="\{\{\s*(\w+)\s*\}\}" as="(\w+)"[^>]*>([\s\S]*?)<\/sc-for>/g)]) {
  const arr = list === "chips" ? withChips.chips : o0[list]; if (!Array.isArray(arr) || !arr.length) { bad.push(list + " (empty)"); continue; }
  for (const f of new Set([...inner.matchAll(new RegExp("\\{\\{\\s*" + as_ + "\\.(\\w+)\\s*\\}\\}", "g"))].map(m => m[1]))) if (!(f in arr[0])) bad.push(list + "." + f);
}
ok("every loop field exists on its items (" + (markup.match(/<sc-for /g) || []).length + " loops)", bad.length === 0, bad.join(", "));
ok("loops and conditionals balanced", (markup.match(/<sc-for /g) || []).length === (markup.match(/<\/sc-for>/g) || []).length && (markup.match(/<sc-if /g) || []).length === (markup.match(/<\/sc-if>/g) || []).length);
ok("no text placeholder inside SVG <text> or <option>; options carry a label attribute", !/<text[^>]*>[^<]*\{\{/.test(markup) && !/<option[^>]*>[^<]*\{\{/.test(markup) && (markup.match(/<option [^>]*label="\{\{ o\.v \}\}"><\/option>/g) || []).length === (markup.match(/<option /g) || []).length);
let depth = 0, nested = 0; for (const m of markup.matchAll(/<button\b|<\/button>/g)) { if (m[0] === "</button>") depth--; else { depth++; if (depth > 1) nested++; } }
ok("no button sits inside another button", nested === 0 && depth === 0, "nested " + nested);
ok("every clickable list item has a handler", ["kpis", "unitChips", "ageRows", "mapPins", "fuelRows", "waterRows", "eduRows", "occRows", "roofChips", "roofSegs", "roofLabels", "netChips", "netSegs", "netLabels", "migChips", "migSegs", "migLabels", "marSegs", "marLabels", "popBars", "assetRows"].every(k => o0[k].every(it => ["pick", "pm"].some(h => typeof it[h] === "function"))));

console.log("--- 6. age and gender fits its card; deep dives are visible and link-ready");
const ageGroup = markup.match(/<div role="group" aria-label="Age and gender[\s\S]*?<\/sc-for>\s*<\/div>/)[0];
ok("age pills are a share of their half-row (0-100%), the longest fills it", o0.ageRows.every(r => r.mw >= 0 && r.mw <= 100 && r.fw >= 0 && r.fw <= 100) && Math.max(...o0.ageRows.flatMap(r => [r.mw, r.fw])) === 100, o0.ageRows.map(r => r.mw + "/" + r.fw).join(" "));
ok("age rows use flexible halves, not fixed pixel widths, so nothing can overflow the card", !/width:\d+px/.test(ageGroup.replace(/width:42px/g, "").replace(/height:\d+px/g, "")) && (ageGroup.match(/flex:1 1 0; min-width:0/g) || []).length === 2 && /width:100%/.test(ageGroup));
ok("the age chart group fills the card's height", /flex:1; min-height:0/.test(ageGroup) && /justify-content:space-around/.test(ageGroup));
ok("pill widths stay in range under filters", ["fuel", "edu", "occ"].every(k => [0, 1, 2].every(v => { const p = {}; p[k] = v; return mk(p).renderVals().ageRows.every(r => r.mw >= 0 && r.mw <= 100 && r.fw >= 0 && r.fw <= 100); })));
const kloop = markup.match(/<sc-for list="\{\{ kpis \}\}"[\s\S]*?<\/sc-for>/)[0];
ok("the deep-dive link is a sibling of the card button, not inside it", /<\/button><a href="\{\{ k\.link \}\}"/.test(kloop) && !/<button[^>]*>(?:(?!<\/button>)[\s\S])*<a /.test(kloop));
ok("deep-dive control is a real, labelled link showing the supplied icon at a visible size (48px)", /aria-label="\{\{ k\.dlabel \}\}"/.test(kloop) && /width:48px; height:48px/.test(kloop) && /<img alt="" src="assets\/deepdive_icon\.png"/.test(kloop) && !/opacity:\{\{ k\.op \}\}[^"]*"><svg|<a[^>]*opacity/.test(kloop));
ok("dimming a card never dims its deep-dive link", (() => { const o = mk({ ev: 0 }).renderVals(); return o.kpis[1].op === 0.35 && !/<a [^>]*opacity/.test(kloop); })());
ok("five deep-dive links, each labelled with its card", o0.kpis.length === 5 && o0.kpis.every(k => k.dlabel.startsWith("Deep dive: " + k.label)));
ok("unlinked cards: href is # and the label says so", o0.kpis.every(k => k.link === "#" && /link not set yet/.test(k.dlabel)));
ok("clicking an unlinked deep dive shows a notice and does not navigate", (() => { const c = mk(); let prevented = false; c.renderVals().kpis[1].dive({ preventDefault: () => { prevented = true; } }); const o = c.renderVals(); const shown = o.hasNote && /Deaths/.test(o.noteText); o.closeNote(); return prevented && shown && !c.renderVals().hasNote; })());
ok("the notice is part of the page, between the cards and the grid", /\{\{ hasNote \}\}[\s\S]*\{\{ noteText \}\}[\s\S]*\{\{ closeNote \}\}/.test(markup) && markup.indexOf("{{ hasNote }}") < markup.indexOf('repeat(7, minmax(150px'));
// configured addresses: build a test copy of the script with two cards linked
const Linked = new Function("DCLogic", script.replace("'Births': ''", "'Births': 'https://example.org/births'").replace("'Deaths': ''", "'Deaths': 'https://example.org/dive?x=1'") + "\nreturn Component;")(DCLogic);
const mkL = (st) => { const c = new Linked({}); Object.assign(c.state, st || {}); return c; };
const L0 = mkL().renderVals().kpis, L1 = mkL({ district: "Mayuge", fuel: 0 }).renderVals().kpis, L2 = mkL({ age: 2, sex: "F", period: "Last 3 months", from: "2026-02-01" }).renderVals().kpis;
ok("linked card: address + the card name, no stray separators when nothing is filtered", L0[0].link === "https://example.org/births?card=Births", L0[0].link);
ok("linked card carries the current filters to the other dashboard", L1[0].link === "https://example.org/births?district=Mayuge&fuel=Firewood&card=Births", L1[0].link);
ok("an address that already has a query keeps it", L1[1].link === "https://example.org/dive?x=1&district=Mayuge&fuel=Firewood&card=Deaths", L1[1].link);
ok("age, sex, period and dates are passed too", L2[0].link === "https://example.org/births?period=Last%203%20months&from=2026-02-01&age=25-44&sex=Female&card=Births", L2[0].link);
ok("a linked deep dive navigates normally (no notice, default not prevented)", (() => { const c = mkL(); let prevented = false; c.renderVals().kpis[0].dive({ preventDefault: () => { prevented = true; } }); return !prevented && !c.renderVals().hasNote && /\(link not set yet\)/.test(c.renderVals().kpis[1].dlabel) === false; })());
ok("unlinked cards in the same page still say so", L0[2].link === "#" && /link not set yet/.test(L0[2].dlabel));
ok("clear all also dismisses the notice", (() => { const c = mk(); c.renderVals().kpis[0].dive({ preventDefault() {} }); c.renderVals().clearAll(); return !c.renderVals().hasNote; })());
let d2 = 0, bad2 = 0; for (const m of markup.matchAll(/<button\b|<\/button>|<a\b|<\/a>/g)) { if (m[0] === "</button>" || m[0] === "</a>") d2--; else { d2++; if (d2 > 1) bad2++; } }
ok("no link or button sits inside another link or button", bad2 === 0 && d2 === 0, "nested " + bad2);
console.log(fails ? "\nFAILED: " + fails : "\nALL PASSED"); process.exit(fails ? 1 : 0);
