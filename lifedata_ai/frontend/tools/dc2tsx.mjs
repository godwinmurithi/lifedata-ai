// Port the approved Master design's markup to React, mechanically, so the page IS the design.
//   node tools/dc2tsx.mjs ../../docs/design/master/design_master_dashboard.dc.html master-view.tsx
// The design is a Design Component page: HTML with {{ placeholders }}, <sc-for list as> loops and <sc-if value>.
// Every inline style, element and aria attribute is carried over; placeholders read the view model `o`
// (master-model.ts renderVals, which uses the design's own field names). The only edits (TRANSFORMS below):
//   1. geography selects come from the page's registered levels (demo: Country..Village; real: Country, Site);
//   2. Update cycle + Time period replace the design's Time period + inert "Time range"/"Apply" (decided 2026-10-20);
//   3. the three "[Basemap tiles load here]" boxes become Leaflet maps (OpenStreetMap) with the same pins;
//   4. cards that may have no registered source are wrapped in <x-slot> (shows the registered reason instead);
//   5. an "Illustrative data, not real surveillance" line and the scope help text sit above every tab;
//   6. the design's painted "Ask Vima" pill is removed (the site's real panel provides it).
import fs from "node:fs";
import path from "node:path";

const [, , src, out] = process.argv;
const html = fs.readFileSync(src, "utf8");
let m = html.split("<x-dc>")[1].split("</x-dc>")[0].replace(/<helmet>[\s\S]*?<\/helmet>/, "");

const must = (i, what) => { if (i < 0) throw new Error("design anchor not found: " + what); return i; };
function blockEnd(s, start, tag) { // index just after the element starting at `start`, counting nested same tags
  const re = new RegExp(`<${tag}\\b|</${tag}>`, "g"); re.lastIndex = start; let depth = 0, mt;
  while ((mt = re.exec(s))) { if (mt[0][1] === "/") { if (--depth === 0) return mt.index + mt[0].length; } else depth++; }
  throw new Error("unclosed " + tag);
}
function enclosing(s, needle, tag) { // the <tag> element that contains `needle`
  const at = must(s.indexOf(needle), needle); const start = s.lastIndexOf(`<${tag} `, at);
  return [start, blockEnd(s, start, tag)];
}

// ---- 1 + 2: filter bar
const LBL = '<label style="display:flex; align-items:center; gap:10px; font-size:15px; color:#CFCFCF">';
const country = must(m.indexOf(LBL + "Country"), "Country select");
const countryEnd = blockEnd(m, country, "label");
const selectTpl = m.slice(country, countryEnd);
const select = (label, width, value, options, onChange) => selectTpl.replace(">Country<", `>${label}<`).replace("width:150px", `width:${width}`)
  .replace("{{ fCountry }}", value).replace("{{ optCountry }}", options).replace("{{ onCountry }}", onChange);
const row1End = must(m.indexOf("</div>", m.indexOf(LBL + "Parish")), "end of first filter row");
m = m.slice(0, country) + `<sc-for list="{{ geoSelects }}" as="g">${select("{{ g.label }}", "{{ g.width }}", "{{ g.value }}", "{{ g.options }}", "{{ g.onChange }}")}</sc-for>` + m.slice(row1End);
const village = must(m.indexOf(LBL + "Village"), "Village select");
const clear = must(m.indexOf('<button type="button" onClick="{{ clearAll }}"', village), "Clear All");
m = m.slice(0, village)
  + `<sc-if value="{{ showCycle }}">${select("Update cycle", "230px", "{{ fCycle }}", "{{ optCycle }}", "{{ onCycle }}")}</sc-if>`
  + `<sc-if value="{{ showPeriod }}">${select("Time period", "190px", "{{ fRange }}", "{{ optRange }}", "{{ onRange }}")}</sc-if>`
  + m.slice(clear);

