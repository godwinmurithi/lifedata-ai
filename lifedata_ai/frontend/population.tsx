import { DashboardDesign, DesignContext, responseColor, ResponseDonut, syncDashboardFilters } from "./design-system";
import React, { useEffect, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "./population.css";

type Row = Record<string, any>;
type Criterion = string[] | { min?: number; max?: number };
type Criteria = Record<string, Criterion>;
type Field = { field: string; label: string; group: string; stage: number; kind: string; options?: string[]; extent?: [number, number] | null };
type Catalog = { catalog: Field[]; facilities: Row[]; boundaries: Row[]; default_facility: string; notes: string[] };
type Result = { stages: { label: string; count: number }[]; matches: number; breakdowns: Record<string, Row[]>; records: Row[]; mapped: number; records_truncated: boolean };
export type PopulationConfiguration = { design?: DashboardDesign; preset: "population-discovery"; source_chart_id: number; boundary_chart_id: number; facility_chart_id: number; default_facility: string; native_dashboard_ids?: number[] };
const colors = ["#cc00ff", "#ffff00", "#87ec36", "#34c8df", "#ff9200", "#f04bff", "#36c98a", "#7a75e8"];
const facilityStyles: Record<string, { color: string; radius: number; label: string }> = {
  HCII: { color: "#22c55e", radius: 7, label: "HC II" },
  HCIII: { color: "#38bdf8", radius: 9, label: "HC III" },
  HCIV: { color: "#f59e0b", radius: 11, label: "HC IV" },
  HOSPITAL: { color: "#e879f9", radius: 13, label: "Hospital" },
  UNKNOWN: { color: "#94a3b8", radius: 7, label: "Level not recorded" },
};
const facilityLevelKey = (value: unknown) => String(value || "").trim().toUpperCase().replace(/\s+/g, "") || "UNKNOWN";
const validFacility = (r: Row) => typeof r.lat === "number" && Number.isFinite(r.lat) && Math.abs(r.lat) <= 90 && typeof r.lng === "number" && Number.isFinite(r.lng) && Math.abs(r.lng) <= 180;
export function facilityLevelStyles(facilities: Row[]) {
  const levels = new Map<string, string>();
  facilities.forEach(f => levels.set(facilityLevelKey(f.facility_level), String(f.facility_level || "").trim()));
  return Object.fromEntries([...levels.entries()].sort(([a],[b])=>a.localeCompare(b)).map(([key,label],i) => {
    const known=facilityStyles[key];
    const numeric=label.match(/^(?:level\s*)?(\d+)$/i);
    const radius=numeric ? Math.min(17, 5+2*Number(numeric[1])) : 9;
    return [key, known || {color:`hsl(${(i*137.508+215)%360}, 72%, 55%)`,radius,label}];
  }));
}
export function facilityAppearance(facility: Row, styles: ReturnType<typeof facilityLevelStyles>) {
  const key = facilityLevelKey(facility.facility_level);
  const style = styles[key] || facilityStyles.UNKNOWN;
  const raw = facility.beds;
  const number = raw == null || String(raw).trim() === "" ? NaN : Number(raw);
  const beds = Number.isFinite(number) && number >= 0 ? number : null;
  return { ...style, beds, radius: Math.min(24, Math.sqrt(style.radius ** 2 + (beds ?? 0))) };
}
const groups = ["Participation", "Blood group", "Screening", "Demographics & clinical", "Geography", "Laboratory values", "Laboratory flags", "Vaccination records"];
const panelDefinitions: {id:string; title:string; tabs:[string,string][]}[] = [
 {id:"blood",title:"Blood Group",tabs:[["blood_group","Bld grp"],["sickle_cells_disease","Sickle cell"],["sickle_cell_carrier","Carrier"]]},
 {id:"donor",title:"Blood Donor",tabs:[["blood_donor","Bld"],["organ_donor","Org"],["clinical_trial","Clin tr"],["malaria","Malaria"]]},
 {id:"liver",title:"Liver Abnormalities",tabs:[["shbsab","HBsAb"],["s_hbsag","HBsAg"],["s_hcv","HCV"],["s_total_cholesterol","Cho"]]},
 {id:"screening",title:"Visual issues",tabs:[["visual_issue","Vis"],["hearing_issues","Hear"],["hiv_status","HIV"]]},
 {id:"pressure",title:"Blood Pressure",tabs:[["bp_results","Press"],["village","Villages"],["mental_health","MHD"]]},
 {id:"vaccines",title:"Vaccines",tabs:[["v_sinovac","Sin"],["v_shingles","Shi"],["v_covid_astrozenica_oxford","Ast Zen"]]},
 {id:"hematology",title:"Hematological disorders",tabs:[["s_vitamin_d","Vit D"],["s_baso_","BAS%"],["s_baso","BAS"],["s_eos","EOS"],["s_hgb","HGB"]]},
 {id:"disability",title:"Disability | Thyroid",tabs:[["disability","Dis"],["s_tsh__thyroid","TSH"]]},
 {id:"sexual",title:"Sexual Abnormalities",tabs:[["s_psa","PSA"],["s_syphillis","Syp"],["s_testorterone","Tes"]]},
 {id:"renal",title:"Renal | Liver",tabs:[["s_creatinine","Ren"],["s_alt","Liv"]]},
 {id:"systemic",title:"Systemic disorders",tabs:[["s_fasting_glucose","Glucose"],["s_hba1c","Diabetes"],["s_ck","Car"],["s_amylase","Pan"]]},
];
const fmt = (value: unknown) => value == null ? "Not recorded" : typeof value === "number" ? Number.isInteger(value) ? value.toLocaleString() : value.toFixed(2) : String(value);
const validPoint = (r: Row) => typeof r.latitude === "number" && typeof r.longitude === "number" && Math.abs(r.latitude) <= 90 && Math.abs(r.longitude) <= 180;

async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, { credentials: "same-origin", ...init });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "Unable to read population data");
  return body;
}

