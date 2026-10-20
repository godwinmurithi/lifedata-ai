// Acceptance tests for the master dashboard's shared filter model. Run: node acceptance_tests.js   (Node 18+)
// They load the reference design and sample data next to this file, call every click handler, and compare the results
// with an independent calculation from the raw data. Port them to the live template as the oracle.
const fs = require("fs");
const html = fs.readFileSync(require("path").join(__dirname, "design_master_dashboard.dc.html"), "utf8");
const script = html.split('data-dc-script data-props=\'')[1].split("'>")[1].split("</script>")[0];
const markup = html.split("<x-dc>")[1].split("</x-dc>")[0];
const R = JSON.parse(fs.readFileSync(require("path").join(__dirname, "sample_data.json"), "utf8"));
class DCLogic { constructor(p) { this.props = p || {}; this.state = {}; } setState(p) { this.state = Object.assign({}, this.state, typeof p === "function" ? p(this.state) : p); } }
const Component = new Function("DCLogic", script + "\nreturn Component;")(DCLogic);
let fails = 0; const ok = (n, c, d = "") => { console.log((c ? "PASS " : "FAIL ") + n + (d !== "" ? "  [" + d + "]" : "")); if (!c) fails++; };
const mk = (st) => { const c = new Component({}); Object.assign(c.state, st || {}); return c; };
const num = (s) => Number(String(s).replace(/,/g, ""));
const sum = (a) => a.reduce((x, y) => x + y, 0);
const o0 = mk().renderVals();

// -------------------------------------------------- independent raw-data helpers (written differently from the component on purpose)
const V = R.villages, HHr = R.hh, PERr = R.per, EVr = R.ev, SIT = R.sites, SIG = R.signals, CL = R.clusters;
const vOf = {}; V.forEach(v => vOf[v.id] = v);
const sitesIn = (ids) => SIT.filter(s => ids.has(s.vid));
const allVill = new Set(V.map(v => v.id));
function expect(f) {   // f: {evT, hous, wat, net, age, sex, syn, path, status, sev, tier}
  let ids = new Set(V.filter(v => !f.tier || v.tier === f.tier).map(v => v.id));
  let hhOk = HHr.map((h, i) => (f.hous === undefined || h[1] === f.hous) && (f.wat === undefined || h[2] === f.wat) && (f.net === undefined || h[3] === f.net));
  let perOk = PERr.map(p => (f.age === undefined || p[2] === f.age) && (f.sex === undefined || p[3] === (f.sex === "M" ? 0 : 1)) && hhOk[p[1]]);
  const hhHasP = new Set(PERr.filter((p, i) => perOk[i]).map(p => p[1]));
  const needP = f.age !== undefined || f.sex !== undefined;
  const hhSel = HHr.map((h, i) => hhOk[i] && (!needP || hhHasP.has(i)));
  const narrowBy = (s) => { ids = new Set([...ids].filter(i => s.has(i))); };
  if (f.hous !== undefined || f.wat !== undefined || f.net !== undefined || f.age !== undefined || f.sex !== undefined) narrowBy(new Set(HHr.filter((h, i) => hhSel[i]).map(h => h[0])));
  if (f.evT !== undefined) narrowBy(new Set(EVr.filter(e => e[2] === f.evT).map(e => e[0])));
  const siteOk = (s) => (f.status === undefined || s.st === f.status) && (f.syn === undefined || sum(s.syn[f.syn]) > 0) && (f.path === undefined || s.lab[f.path] > 0) && (f.sev === undefined || SIG.some(g => SIT[g.s] === s && g.sev === f.sev));
  if (f.status !== undefined || f.syn !== undefined || f.path !== undefined || f.sev !== undefined) narrowBy(new Set(SIT.filter(siteOk).map(s => s.vid)));
  const sites = SIT.filter(s => ids.has(s.vid) && siteOk(s));
  return { ids, res: PERr.filter((p, i) => ids.has(p[0]) && perOk[i]).length, hh: HHr.filter((h, i) => ids.has(h[0]) && hhSel[i]).length,
    births: EVr.filter(e => ids.has(e[0]) && e[2] === 0).length, deaths: EVr.filter(e => ids.has(e[0]) && e[2] === 1).length, sites: sites.length,
    clusters: CL.filter(c => ids.has(c.id)).length, villages: ids.size, sig: SIG.filter(g => sites.includes(SIT[g.s]) && (f.sev === undefined || g.sev === f.sev) && (f.syn === undefined || g.si === f.syn)).length };
}
const STATE = { evT: "evT", hous: "hous", wat: "wat", net: "net", age: "age", sex: "sex", syn: "syn", path: "path", status: "status", sev: "sev", tier: "tier" };
const toState = (f) => { const s = {}; for (const k in f) s[STATE[k]] = f[k]; return s; };
function compare(name, f) {
  const o = mk(toState(f)).renderVals(), e = expect(f);
  const got = [num(o.kRes), num(o.kHh), num(o.kBirths), num(o.kDeaths), num(o.sKSites), num(o.gKClusters), o.gPins.length, num(o.sKSignals)].join();
  const want = [e.res, e.hh, e.births, e.deaths, e.sites, e.clusters, e.villages, e.sig].join();
  ok(name, got === want, "residents,households,births,deaths,sites,clusters,villages,signals = " + got + (got === want ? "" : "  expected " + want));
}

