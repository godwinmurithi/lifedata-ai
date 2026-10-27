import React, { createContext, useContext, useState } from "react";
import "./design-system.css";
export type ChartStyle = {color?:string;colors?:Partial<Record<"accent"|"yes"|"no"|"unknown",string>>;palette?:string[]};
export type DashboardDesign = { chart_styles?:Record<string,ChartStyle>; baseline?: "vima-buikwe-v1" | "vima-adaptive-v1"; colors?: Partial<Record<"accent" | "yes" | "no" | "unknown", string>>; palette?: string[]; groups?: {title: string; chart_ids: number[]}[] };
export const designTokens = { font: 'Inter, "Segoe UI", Arial, sans-serif', accent: "#a43b5d", yes: "#13a58a", no: "#e15b6c", unknown: "#94a3b8", palette: ["#cc00ff", "#ffff00", "#87ec36", "#34c8df", "#ff9200", "#f04bff", "#36c98a", "#7a75e8"] };
export const DesignContext = createContext<DashboardDesign>({});
export function responseColor(value: unknown, index: number, design: DashboardDesign = {}) {
  const label = String(value ?? "").trim().toLowerCase();
  if (value == null || ["", "unknown", "not recorded", "n/a"].includes(label)) return design.colors?.unknown || designTokens.unknown;
  if (["yes", "y", "true"].includes(label)) return design.colors?.yes || designTokens.yes;
  if (["no", "n", "false"].includes(label)) return design.colors?.no || designTokens.no;
  const palette = design.palette || designTokens.palette;
  return palette[index % palette.length];
}
export function ResponseDonut({values, title, select}: {values: {label: unknown; value: number}[]; title: string; select?: (label: string) => void}) {
 const design = useContext(DesignContext); const total = values.reduce((n,r)=>n+r.value,0); let offset=0;
 if (!total) return <div className="empty">No results for this chart.</div>;
 return <div className="vima-donut"><svg viewBox="0 0 240 180" role="img" aria-label={title}>
 <circle cx="120" cy="88" r="48" fill="none" stroke="#e8eef3" strokeWidth="23"/>
 {values.map((r,i)=>{const start=offset;offset+=r.value;const angle=2*Math.PI*(start+r.value/2)/total-Math.PI/2,x=120+72*Math.cos(angle),y=88+72*Math.sin(angle); const label=String(r.label??"Not recorded");return <g key={i}><circle cx="120" cy="88" r="48" fill="none" stroke={responseColor(r.label,i,design)} strokeWidth="23" pathLength="100" strokeDasharray={`${100*r.value/total} ${100-100*r.value/total}`} strokeDashoffset={-100*start/total} transform="rotate(-90 120 88)" role={select ? "button" : undefined} tabIndex={select && r.label!=null ? 0 : undefined} aria-label={`${label}: ${r.value}`} onClick={()=>r.label!=null&&select?.(label)} onKeyDown={e=>{if((e.key==="Enter"||e.key===" ")&&r.label!=null&&select){e.preventDefault();select(label);}}}><title>{label}: {r.value} ({(100*r.value/total).toFixed(1)}%)</title></circle><text x={x} y={y} textAnchor={x<110?"end":x>130?"start":"middle"}>{label.slice(0,19)}<tspan x={x} dy="12">{(100*r.value/total).toFixed(1)}%</tspan></text></g>;})}
 </svg><div className="vima-response-counts">{values.map((r,i)=><span key={i}><i style={{background:responseColor(r.label,i,design)}}/>{String(r.label??"Not recorded")}: {r.value.toLocaleString()}</span>)}</div></div>;
}
export function ChartTabs({title, tabs, active, select, className = "vima-tabs"}: {className?: string;title: string; tabs: {id:number;label:string}[];active:number;select:(id:number)=>void}) {
 return <div className={className} role="tablist" aria-label={title}>{tabs.map((tab,i)=><button key={tab.id} role="tab" aria-selected={tab.id===active} tabIndex={tab.id===active?0:-1} onClick={()=>select(tab.id)} onKeyDown={e=>{const next=e.key==="ArrowRight"?(i+1)%tabs.length:e.key==="ArrowLeft"?(i+tabs.length-1)%tabs.length:e.key==="Home"?0:e.key==="End"?tabs.length-1:-1;if(next>=0){e.preventDefault();select(tabs[next].id);(e.currentTarget.parentElement?.children[next] as HTMLElement)?.focus();}}}>{tab.label}</button>)}</div>;
}
export function DesignLibrary() {
 const [alternate,setAlternate]=useState(false),[active,setActive]=useState(1);
 const design:DashboardDesign=alternate?{colors:{yes:"#2563eb",no:"#f59e0b"}}:{};
 return <DesignContext.Provider value={design}><main className="dashboard-shell vima-baseline"><header className="dashboard-header"><div><a href="/lifedata-ai/dashboards/">Dashboard library</a><h1>Vima design library</h1><p>Buikwe baseline · v1 · Examples use demonstration data</p></div><button onClick={()=>setAlternate(!alternate)}>Change example colors</button></header><div className="dashboard-grid"><section className="chart-card" style={{gridColumn:"span 4",height:330}}><div className="card-header"><h2>Yes / No response</h2></div><ResponseDonut title="Yes / No example" values={[{label:"Yes",value:72},{label:"No",value:23},{label:null,value:5}]}/></section><section className="chart-card" style={{gridColumn:"span 4",height:330}}><div className="card-header"><h2>Related variables in one card</h2></div><ChartTabs title="Example variables" tabs={[{id:1,label:"Blood donation"},{id:2,label:"Organ donation"},{id:3,label:"Clinical trial"}]} active={active} select={setActive}/><ResponseDonut title="Tabbed response example" values={[{label:"Yes",value:30+active*10},{label:"No",value:70-active*10}]}/></section><section className="chart-card" style={{gridColumn:"span 4",height:330}}><div className="card-header"><h2>Design precepts</h2></div><div className="vima-precepts"><p>Inter / Segoe UI / Arial. Compact cards, pale canvas, quiet borders, clear labels and readable values.</p><p>Put headline metrics first, related variables in tabs, the map in the central wide area, and detail below.</p><p>Donuts for binary responses; horizontal bars for longer categories; lines for time; tables for detail.</p><p>Colors may change by prompt. Labels, counts, missing values, and keyboard navigation remain explicit.</p><a href="/lifedata-ai/dashboards/2e75e425-ae09-4cd8-acf6-6fa4dcfabae3/">Open the Buikwe benchmark ↗</a></div></section></div></main></DesignContext.Provider>;
}

export function syncDashboardFilters(filters:{column:string;operator:string;values:string[]}[]) {
 const host=window as Window & {__lifedataDashboardContext?:Record<string,unknown>};
 if(host.__lifedataDashboardContext?.dashboard_surface!=="custom")return;
 host.__lifedataDashboardContext={...host.__lifedataDashboardContext,filters};
 window.dispatchEvent(new Event("lifedata:context"));
}