function PopulationMap({ rows, boundaries, facilities, allFacilities, selected, radius, selectArea, selectPerson, selectFacility }: {
  rows: Row[]; boundaries: Row[]; facilities: Row[]; allFacilities: Row[]; selected: string[]; radius?: number;
  selectArea: (name: string) => void; selectPerson: (id: string) => void; selectFacility: (name: string) => void;
}) {
  const levelStyles = facilityLevelStyles(allFacilities);
  const host = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map>();
  const layers = useRef<L.LayerGroup>();
  const first = useRef(true);
  const [tilesFailed, setTilesFailed] = useState(false);
  useEffect(() => {
    if (!host.current) return;
    map.current = L.map(host.current).setView([0, 0], 2);
    const streets=L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",{attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',maxZoom:19});
    const satellite=L.tileLayer("https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",{attribution:"Esri, Vantor, Earthstar Geographics, GIS User Community",maxZoom:19}).addTo(map.current);
    satellite.on("tileerror",()=>setTilesFailed(true)).on("tileload",()=>setTilesFailed(false));
    streets.on("tileerror",()=>setTilesFailed(true)).on("tileload",()=>setTilesFailed(false));
    L.control.layers({"Satellite":satellite,"Street map":streets},undefined,{position:"topright"}).addTo(map.current);
    layers.current = L.layerGroup().addTo(map.current);
    const observer = new ResizeObserver(() => map.current?.invalidateSize());
    observer.observe(host.current);
    return () => { observer.disconnect(); map.current?.remove(); };
  }, []);
  useEffect(() => {
    if (!map.current || !layers.current) return;
    layers.current.clearLayers();
    const counts = new Map<string, number>();
    rows.forEach(r => counts.set(r.subcounty, (counts.get(r.subcounty) || 0) + 1));
    boundaries.forEach(area => {
      let geometry;
      try { geometry = typeof area.geometry_geojson === "string" ? JSON.parse(area.geometry_geojson) : area.geometry_geojson; } catch { return; }
      const active = selected.includes(area.subcounty);
      L.geoJSON(geometry, { style: { color: active ? "#a43455" : "#7196a3", weight: active ? 3 : 1.5,
        fillColor: active ? "#b33f5d" : "#60b7aa", fillOpacity: active ? .18 : .07 },
        onEachFeature: (_, layer) => {
          const label = document.createElement("div");
          label.textContent = `${area.subcounty} · ${counts.get(area.subcounty) || 0} current matches · Click to select area`;
          layer.bindTooltip(label).on("click", () => selectArea(area.subcounty));
        },
      }).addTo(layers.current!);
    });
    const bloodGroups = [...new Set(rows.map(r => r.blood_group))].sort();
    rows.filter(validPoint).forEach(person => {
      const marker = L.circleMarker([person.latitude, person.longitude], {
        radius: 5, color: "#fff", weight: 1, fillOpacity: .88,
        fillColor: colors[bloodGroups.indexOf(person.blood_group) % colors.length],
      });
      const popup = document.createElement("div");
      const title = document.createElement("strong"); title.textContent = person.person_id; popup.append(title);
      const info = document.createElement("p"); info.textContent = `${fmt(person.blood_group)} · ${fmt(person.age)} years · ${fmt(person.village)}`; popup.append(info);
      const button = document.createElement("button"); button.textContent = "View permitted detail"; button.onclick = () => selectPerson(person.person_id); popup.append(button);
      marker.bindPopup(popup).addTo(layers.current!);
    });
    facilities.forEach(facility => { if (validFacility(facility)) {
      const appearance = facilityAppearance(facility, levelStyles);
      const label = document.createElement("strong"); label.textContent = `${facility.facility_name} · ${appearance.label} · ${appearance.beds == null ? "Beds not recorded" : `${appearance.beds} beds`}`;
      L.circleMarker([facility.lat, facility.lng], { radius: appearance.radius, color: "#fff", weight: 2, fillColor: appearance.color, fillOpacity: 1, className: "pd-facility-marker" }).on("click", () => selectFacility(facility.facility_name)).bindTooltip(label, { direction: "top" }).addTo(layers.current!);
      if (radius != null && radius >= 0) L.circle([facility.lat, facility.lng], { radius: radius * 1000, color: "#b33f5d", fillOpacity: .035, dashArray: "5 5" }).addTo(layers.current!);
    }});
    if (first.current && (rows.some(validPoint) || facilities.some(validFacility))) {
      first.current = false;
      const points = rows.filter(validPoint).map(r => [r.latitude, r.longitude] as L.LatLngTuple);
      facilities.filter(validFacility).forEach(f => points.push([f.lat, f.lng]));
      map.current.fitBounds(L.latLngBounds(points), { padding: [45, 45], maxZoom: 13 });
    }
  }, [rows, boundaries, facilities, allFacilities, selected.join("|"), radius]);
  function fitMatches() {
    const points = rows.filter(validPoint).map(r => [r.latitude, r.longitude] as L.LatLngTuple);
    if (points.length) map.current?.fitBounds(L.latLngBounds(points), { padding: [35, 35], maxZoom: 14 });
  }
  return <section className="pd-map-card pd-card">
    <div className="pd-card-heading"><div><span className="pd-eyebrow">GEOGRAPHIC DISCOVERY</span><h2>Where are the matches?</h2></div><button onClick={fitMatches} disabled={!rows.some(validPoint)}>Fit matches</button></div>
    <div ref={host} className="pd-map" aria-label="Population points and selectable administrative polygons" />
    <div className="pd-map-legend"><span><i className="pd-dot" /> Population points</span><span><i className="pd-polygon" /> Click an area to filter</span>{Object.entries(levelStyles).map(([key, style]) => <span key={key}><i className="pd-facility-key" style={{ backgroundColor: style.color }} />{style.label}</span>)}<span>{facilities.length} facilities · Size: level + beds · Click to select</span></div>
    {tilesFailed && <p className="pd-inline-note">Background tiles unavailable. Points and area boundaries remain interactive.</p>}
  </section>;
}