console.log("--- 1. defaults match every screenshot number");
ok("overview KPIs", [o0.kRes, o0.kHh, o0.kU5, o0.kBirths, o0.kDeaths, o0.kNet].join() === "180,55,5,34,11,4");
ok("stat cards", [o0.sEver, o0.sPreg, o0.sU5, o0.sOpd].join(" | ") === "1,146 | 27 | 381 | 142");
ok("event mix", o0.mixLegend.map(g => g.t).join() === "Births (34),In-migration (18),Out-migration (14),Deaths (11),Pregnancies (27)");
ok("pyramid", o0.pyRows.map(r => r.m + "/" + r.f).join() === "23/11,18/12,8/9,11/7,13/12,12/9,15/15,4/1");
ok("housing / water / bednet", [o0.housRows, o0.watRows, o0.netRows].map(a => a.map(r => r.n).join("-")).join(" | ") === "13-21-16-5 | 29-23-3 | 36-19");
ok("sentinel KPIs", [o0.sKSites, o0.sKReports, o0.sKSignals, o0.sKRrt, o0.sKLab, o0.sKLag].join() === "24,312,7,3,18,1.4d");
ok("pathogens", o0.pathLegend.map(g => g.t).join() === "Malaria (9),Respiratory (5),Diarrheal (3),Measles (1)");
ok("geo KPIs", [o0.gKClusters, o0.gKHigh, o0.gKPop, o0.gKMean, o0.gKRad, o0.gKP].join() === "9,14,4,200,62%,4.2km,0.03");
ok("tier distribution and hotspot table", o0.tierItems.map(t => t.label).join() === "High (14),Moderate (22),Low (31)" && o0.gRows.map(r => r.name + r.cases + r.score).join() === "Nampiima3884,Kasakya3179,Lugazi Central2773,Buwama1958,Kitosi1652,Butowali B1449");

console.log("--- 2. every chart element is wired: clicking it changes the shared state, and clicking again clears it");
function wired(name, getHandler, key, want, none) {
  const c = mk(); const h = getHandler(c.renderVals());
  if (typeof h !== "function") { ok(name, false, "no handler"); return; }
  h(); const after = c.state[key]; const set = JSON.stringify(after) === JSON.stringify(want);
  c.renderVals().chips; const h2 = getHandler(c.renderVals()); h2(); const cleared = JSON.stringify(c.state[key]) === JSON.stringify(none);
  ok(name, set && cleared, key + " -> " + JSON.stringify(after) + " -> " + JSON.stringify(c.state[key]));
}
wired("KPI tile: Births", o => ({ _: 0 }) && (() => o.pickBirths()), "evT", 0, -1);
wired("KPI tile: Deaths", o => () => o.pickDeaths(), "evT", 1, -1);
wired("KPI tile: Under five", o => () => o.pickU5(), "age", 7, -1);
wired("stat card: Currently pregnant", o => () => o.pickPreg(), "evT", 4, -1);
wired("KPI tile: High-risk villages", o => () => o.pickHigh(), "tier", "High", "All");
for (let t = 0; t < 5; t++) {
  wired("event legend " + t, o => o.vLegend[t].pick, "evT", t, -1);
  wired("event mix legend " + t, o => o.mixLegend.find(g => g.t.startsWith(["Births", "Deaths", "In-migration", "Out-migration", "Pregnancies"][t])).pick, "evT", t, -1);
}
for (let t = 0; t < 5; t++) ok("event mix slice " + t + " is clickable and dims the others", (() => { const c = mk({ evT: t }); const o = c.renderVals(); return o.mixSegs.length > 0 && o.mixSegs.filter(s => s.op === 1).length === 1 && typeof o.mixSegs[0].pick === "function"; })());
wired("month label Aug", o => o.vLabels[4].pick, "tIdx", 4, -1);
ok("month point sets event type AND month", (() => { const c = mk(); c.renderVals().vDots[7].pick(); return c.state.evT === 1 && c.state.tIdx === 0; })(), "dot #7 = series 1, month 0");
for (let a = 0; a < 8; a++) { wired("pyramid men " + a, o => o.pyRows[a].pm, "age", a, -1); }
ok("pyramid: men bar sets age+sex, age label sets age only, women bar sets age+sex", (() => {
  const c = mk(); c.renderVals().pyRows[3].pm(); const a = [c.state.age, c.state.sex]; c.renderVals().pyRows[3].pa(); const b = [c.state.age, c.state.sex]; c.renderVals().pyRows[3].pf(); const d = [c.state.age, c.state.sex]; return a.join() === "3,M" && b.join() === "3," && d.join() === "3,F"; })());