// ---- 3: maps
for (const [aria, list] of [["Map of villages with residents", "ovPins"], ["Map of sentinel sites", "sPins"], ["Map of villages sized by cases", "gPins"]]) {
  const start = must(m.indexOf(`<div role="group" aria-label="${aria}`), aria), end = blockEnd(m, start, "div");
  const open = m.slice(start, m.indexOf(">", start) + 1);
  const label = open.match(/aria-label="([^"]*)"/)[1], style = open.match(/style="([^"]*)"/)[1];
  m = m.slice(0, start) + `<x-map pins="{{ ${list} }}" label="${label}" style="${style}"></x-map>` + m.slice(end);
}

// ---- 4: cards that may be unbound
const wrap = (needle, tag, slot) => { const [a, b] = enclosing(m, needle, tag); m = m.slice(0, a) + `<x-slot name="${slot}">` + m.slice(a, b) + "</x-slot>" + m.slice(b); };
wrap(">Vital events by month</h2>", "section", "chart.vital_events_by_month");
wrap(">Event mix</h2>", "section", "chart.event_mix");
wrap(">Where events are happening</h2>", "section", "map.events");
wrap(">Ever registered</div>", "section", "stat.ever_registered");
wrap(">Currently pregnant</div>", "button", "stat.currently_pregnant");
wrap(">Under-five</div>", "section", "stat.under_five_ever");
wrap(">Outpatient visits</div>", "section", "stat.outpatient_visits");
wrap(">Age and gender</h2>", "section", "chart.age_gender");
wrap(">Housing quality</h2>", "section", "chart.housing");
wrap(">Water &amp; sanitation</h2>", "section", "chart.water_sanitation");
wrap(">Bednet use</h2>", "section", "chart.bednet");

// ---- 5: village-scope help above every tab, shown only with a person or household filter
// (the "Illustrative data" line was removed at the user's request, 2026-10-20)
const content = must(m.indexOf('<sc-if value="{{ isEmpty }}"'), "content area");
m = m.slice(0, content)
  + '<sc-if value="{{ scopeHelp }}"><div class="mf-help" style="font-family:\'Inter\',sans-serif; font-size:13px; color:#66615A">{{ scopeHelpText }}</div></sc-if>'
  + m.slice(content);

// ---- 6: drop the painted Ask Vima pill (last absolutely positioned block)
const pill = m.lastIndexOf('<div style="position:absolute; right:22px; bottom:22px;');
if (pill > 0) m = m.slice(0, pill) + m.slice(blockEnd(m, pill, "div"));

// the design names its <option> loop variable `o`, which would shadow the view model `o`
m = m.split('as="o"').join('as="opt"').split("{{ o.v }}").join("{{ opt.v }}");
// the server switches to yearly points for timelines longer than 36 months; the title says which
if (m.split(">Vital events by month</h2>").length !== 2) throw new Error("vital events title not found");
m = m.replace(">Vital events by month</h2>", ">{{ vTitle }}</h2>");
// trend points show their value on hover (series, time point and count)
{
  const dot = 'onClick="{{ d.pick }}" style="cursor:pointer"></circle>';
  if (m.split(dot).length !== 3) throw new Error("expected the two trend-point loops (vDots, wDots)");
  m = m.split(dot).join('onClick="{{ d.pick }}" style="cursor:pointer"><title>{{ d.tip }}</title></circle>');
}
// options also carry their label as text: some browsers and screen readers ignore the label attribute
m = m.replace(/(<option [^>]*label="\{\{ opt\.v \}\}"[^>]*>)<\/option>/g, "$1{{ opt.v }}</option>");