function segmentColor(label: unknown, index: number, design: DashboardDesign) {
  const value=String(label).toLowerCase();
  if(["yes","negative","normal","no abnormality"].includes(value))return design.colors?.yes || "#13a58a";
  if(["no","positive","high","abnormal"].includes(value))return design.colors?.no || "#e15b6c";
  if(value==="low")return "#e7ac42";
  if(label==null)return design.colors?.unknown || "#777";
  return responseColor(label,index,design);
}
export function Pie({ field, rows, select, loading, unit = "people" }: {field:string;rows:Row[];select:(field:string,value:string)=>void;loading:boolean;unit?:string}) {
  const parent=React.useContext(DesignContext),override=parent.chart_styles?.[`field:${field}`] || {},design={...parent,...override,colors:{...parent.colors,...override.colors}};
  const total=rows.reduce((n,r)=>n+Number(r.people),0);let offset=0;
  const values: Row[]=rows.map((r,i)=>{const value=Number(r.people),start=offset;offset+=value;return {...r,value,start,color:segmentColor(r[field],i,design)};});
  const isBar=["bp_results","s_fasting_glucose"].includes(field);
  const binary=rows.length>0 && rows.every(r=>r[field]==null || ["yes","no","y","n","true","false"].includes(String(r[field]).toLowerCase()));
  if(binary && !loading) return <DesignContext.Provider value={design}><ResponseDonut title={`${field} response chart`} values={rows.map(r=>({label:r[field],value:Number(r.people)}))} select={value=>select(field,value)}/></DesignContext.Provider>;
  if(isBar && !loading && total)return <div className="sb-horizontal-bars">{values.map((r,i)=><button key={i} disabled={r[field]==null} title={`${fmt(r[field])}: ${r.value} ${unit}`} onClick={()=>select(field,String(r[field]))}><span>{fmt(r[field])}</span><i><em style={{width:`${100*r.value/Math.max(...values.map(v=>v.value))}%`,background:r.color}}/></i><b>{r.value}</b></button>)}</div>;
  return <div className="sb-pie-body">{loading?<span className="sb-pie-message">Updating…</span>:total?<svg className={`sb-pie ${isBar?"sb-bar":""}`} viewBox="0 0 240 180" role="img" aria-label={`${field} ${isBar?"bar":"pie"} chart`}>
    {isBar?values.map((r,i)=>{const w=200/values.length,x=22+i*w,h=112*r.value/Math.max(...values.map(v=>v.value));return <g key={i} role="button" tabIndex={r[field]==null?-1:0} aria-label={`${fmt(r[field])}: ${r.value} ${unit}`} onClick={()=>r[field]!=null&&select(field,String(r[field]))} onKeyDown={e=>{if(e.key==="Enter"&&r[field]!=null)select(field,String(r[field]));}}><rect x={x+3} y={140-h} width={w-7} height={h} fill={r.color}/><text x={x+w/2} y={134-h} textAnchor="middle">{r.value}</text><text x={x+w/2} y="155" textAnchor="middle">{fmt(r[field])}</text></g>;}):<><circle cx="120" cy="88" r="48" fill="none" stroke="#e8eef3" strokeWidth="23"/>{values.map((r,i)=>{const angle=2*Math.PI*(r.start+r.value/2)/total-Math.PI/2,x=120+72*Math.cos(angle),y=88+72*Math.sin(angle);return <g key={i}><circle cx="120" cy="88" r="48" fill="none" stroke={r.color} strokeWidth="23" pathLength="100" strokeDasharray={`${100*r.value/total} ${100-100*r.value/total}`} strokeDashoffset={-100*r.start/total} transform="rotate(-90 120 88)" role="button" tabIndex={r[field]==null?-1:0} aria-label={`${fmt(r[field])}: ${r.value} ${unit}`} onClick={()=>r[field]!=null&&select(field,String(r[field]))} onKeyDown={e=>{if((e.key==="Enter"||e.key===" ")&&r[field]!=null)select(field,String(r[field]));}}><title>{fmt(r[field])}: {r.value} ({(100*r.value/total).toFixed(1)}%)</title></circle><text x={x} y={y} textAnchor={x<110?"end":x>130?"start":"middle"}>{String(r[field]??"Not recorded").slice(0,19)}<tspan x={x} dy="10">{field==="blood_group"?r.value:`${(100*r.value/total).toFixed(1)}%`}</tspan></text></g>;})}</>}
  </svg>:<span className="sb-pie-message">No matching records</span>}</div>;
}