for (let i = 0; i < 4; i++) wired("housing bar " + i, o => o.housRows[i].pick, "hous", i, -1);
for (let i = 0; i < 3; i++) wired("water bar " + i, o => o.watRows[i].pick, "wat", i, -1);
for (let i = 0; i < 2; i++) wired("bednet bar " + i, o => o.netRows[i].pick, "net", i, -1);
ok("overview map pin sets the whole village path", (() => { const c = mk(); c.renderVals().ovPins[0].pick(); return c.state.village !== "All" && c.state.district !== "All" && c.state.country === "Uganda"; })());
for (let i = 0; i < 4; i++) wired("syndrome legend " + i, o => o.wLegend[i].pick, "syn", [0, 2, 1, 3][i], -1);
ok("weekly point sets syndrome AND week", (() => { const c = mk(); c.renderVals().wDots[9].pick(); return c.state.syn === 1 && c.state.tIdx === 2; })(), "dot #9 = syndrome 1, week 2");
wired("week label W19", o => o.wLabels[4].pick, "tIdx", 4, -1);
for (const [i, st] of [[0, "alert"], [1, "watch"], [2, "normal"]]) wired("site status legend " + st, o => o.statusItems[i].pick, "status", st, "");
ok("sentinel map pin sets the village path", (() => { const c = mk(); c.renderVals().sPins[0].pick(); return c.state.village !== "All"; })());
for (let k = 0; k < 4; k++) { wired("pathogen legend " + k, o => o.pathLegend[k].pick, "path", k, -1); }
ok("pathogen slices are clickable and dim the others", (() => { const o = mk({ path: 0 }).renderVals(); return o.pathSegs.filter(s => s.op === 1).length === 1 && o.pathSegs.every(s => typeof s.pick === "function"); })());
ok("signal row: facility sets village, syndrome sets syndrome, severity sets severity", (() => {
  const c = mk(); const r = c.renderVals().sigRows[0]; r.pick(); const a = c.state.village !== "All"; r.synPick(); const b = c.state.syn === 0; r.sevPick(); const d = c.state.sev === "High"; return a && b && d; })());
for (const t of ["High", "Moderate", "Low"]) {
  wired("tier legend (donut) " + t, o => o.tierItems.find(x => x.label.startsWith(t)).pick, "tier", t, "All");
  wired("tier legend (map) " + t, o => o.tierPlain.find(x => x.label === t).pick, "tier", t, "All");
  ok("tier slice " + t, (() => { const o = mk({ tier: t }).renderVals(); return o.tierSegs.filter(s => s.op === 1).length === 1; })());
}
ok("geo map pin and scatter point set the village path", (() => { const c = mk(); c.renderVals().gPins[5].pick(); const a = c.state.village !== "All"; const d = mk(); d.renderVals().gPts[0].pick(); return a && d.state.village === "Nampiima"; })());
ok("hotspot row: village name sets village, tier pill sets tier", (() => { const c = mk(); const r = c.renderVals().gRows[3]; r.pick(); const a = c.state.village === "Buwama"; r.tierPick(); return a && c.state.tier === "Moderate"; })());
ok("clear all resets every dimension", (() => { const c = mk({ evT: 1, age: 3, sex: "M", hous: 2, wat: 1, net: 0, syn: 2, path: 1, status: "alert", sev: "High", tier: "High", tIdx: 2, district: "Buikwe" }); c.renderVals().clearAll(); return JSON.stringify(c.state) === JSON.stringify(Object.assign({}, mk().state, {})); })());