// ================================================================ 7: fit one window
// Every tab fits the browser window (user request, 2026-10-20). The design's markup stays; its layout
// containers get class names (below) and master-parts.tsx MASTER_FIT_CSS sizes them: rows share the
// window height, cards shrink, charts scale. Each anchor is a style fragment of the design; the
// count is checked so a changed design fails here instead of silently losing the fit.
function addClass(needle, cls, count) {
  let n = 0;
  m = m.replace(/<([a-zA-Z][\w-]*)\b([^>]*)>/g, (all, tag, rest) => {
    const st = (rest.match(/style="([^"]*)"/) || [])[1];
    if (st === undefined || !st.includes(needle)) return all;
    n++;
    return rest.includes('class="') ? all.replace('class="', `class="${cls} `) : `<${tag} class="${cls}"${rest}>`;
  });
  if (count !== undefined && n !== count) throw new Error(`fit anchor "${needle}" matched ${n} elements, expected ${count}`);
  if (!n) throw new Error(`fit anchor "${needle}" not found`);
}
const swap = (a, b, count) => { const n = m.split(a).length - 1; if (n !== count) throw new Error(`fit swap "${a}" matched ${n}, expected ${count}`); m = m.split(a).join(b); };
addClass("position:relative; background:#0E0F11; max-width:1440px", "mf-root", 1);
addClass("background:#F5F2EC; min-height:1270px", "mf-page", 1);
addClass("background:#1B1B1C; display:flex; gap:14px", "mf-tabs", 1);
addClass("padding:18px 20px 14px", "mf-tab", 1);
addClass("background:#000000; padding:16px 20px 18px", "mf-filters", 1);
addClass("display:flex; align-items:center; gap:22px; flex-wrap:wrap", "mf-frow", 2);
addClass("display:flex; align-items:center; gap:10px; font-size:15px; color:#CFCFCF", "mf-flabel");
addClass("height:38px; font-size:15px", "mf-select");
addClass("height:42px; padding:0 6px", "mf-clear", 1);
addClass("padding:12px 14px 0; align-items:center", "mf-chips", 1);
addClass("padding:14px 14px 26px; display:flex; flex-direction:column; gap:12px", "mf-body", 1);
addClass("grid-template-columns:repeat(auto-fit, minmax(190px, 1fr)); gap:12px", "mf-kpis", 3);
addClass("border-radius:18px; padding:14px 18px; box-sizing:border-box; display:flex; align-items:center; gap:14px", "mf-kpi");
addClass("border-radius:18px; padding:13px 17px; box-sizing:border-box; display:flex; align-items:c", "mf-kpi");
addClass("width:52px; height:52px; border-radius:14px", "mf-ico");
addClass("font-size:30px; line-height:1.1; font-weight:700", "mf-kval");
addClass("display:flex; flex-wrap:wrap; gap:12px; align-items:stretch", "mf-row", 4);
addClass("flex:1 1 calc((100% - 36px) / 4 - 1px); min-width:min(100%, 240px); display:flex; flex-direction:column; gap:12px", "mf-col", 2);
addClass("border-radius:18px; padding:18px 20px; box-sizing:border-box", "mf-card");
addClass("display:flex; align-items:center; justify-content:space-between; margin-bottom:12px", "mf-head");
addClass("margin:0; font-size:17px; font-weight:600", "mf-h2");
addClass("padding:16px 20px; box-sizing:border-box; flex:1 1 0; display:flex; align-items:center; justify-content:space-between; min-height:130px", "mf-stat", 3);
addClass("padding:15px 19px; box-sizing:border-box; flex:1 1 0; display:flex; align-items:center; justify-content:space-between; min-height:130px", "mf-stat", 1);
addClass("width:58px; height:58px; border-radius:16px", "mf-sico", 4);
addClass("font-size:38px; line-height:1.15; font-weight:700", "mf-sval", 4);
addClass("display:flex; flex-wrap:wrap; gap:6px 10px; margin-top:auto; padding-top:14px", "mf-legend");
addClass("display:flex; flex-wrap:wrap; gap:8px 12px; margin-top:auto; padding-top:14px", "mf-legend");
addClass("display:grid; grid-template-columns:1fr 1fr; gap:4px 10px; margin-top:10px", "mf-legend", 1);
addClass("overflow-x:auto", "mf-tablewrap", 2);
// the two tables are full-width rows of their own
for (const title of [">Recent surveillance signals</h2>", ">Top hotspot villages</h2>"]) {
  const [a] = enclosing(m, title, "section"), open = m.slice(a, m.indexOf(">", a) + 1);
  m = m.slice(0, a) + open.replace(/^<section( class="([^"]*)")?/, (_, __, c) => `<section class="mf-tablecard${c ? " " + c : ""}"`) + m.slice(a + open.length);
}
// age and gender: rows span the card; bars are a share of their half instead of fixed pixels
addClass("display:flex; flex-direction:column; align-items:center", "mf-pyr", 1);
addClass("display:flex; align-items:center; height:35px; gap:8px", "mf-pyrow", 1);
swap("width:100px; display:flex; justify-content:flex-end;", "flex:1 1 0; min-width:0; display:flex; justify-content:flex-end;", 1);
swap("width:100px; display:flex; background:none;", "flex:1 1 0; min-width:0; display:flex; background:none;", 1);
swap("width:{{ r.mw }}px;", "width:{{ r.mw }}%;", 1);
swap("width:{{ r.fw }}px;", "width:{{ r.fw }}%;", 1);
// bar lists (housing, water, bednet): rows share the card height
addClass("display:flex; align-items:center; height:50px; width:100%", "mf-bar", 3);
addClass("height:38px; background:", "mf-barfill", 3);
// donuts scale with their card
addClass("display:block; margin:0 auto", "mf-donut", 1);
addClass("display:block; margin:6px auto 0", "mf-donut", 1);
addClass("display:block; margin:10px auto 0", "mf-donut", 1);
// scatter: both 260px columns follow the card height
addClass("display:flex; gap:8px; margin-top:8px", "mf-scatter", 1);
swap("justify-content:space-between; height:260px;", "justify-content:space-between; height:100%;", 1);
swap("position:relative; height:260px;", "position:relative; height:100%;", 1);
// line charts: the chart and its axis labels keep one aspect ratio together, so labels stay under their points
for (const [svgStart, ratio] of [['<svg role="img" aria-label="Births, deaths', "320 / 148"], ['<svg role="img" aria-label="Weekly reports by syndrome', "360 / 210"]]) {
  const a = must(m.indexOf(svgStart), svgStart), labels = must(m.indexOf('<div style="position:relative; height:26px">', a), "axis labels");
  const b = blockEnd(m, labels, "div");
  m = m.slice(0, a) + `<div class="mf-lbox"><div class="mf-linner" style="aspect-ratio:${ratio}">` + m.slice(a, b) + "</div></div>" + m.slice(b);
}
addClass("position:relative; height:26px", "mf-axis", 2);