function Selector({ field, value, set }: { field: Field; value?: Criterion; set: (value?: Criterion) => void }) {
  if (field.kind === "number") {
    const range = (!Array.isArray(value) && value) || {};
    return <fieldset className="pd-range"><legend>{field.label}</legend><div>
      {(["min", "max"] as const).map(key => <input key={key} type="number" step="any" value={range[key] ?? ""}
        aria-label={`${field.label} ${key === "min" ? "minimum" : "maximum"}`} placeholder={key === "min" ? "Min" : "Max"}
        onChange={event => { const next = {...range}; if (!event.target.value) delete next[key]; else next[key] = Number(event.target.value); set(Object.keys(next).length ? next : undefined); }} />)}
      </div>{field.extent && <small>Recorded range {fmt(field.extent[0])}–{fmt(field.extent[1])}</small>}</fieldset>;
  }
  return <label className="pd-select">{field.label}<select aria-label={field.label} value={Array.isArray(value) ? value[0] || "" : ""} onChange={e => set(e.target.value ? [e.target.value] : undefined)}>
    <option value="">All recorded values</option>{field.options?.map(option => <option key={option}>{option}</option>)}
  </select></label>;
}

export default function Population({ id, title, config, theme, themeControl }: { id: string; title: string; config: PopulationConfiguration; theme: "light" | "dark"; themeControl: React.ReactNode }) {
  const endpoint = `/lifedata-ai/api/publications/${id}/population/`;
  const [catalog, setCatalog] = useState<Catalog>();
  const [criteria, setCriteria] = useState<Criteria>({});
  const [facilityName, setFacilityName] = useState(config.default_facility || "");
  const [panelFields, setPanelFields] = useState<Record<string,string>>(Object.fromEntries(panelDefinitions.map(p=>[p.id,p.tabs[0][0]])));
  const [peopleField, setPeopleField] = useState("people");
  const [data, setData] = useState<Result>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [extraOpen, setExtraOpen] = useState(false);
  const [selectorSearch, setSelectorSearch] = useState("");
  const [person, setPerson] = useState("");
  const [detail, setDetail] = useState<Row | null>();
  const [page, setPage] = useState(0);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    function escape(event: KeyboardEvent) { if(event.key === "Escape") { setExtraOpen(false); setPerson(""); } }
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, []);
  useEffect(() => { const controller = new AbortController(); request<Catalog>(endpoint, { signal: controller.signal }).then(value => {
      setCatalog(value); setFacilityName(value.default_facility);
    }).catch(err => { if (err.name !== "AbortError") { setError(err.message); setLoading(false); } });
    return () => controller.abort();
  }, [id]);
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError(""); setPerson(""); setPage(0);
    const timer = window.setTimeout(() => request<Result>(endpoint, {
      method: "POST", headers: {"Content-Type":"application/json"}, signal: controller.signal,
      body: JSON.stringify({criteria, facility:facilityName, profile_fields:[...new Set([...Object.values(panelFields),"mental_health"])]}),
    }).then(value => { setData(value); setLoading(false); }).catch(err => {
      if (err.name !== "AbortError") { setError(err.message); setLoading(false); }
    }), 180);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [criteria, facilityName, refresh, JSON.stringify(panelFields)]);
  useEffect(() => {
    setDetail(undefined);
    if (!person) return;
    const controller = new AbortController();
    request<{record:Row|null}>(endpoint, {method:"POST",headers:{"Content-Type":"application/json"},signal:controller.signal,
      body:JSON.stringify({criteria,facility:facilityName,person_id:person})}).then(r=>setDetail(r.record)).catch(err=>{
        if(err.name!=="AbortError")setError(err.message);
      });
    return () => controller.abort();
  }, [person]);
  const fields = new Map(catalog?.catalog.map(f => [f.field,f]) || []);
  const facilities = (catalog?.facilities || []).filter(r => !facilityName || r.facility_name === facilityName);
  useEffect(()=>{
    const current:{column:string;operator:string;values:string[]}[]=[];
    Object.entries(criteria).forEach(([column,value])=>{
      if(Array.isArray(value))current.push({column,operator:"IN",values:value});
      else {if(value.min!=null)current.push({column,operator:">=",values:[String(value.min)]});if(value.max!=null)current.push({column,operator:"<=",values:[String(value.max)]});}
    });
    if(facilityName)current.push({column:"facility_name",operator:"IN",values:[facilityName]});
    syncDashboardFilters(current);
  },[JSON.stringify(criteria),facilityName]);
  const activeCount = Object.keys(criteria).length;
  const live = !loading && !error ? data : undefined;
  const primary = ["blood_donor", "blood_group", "hiv_status", "s_hbsag", "s_hcv", "s_syphillis", "age", "sex", "district", "county", "subcounty", "parish", "village", "distance_km"];
  const selectedArea = Array.isArray(criteria.subcounty) ? criteria.subcounty : [];
  const radius = !Array.isArray(criteria.distance_km) ? criteria.distance_km?.max : undefined;
  function set(field: string, value?: Criterion) {
    setCriteria(previous => {
      const next = {...previous}; if(value) next[field]=value; else delete next[field];
      const hierarchy = ["district","county","subcounty","parish","village"];
      const index = hierarchy.indexOf(field);
      if(index>=0) hierarchy.slice(index+1).forEach(f=>delete next[f]);
      return next;
    });
  }
  function select(field: string, value: string) { if(fields.has(field)) set(field,[value]); }
  const labels = (field: string, value: Criterion) => Array.isArray(value) ? value.join(", ") : `${value.min != null ? `≥ ${value.min}` : ""}${value.min != null && value.max != null ? " · " : ""}${value.max != null ? `≤ ${value.max}` : ""}`;
  const currentNative = config.native_dashboard_ids?.[1];
  async function openNative(event: React.MouseEvent<HTMLAnchorElement>) {
    if (!currentNative) return;
    event.preventDefault();
    try {
      if(Object.keys(criteria).some(f=>!primary.includes(f)))throw new Error("Keep this advanced selection in the Sandbox view. Superset's top selectors cover demographics, screening and geography.");
      const dataMask: Record<string, object> = {};
      const selected: Criteria = {...criteria}; if (facilityName) selected.facility_name=[facilityName];
      for(const [field,value] of Object.entries(selected)) {
        const filters = Array.isArray(value) ? [{col:field,op:"IN",val:value}] : Object.entries(value).map(([key,val])=>({col:field,op:key==="min"?">=":"<=",val}));
        const range = fields.get(field)?.extent || [0,100];
        const filterValue = Array.isArray(value) ? value : [value.min ?? range[0],value.max ?? range[1]];
        const nativeId = `NATIVE_FILTER-discovery-${field}`;
        dataMask[nativeId] = {id:nativeId,extraFormData:{filters},filterState:{value:filterValue,label:labels(field,value)}};
      }
      const csrf = await request<{result:string}>("/api/v1/security/csrf_token/");
      const permalink = await request<{url:string}>(`/api/v1/dashboard/${currentNative}/permalink`, {
        method:"POST",headers:{"Content-Type":"application/json","X-CSRFToken":csrf.result},
        body:JSON.stringify({dataMask,activeTabs:[]}),
      });
      const url = new URL(permalink.url,location.origin);
      location.assign(url.pathname+url.search+url.hash);
    } catch(err) { setError(err instanceof Error ? err.message : "Unable to open Superset with selected criteria"); }
  }
  return <DesignContext.Provider value={config.design || {}}><main className={`pd-shell sb-shell ${theme}`} style={config.design?.colors?.accent ? {"--pd-accent":config.design.colors.accent,"--pd-text-a43b5d":config.design.colors.accent,"--pd-surface-a43b5d":config.design.colors.accent} as React.CSSProperties : undefined}>
    <header className="sb-toolbar"><a className="sb-title" href="/lifedata-ai/dashboards/">{title}</a>{["district","county","subcounty","parish","village","age"].filter(f=>fields.has(f)).map(f=><Selector key={f} field={fields.get(f)!} value={criteria[f]} set={v=>set(f,v)}/>)}<label className="pd-select sb-facility">Facility<select aria-label="Selected facility" value={facilityName} onChange={e=>setFacilityName(e.target.value)}><option value="">All facilities</option>{catalog?.facilities.map((r,i)=><option key={i}>{r.facility_name}</option>)}</select></label><button className="sb-menu" aria-label="All available selectors" onClick={()=>setExtraOpen(true)}>☰</button></header>
    <div className="sb-criteria-strip">{themeControl}<span role="status">{loading?"Updating through Superset…":error||`${live?.matches ?? "—"} current matches · ${activeCount} criteria`}</span><div className="sb-active">{Object.entries(criteria).map(([f,v])=><button key={f} onClick={()=>set(f)} aria-label={`Remove ${fields.get(f)?.label||f}`}>{fields.get(f)?.label||f}: {labels(f,v)} ×</button>)}</div><button onClick={()=>setCriteria({})} disabled={!activeCount}>Reset all</button><button onClick={()=>setRefresh(n=>n+1)}>Refresh</button><a href={`/superset/dashboard/${currentNative}/`} onClick={openNative}>Superset ↗</a></div>
    <div className="sb-grid">
      <section className="sb-panel sb-people"><h2>Number of People <small>CURRENT MATCHES</small></h2>{peopleField==="people"?<div className="sb-gauge"><svg viewBox="0 0 260 155" role="img" aria-label="Current matching population gauge"><path d="M 25 125 A 105 105 0 0 1 235 125" fill="none" stroke="#e8eef3" strokeWidth="28"/><path d="M 25 125 A 105 105 0 0 1 235 125" fill="none" stroke={config.design?.chart_styles?.gauge?.color || "#e7ac42"} strokeWidth="28" pathLength="100" strokeDasharray={`${live?100*live.matches/Math.max(1,live.stages[0].count):0} 100`}/><text x="130" y="126" textAnchor="middle" data-testid="population-matches">{loading?"…":live?.matches??"—"}</text><text x="14" y="150">0</text><text x="245" y="150" textAnchor="end">{live?.stages[0].count??"—"}</text></svg></div>:<Pie field={peopleField} rows={live?.breakdowns[peopleField]||[]} select={select} loading={loading}/>}
      <div className="sb-tabs" role="tablist" aria-label="Population metrics">{[["people","Pple"],["sex","Sex"],["mental_health","MH"],["age_band","Age"]].map(([f,label])=><button role="tab" aria-selected={peopleField===f} key={f} onClick={()=>setPeopleField(f)}>{label}</button>)}</div></section>
      {panelDefinitions.map(panel=>{const tabs=panel.tabs.filter(([f])=>fields.has(f)),field=panelFields[panel.id];return <section key={panel.id} className={`sb-panel sb-${panel.id}`}><h2>{panel.title}</h2><Pie field={field} rows={live?.breakdowns[field]||[]} select={select} loading={loading}/><div className="sb-tabs" role="tablist" aria-label={`${panel.title} questions`}>{tabs.map(([f,label])=><button role="tab" key={f} aria-selected={field===f} onClick={()=>setPanelFields(previous=>({...previous,[panel.id]:f}))}>{label}</button>)}</div></section>;})}
      <PopulationMap rows={live?.records||[]} boundaries={catalog?.boundaries||[]} facilities={facilities} allFacilities={catalog?.facilities || []} selectFacility={setFacilityName} selected={selectedArea} radius={radius} selectArea={name=>set("subcounty",selectedArea.includes(name)?undefined:[name])} selectPerson={setPerson}/>
      <div className="sb-centre-bottom"><section className="sb-panel sb-funnel pd-funnel"><h2>Population discovery</h2><div className="pd-stage-grid">{(live?.stages||["Total population","Willingness / participation","Required blood group","Selected screening results","Age & clinical criteria","Geography & proximity"].map(label=>({label,count:0}))).map((stage,i)=><div className={`pd-stage ${i===5?"pd-stage-final":""}`} key={stage.label} data-testid={`population-stage-${i}`}><span>{i+1}</span><p>{stage.label}</p><strong>{live?stage.count:"…"}</strong></div>)}</div></section>
      <section className="sb-panel pd-records"><h2>Matching population · authorized records</h2><div className="pd-table-scroll"><table><thead><tr>{["Record","Age","Sex","Blood group","Area","Distance km","Willingness","HIV","HBsAg","HCV","Syphilis"].map(t=><th key={t}>{t}</th>)}</tr></thead><tbody>{live?.records.map(r=><tr key={r.person_id}><td><button onClick={()=>setPerson(r.person_id)} aria-label={`View ${r.person_id}`}>{r.person_id}</button></td>{[r.age,r.sex,r.blood_group,r.village,r.distance_km,r.blood_donor,r.hiv_status,r.s_hbsag,r.s_hcv,r.s_syphillis].map((v,i)=><td key={i}>{fmt(v)}</td>)}</tr>)}</tbody></table>{live&&!live.records.length&&<p>No matching records.</p>}</div></section></div>
    </div><footer className="sb-footer">{live?.mapped??"—"} mapped · {facilities.length} facilities · Matches selected discovery criteria; clinical review required for donation. Willingness is not separate contact consent.</footer>
    {extraOpen && <div className="pd-overlay" onClick={()=>setExtraOpen(false)}><section className="pd-selector-dialog" role="dialog" aria-modal="true" aria-label="All available discovery selectors" onClick={e=>e.stopPropagation()}><div className="pd-card-heading"><div><h2>Explore every available criterion</h2><p className="pd-muted">Apply recorded demographic, clinical, laboratory and vaccination criteria.</p></div><button onClick={()=>setExtraOpen(false)}>Done</button></div><div className="sb-primary-selectors">{primary.filter(f=>fields.has(f)).map(f=><Selector key={f} field={fields.get(f)!} value={criteria[f]} set={v=>set(f,v)}/>)}</div><input className="pd-search" aria-label="Find a selector" placeholder="Search selectors…" value={selectorSearch} onChange={e=>setSelectorSearch(e.target.value)} /><div className="pd-all-selectors">{groups.map(group=>{const entries=catalog?.catalog.filter(f=>f.group===group && f.label.toLowerCase().includes(selectorSearch.toLowerCase()));return entries?.length?<details key={group} open={!!selectorSearch||group==="Screening"}><summary>{group}<span>{entries.length} selectors</span></summary><div className="pd-selector-grid">{entries.map(field=><Selector key={field.field} field={field} value={criteria[field.field]} set={value=>set(field.field,value)} />)}</div></details>:null;})}</div></section></div>}
    {person && <div className="pd-overlay" onClick={()=>setPerson("")}><section className="pd-detail-dialog" role="dialog" aria-modal="true" aria-label={`Permitted detail for ${person}`} onClick={e=>e.stopPropagation()}><div className="pd-card-heading"><div><span className="pd-eyebrow">PERMITTED INDIVIDUAL DETAIL</span><h2>{person}</h2></div><button onClick={()=>setPerson("")}>Close</button></div><p className="pd-inline-note">Recorded values describe this matching record. No medical approval or contact permission is inferred.</p>{detail===undefined?<p>Loading permitted detail…</p>:detail===null?<p>This record is no longer in the selected cohort.</p>:groups.map(group=><section key={group}><h3>{group}</h3><dl className="pd-detail-fields">{catalog?.catalog.filter(f=>f.group===group&&f.field in detail).map(f=><div key={f.field}><dt>{f.label}</dt><dd>{fmt(detail[f.field])}</dd></div>)}</dl></section>)}</section></div>}
  </main></DesignContext.Provider>;
}