console.log("--- 3. one selection filters all three tabs: numbers match an independent calculation from the raw data");
compare("no filter", {});
for (let t = 0; t < 5; t++) compare("event type " + ["Births", "Deaths", "In-migration", "Out-migration", "Pregnancies"][t], { evT: t });
for (let a = 0; a < 8; a++) compare("age band " + ["65+", "55-64", "45-54", "35-44", "25-34", "15-24", "5-14", "0-4"][a], { age: a });
compare("men aged 25-34", { age: 4, sex: "M" }); compare("women only", { sex: "F" });
for (let i = 0; i < 4; i++) compare("housing " + ["Good", "Moderate", "Poor", "Dilapidated"][i], { hous: i });
for (let i = 0; i < 3; i++) compare("water " + ["Safe", "Improved toilet", "Open defecation"][i], { wat: i });
for (let i = 0; i < 2; i++) compare("bednet " + ["With", "Without"][i], { net: i });
for (let i = 0; i < 4; i++) compare("syndrome " + ["Fever", "Respiratory", "Diarrhea", "Rash"][i], { syn: i });
for (let k = 0; k < 4; k++) compare("pathogen " + ["Malaria", "Respiratory", "Diarrheal", "Measles"][k], { path: k });
for (const st of ["alert", "watch", "normal"]) compare("site status " + st, { status: st });
for (const sv of ["High", "Moderate", "Low"]) compare("signal severity " + sv, { sev: sv });
for (const t of ["High", "Moderate", "Low"]) compare("tier " + t, { tier: t });
compare("combined: poor housing + under-fives", { hous: 2, age: 7 }); compare("combined: with bednet + births villages", { net: 0, evT: 0 });
compare("combined: syndrome Fever + status alert", { syn: 0, status: "alert" }); compare("combined: tier High + deaths", { tier: "High", evT: 1 });

console.log("--- 4. a chart that is itself a filter keeps showing the alternatives; the others follow");
const poor = mk({ hous: 2 }).renderVals();
ok("housing chart still lists all four categories with the unfiltered counts", poor.housRows.map(r => r.n).join("-") === "13-21-16-5" && poor.housRows[2].op === 1 && poor.housRows[0].op === 0.3);
ok("water chart follows the housing selection (only Poor-housing households)", poor.watRows.reduce((a, r) => a + r.n, 0) === 16 && poor.netRows.reduce((a, r) => a + r.n, 0) === 16, "water total " + poor.watRows.reduce((a, r) => a + r.n, 0));
const u5 = mk({ age: 7 }).renderVals();
ok("pyramid keeps every age band while one is selected; residents shows only that band", u5.pyRows.map(r => r.m + "/" + r.f).join() === "23/11,18/12,8/9,11/7,13/12,12/9,15/15,4/1" && u5.kRes === "5" && u5.pyRows[7].ao === 1 && u5.pyRows[0].ao === 0.3);
const fe = mk({ syn: 0 }).renderVals();
ok("syndrome chart keeps all four series while Fever is selected, and dims the others", fe.wLines.length === 4 && fe.wLines.filter(l => l.op === 1).length === 1);
ok("signals table follows the syndrome filter", fe.sigRows.every(r => r.syn.toLowerCase().includes("fever") || r.syn === "Febrile illness cluster") && fe.sigRows.length === 3, fe.sigRows.map(r => r.syn).join(", "));
const hv = mk({ sev: "High" }).renderVals();
ok("severity filter narrows the signals table and the sites", hv.sigRows.length === 2 && hv.sKSites === "2" && hv.sKSignals === "2");
const tix = mk({ tIdx: 2 }).renderVals();
ok("a selected month filters event KPIs but the line chart keeps all months", tix.kBirths === "6" && tix.vLabels.length === 7 && tix.vLabels[2].pr === true && tix.vDots.filter(d => d.r === 6).length === 5, "births in Jun = " + tix.kBirths);
const per = mk({ period: "Last 3 months" }); const pr0 = per.renderVals(); pr0.vLabels[1].pick();
ok("changing the period clears a selected time point", (() => { per.renderVals().onPeriod({ target: { value: "All" } }); return per.state.tIdx === -1; })());
ok("selected time point is bold in the chart labels", mk({ tIdx: 3 }).renderVals().vLabels[3].w === 700);