// ================================================================ convert to JSX
const VOID = new Set(["img", "input", "br", "hr", "meta", "link"]);
const tokens = m.match(/<!--[\s\S]*?-->|<\/?[a-zA-Z][^>]*>|[^<]+/g);
function parse() {
  const root = { tag: "#root", attrs: [], children: [] }, stack = [root];
  for (const t of tokens) {
    if (t.startsWith("<!--")) continue;
    if (t.startsWith("</")) { const name = t.slice(2, -1).trim(); const top = stack.pop(); if (top.tag !== name) throw new Error(`mismatched </${name}> (open ${top.tag})`); continue; }
    if (t.startsWith("<")) {
      const name = t.match(/^<([a-zA-Z][\w-]*)/)[1]; const attrs = [];
      for (const a of t.slice(name.length + 1, t.endsWith("/>") ? -2 : -1).matchAll(/([^\s=]+)(?:="([^"]*)")?/g)) attrs.push([a[1], a[2] ?? ""]);
      const node = { tag: name, attrs, children: [] }; stack.at(-1).children.push(node);
      if (!VOID.has(name) && !t.endsWith("/>")) stack.push(node);
      continue;
    }
    stack.at(-1).children.push({ text: t });
  }
  if (stack.length !== 1) throw new Error("unclosed elements: " + stack.map((s) => s.tag).join(">"));
  return root;
}
const PH = /\{\{\s*([^}]+?)\s*\}\}/g;
const HAS = /\{\{/; // non-global, safe for .test()
const expr = (p, scope) => (p === "true" || p === "false" ? p : scope.includes(p.split(".")[0]) ? p : "o." + p);
const tpl = (v, scope) => "`" + v.replace(/`/g, "\\`").replace(/\$\{/g, "\\${").replace(PH, (_, p) => "${" + expr(p, scope) + "}") + "`";
const value = (v, scope) => { const only = v.match(/^\{\{\s*([^}]+?)\s*\}\}$/); return only ? "{" + expr(only[1], scope) + "}" : HAS.test(v) ? "{" + tpl(v, scope) + "}" : JSON.stringify(v); };
const camel = (s) => s.replace(/^-webkit-/, "Webkit-").replace(/-([a-z])/g, (_, c) => c.toUpperCase());
const ATTR = { class: "className", for: "htmlFor", tabindex: "tabIndex", colspan: "colSpan" };
function style(v, scope) {
  const decls = new Map(); // a property set twice keeps its last value, exactly as CSS does
  v.split(";").map((d) => d.trim()).filter(Boolean).forEach((d) => {
    const i = d.indexOf(":"); const k = camel(d.slice(0, i).trim()), raw = d.slice(i + 1).trim();
    decls.delete(k);
    decls.set(k, `${JSON.stringify(k)}: ${HAS.test(raw) ? tpl(raw, scope) : JSON.stringify(raw)}`);
  });
  return "{{ " + [...decls.values()].join(", ") + " }}";
}
function attrs(node, scope) {
  return node.attrs.filter(([k]) => !k.startsWith("hint-")).map(([k, v]) => {
    if (k === "style") return `style=${style(v, scope)}`;
    const name = ATTR[k] || (k.startsWith("aria-") || k.startsWith("data-") ? k : k.includes("-") || k.includes(":") ? camel(k.replace(":", "-")) : k);
    return `${name}=${value(v, scope)}`;
  }).join(" ");
}
const esc = (s) => s.replace(/[{}]/g, (c) => `{'${c}'}`);
function text(t, scope) { // literal text stays text (braces escaped); {{ x }} becomes {o.x}
  let out = "", last = 0;
  for (const mt of t.matchAll(PH)) { out += esc(t.slice(last, mt.index)) + "{" + expr(mt[1], scope) + "}"; last = mt.index + mt[0].length; }
  return out + esc(t.slice(last));
}
function emit(node, scope) {
  if (node.text !== undefined) return text(node.text, scope);
  const kids = (sc) => node.children.map((c) => emit(c, sc)).join("");
  const get = (k) => (node.attrs.find(([a]) => a === k) || [])[1];
  if (node.tag === "sc-for") {
    const list = get("list").match(/\{\{\s*([^}]+?)\s*\}\}/)[1], as = get("as"), sc = [...scope, as];
    return `{(${expr(list, scope)} || []).map((${as}: any, ${as}_i: number) => <React.Fragment key={${as}_i}>${kids(sc)}</React.Fragment>)}`;
  }
  if (node.tag === "sc-if") return `{(${expr(get("value").match(/\{\{\s*([^}]+?)\s*\}\}/)[1], scope)}) ? <>${kids(scope)}</> : null}`;
  if (node.tag === "x-map") return `<MasterMap className="mf-map" pins={${expr(get("pins").match(/\{\{\s*([^}]+?)\s*\}\}/)[1], scope)}} label=${JSON.stringify(get("label"))} style=${style(get("style"), scope)} />`;
  if (node.tag === "x-slot") return `{o.slot(${JSON.stringify(get("name"))}) ? <>${kids(scope)}</> : <Unbound o={o} name=${JSON.stringify(get("name"))} />}`;
  const a = attrs(node, scope);
  return VOID.has(node.tag) ? `<${node.tag}${a ? " " + a : ""} />` : `<${node.tag}${a ? " " + a : ""}>${kids(scope)}</${node.tag}>`;
}
const tree = parse();
const body = tree.children.map((c) => emit(c, [])).join("");
fs.mkdirSync(path.dirname(out), { recursive: true }); // tests/.build does not exist in a fresh checkout
fs.writeFileSync(out, `// GENERATED from docs/design/master/design_master_dashboard.dc.html by tools/dc2tsx.mjs. Do not edit by hand:
// change the design or the transforms in tools/dc2tsx.mjs and regenerate (npm run gen:master).
/* eslint-disable */
import React from "react";
import { MasterMap, Unbound } from "./master-parts";

export function MasterView({ o }: { o: any }) {
  return (<>${body}</>);
}
`);
console.log("wrote " + out + " (" + body.length + " chars)");