console.log("--- 5. consistency and safety");
ok("contradictory selections show the empty message instead of a blank page", (() => { const o = mk({ hous: 3, wat: 2, net: 0, age: 0, sex: "F" }).renderVals(); return o.isEmpty === true && !o.showGeo && !o.showOverview && !o.showSentinel; })());
ok("every active dimension has a removable chip", (() => { const o = mk({ evT: 1, age: 3, sex: "M", hous: 2, wat: 1, net: 0, syn: 2, path: 1, status: "alert", sev: "High", tier: "High", tIdx: 2, period: "Last 6 months", district: "Buikwe" }).renderVals(); return o.chips.length === 14 && o.chips.every(c => typeof c.clear === "function"); })());
ok("removing a chip removes only that filter", (() => { const c = mk({ evT: 1, age: 3 }); const o = c.renderVals(); o.chips.find(x => x.label.startsWith("Event")).clear(); return c.state.evT === -1 && c.state.age === 3; })());
ok("removing a geography chip clears the levels below it", (() => { const c = mk({ district: "Buikwe", subcounty: "Wakisi", village: "Buwama" }); c.renderVals().chips.find(x => x.label.startsWith("Subcounty")).clear(); return c.state.subcounty === "All" && c.state.village === "All" && c.state.district === "Buikwe"; })());
ok("dropdown options cascade", mk({ district: "Mayuge" }).renderVals().optSubcounty.length === 1 + 8, mk({ district: "Mayuge" }).renderVals().optSubcounty.length + " options");
const holes = [...markup.matchAll(/\{\{\s*([A-Za-z0-9_.$]+)\s*\}\}/g)].map(m => m[1]);
const loopVars = {}; [...markup.matchAll(/<sc-for list="\{\{\s*(\w+)\s*\}\}" as="(\w+)"/g)].forEach(m => { loopVars[m[2]] = m[1]; });
const missing = [...new Set(holes)].filter(h => { const [root, f] = h.split("."); if (["true", "false"].includes(root)) return false; if (loopVars[root] && f) return false; return !(root in o0) && !loopVars[root]; });
ok("every top-level placeholder has a value", missing.length === 0, missing.join(", "));
const withChips = mk({ district: "Buikwe" }).renderVals();
const bad = []; for (const [, list, as_, inner] of [...markup.matchAll(/<sc-for list="\{\{\s*(\w+)\s*\}\}" as="(\w+)"[^>]*>([\s\S]*?)<\/sc-for>/g)]) {
  const arr = list === "chips" ? withChips.chips : o0[list]; if (!Array.isArray(arr) || !arr.length) { bad.push(list + " (empty or not a list)"); continue; }
  for (const f of new Set([...inner.matchAll(new RegExp("\\{\\{\\s*" + as_ + "\\.(\\w+)\\s*\\}\\}", "g"))].map(m => m[1]))) if (!(f in arr[0])) bad.push(list + "." + f);
}
ok("every loop field exists on its items (" + (markup.match(/<sc-for /g) || []).length + " loops)", bad.length === 0, bad.join(", "));
ok("loops and conditionals are balanced", (markup.match(/<sc-for /g) || []).length === (markup.match(/<\/sc-for>/g) || []).length && (markup.match(/<sc-if /g) || []).length === (markup.match(/<\/sc-if>/g) || []).length);
ok("no text placeholder inside an SVG <text> or an <option>", !/<text[^>]*>[^<]*\{\{/.test(markup) && !/<option[^>]*>[^<]*\{\{/.test(markup));
ok("every <option> carries its label as an attribute", (markup.match(/<option [^>]*label="\{\{ o\.v \}\}"><\/option>/g) || []).length === (markup.match(/<option /g) || []).length);
let depth = 0, nested = 0; for (const m of markup.matchAll(/<button\b|<\/button>/g)) { if (m[0] === "</button>") depth--; else { depth++; if (depth > 1) nested++; } }
ok("no button sits inside another button", nested === 0 && depth === 0, "nested " + nested);
ok("every clickable list item has a handler", ["vLegend", "mixLegend", "pyRows", "housRows", "watRows", "netRows", "wLegend", "statusItems", "pathLegend", "tierItems", "tierPlain", "sigRows", "gRows", "vDots", "wDots", "ovPins", "sPins", "gPins", "gPts", "mixSegs", "pathSegs", "tierSegs", "vLabels", "wLabels"].every(k => o0[k].every(it => ["pick", "pm"].some(h => typeof it[h] === "function"))));
console.log(fails ? "\nFAILED: " + fails : "\nALL PASSED");
process.exit(fails ? 1 : 0);
