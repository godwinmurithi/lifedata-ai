import React, { useEffect, useRef, useState } from "react";
import ReactDOM from "react-dom";
import {
  DndContext,
  closestCenter,
  PointerSensor,
  KeyboardSensor,
  useSensor,
  useSensors,
  DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  useSortable,
  arrayMove,
  rectSortingStrategy,
  sortableKeyboardCoordinates,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import * as echarts from "echarts/core";
import { BarChart, LineChart, PieChart, ScatterChart } from "echarts/charts";
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartsOption } from "echarts";
import "./app.css";
import Facilities, { FacilityConfiguration } from "./facilities";
import { DashboardDesign, DesignContext, designTokens, responseColor, ResponseDonut, ChartTabs, DesignLibrary, syncDashboardFilters } from "./design-system";
import BuikweWorkspace, {WorkspaceConfiguration} from "./buikwe-workspace";
import Population, { PopulationConfiguration } from "./population";
import HdssTemplate, { HdssCreator } from "./hdss-template";
import MasterTemplate from "./master-template";

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  ScatterChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  CanvasRenderer,
]);

import {mountQueryMap} from "./query-map";
import { Encoding, resolveEncoding, pivotRows, formatValue } from "./chart-encoding";

type Row = Record<string, unknown>;
type Layout = { chart_id: number; span: number; height: number };
type Chart = { id: number; title: string; viz_type: string };
type Composition = {encodings?:Record<string,Encoding>;layout_mode?:"flow"|"viewport";purpose:string;subtitle?:string;skin?:"navy-teal"|"warm";sections?:{title:string;chart_ids:number[]}[];filters?:{field:string;label:string;chart_ids:number[]}[]};
type ReferenceConfiguration = {preset:"reference-template";template:"sincep-hdss";source_chart_id:number;chart_ids:number[];bindings:Record<string,{chart_ids:number[];title?:string;encoding?:Encoding}>;composition?:Composition;design?:DashboardDesign};
type ComposedConfiguration = {preset:"composed-dashboard";source_chart_id:number;chart_ids:number[];composition:Composition;design?:DashboardDesign};
function getComposition(publication?:Publication):Composition|undefined {const config=publication?.configuration;return config && (config.preset==="composed-dashboard" || config.preset==="reference-template") ? (config as ComposedConfiguration).composition : undefined;}
export type Publication = {
  configuration?: (FacilityConfiguration | PopulationConfiguration | WorkspaceConfiguration | ComposedConfiguration | ReferenceConfiguration | {preset?: undefined}) & {design?: DashboardDesign};
  quality?:{status:string;build_id?:string};
  id: string;
  title: string;
  theme: string;
  layout: Layout[];
  dashboard_id: number | null;
  can_edit: boolean;
  url: string;
  charts: Chart[];
  owner?: string;
  created_at?: string;
  updated_at?: string;
  chart_count?: number;
};
declare global {
  interface Window {
    __lifedataCopilot?: { open: () => void };
  }
}
type ChartData = {
  rows: Row[];
  columns: string[];
  form_data: Record<string, unknown>;
  row_limit: number;
};
const root = document.getElementById("lifedata-dashboard")!;
const publicationId = root.dataset.publication || "";
const api = "/lifedata-ai/api/publications/";

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { credentials: "same-origin", ...init });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      body.error || body.message || `Request failed (${response.status})`,
    );
  }
  return response.json();
}
function number(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (
    typeof value === "string" &&
    value.trim() !== "" &&
    Number.isFinite(Number(value))
  )
    return Number(value);
  return null;
}
function display(value: unknown): string {
  return value === null || value === undefined
    ? "—"
    : typeof value === "object"
      ? JSON.stringify(value)
      : String(value);
}
function relativeTime(iso?: string): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(iso).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}
const ACCENTS = ["#7fb832", "#9aca55", "#c98a1f", "#4d8b85", "#7a6a9e", "#16332a"];
function accentFor(id: string): string {
  let hash = 0;
  for (let index = 0; index < id.length; index += 1)
    hash = (hash * 31 + id.charCodeAt(index)) >>> 0;
  return ACCENTS[hash % ACCENTS.length];
}
function SearchIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <circle cx="11" cy="11" r="7" />
      <path d="M21 21l-4.3-4.3" />
    </svg>
  );
}
function ClockIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3.5 2" />
    </svg>
  );
}
function Table({ data }: { data: ChartData }) {
  const [page, setPage] = useState(0);
  const [query, setQuery] = useState("");
  const rows = data.rows.filter(
    (row) =>
      !query ||
      data.columns.some((column) =>
        display(row[column]).toLowerCase().includes(query.toLowerCase()),
      ),
  );
  return (
    <div className="data-table">
      <input
        className="table-search"
        aria-label="Search table"
        placeholder="Search these rows…"
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
          setPage(0);
        }}
      />
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {data.columns.map((column) => (
                <th key={column}>{column.replace(/_/g, " ")}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.slice(page * 50, page * 50 + 50).map((row, index) => (
              <tr key={index}>
                {data.columns.map((column) => (
                  <td key={column}>{display(row[column])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="table-footer">
        <button disabled={page === 0} onClick={() => setPage(page - 1)}>
          Previous
        </button>
        <span>{rows.length.toLocaleString()} loaded rows</span>
        <button
          disabled={(page + 1) * 50 >= rows.length}
          onClick={() => setPage(page + 1)}
        >
          Next
        </button>
      </div>
    </div>
  );
}
function optionFor(
  chart: Chart,
  data: ChartData,
  dark: boolean,
  design: DashboardDesign = {},
  explicit: Encoding = {},
): EChartsOption | null {
  const { rows, columns, form_data: form } = data;
  const viz = explicit.chart_type || chart.viz_type;
  if (!rows.length || viz === "table" || viz.startsWith("big_number"))
    return null;
  const encoding=resolveEncoding(columns, rows, form, explicit);
  const {dimension,metrics}=encoding;
  if (!metrics.length) return null;
  const text = dark ? "#e4eaf3" : "#324359";
  const common: EChartsOption = {
    color: design.palette || designTokens.palette,
    tooltip: { trigger: "axis" },
    textStyle: { color: text, fontFamily: designTokens.font },
    grid: { left: 55, right: 24, top: 36, bottom: 64 },
    legend: { show: typeof form.show_legend === "boolean" ? form.show_legend : metrics.length > 1, textStyle: { color: text }, type: "scroll" },
  };
  if (viz === "pie" || viz === "donut")
    return {
      ...common,
      tooltip: { trigger: "item" },
      series: [
        {
          type: "pie",
          radius: ["45%", "65%"],
          data: rows.map((row) => ({
            name: display(row[dimension]),
            value: number(row[metrics[0]]) || 0,
            itemStyle: {color: responseColor(row[dimension], rows.indexOf(row), design)},
          })),
          label: { color: text },
        },
      ],
    };
  if (viz.includes("line") || viz.includes("area") || viz.includes("bar")) {
    const horizontal = form.orientation === "horizontal";
    const pivot=pivotRows(rows,encoding);
    const categories = pivot.keys;
    const category = {
      type: "category" as const,
      data: categories,
      axisLabel: { color: text, hideOverlap: true },
    };
    const value = {
      type: "value" as const,
      axisLabel: { color: text, formatter:(value:number)=>formatValue(value,encoding.number_format) },
      name:encoding.unit || "",
      splitLine: { lineStyle: { color: dark ? "#2f3b50" : "#e7edf3" } },
    };
    return {
      ...common,
      xAxis: horizontal ? value : encoding.time ? {type:"time",axisLabel:{color:text}} : category,
      yAxis: horizontal ? category : value,
      series: pivot.series.map((series) => ({
        name: series.name.replace(/_/g, " "),
        type: viz.includes("bar") ? ("bar" as const) : ("line" as const),
        data: encoding.time && !horizontal ? pivot.keys.map((key,index)=>[key,series.values[index]]) : series.values,
        stack:encoding.stack?"total":undefined,
        connectNulls:false,
        ...(viz.includes("area") ? { areaStyle: {} } : {}),
      })),
    };
  }
  if (viz.includes("scatter") && metrics.length >= 2)
    return {
      ...common,
      xAxis: { type: "value", name: metrics[0] },
      yAxis: { type: "value", name: metrics[1] },
      series: [
        {
          type: "scatter",
          data: rows.map((row) => [
            number(row[metrics[0]]),
            number(row[metrics[1]]),
          ]),
        },
      ],
    };
  return null;
}
function Visual({
  chart,
  data,
  dark,
  encoding = {},
}: {
  encoding?: Encoding;
  chart: Chart;
  data: ChartData;
  dark: boolean;
}) {
  const container = useRef<HTMLDivElement>(null);
  const parent = React.useContext(DesignContext), override=parent.chart_styles?.[`chart:${chart.id}`] || {}, design={...parent,...override,colors:{...parent.colors,...override.colors}};
  let option:EChartsOption|null=null, encodingError="";
  try { option = optionFor(chart, data, dark, design, encoding); } catch(error) { encodingError=(error as Error).message; }
  const dimension = typeof data.form_data.x_axis === "string" ? data.form_data.x_axis : (data.form_data.groupby as string[] | undefined)?.[0] || data.columns[0];
  const metric = data.columns.find(c => c !== dimension && data.rows.some(r => number(r[c]) !== null));
  const binary = (chart.viz_type === "pie" || chart.viz_type.includes("bar")) && metric && data.rows.every(r => r[dimension] == null || ["yes","no","y","n","true","false","unknown","not recorded"].includes(String(r[dimension]).toLowerCase()));
  useEffect(()=>{if(encoding.chart_type!=="map" || !container.current)return;return mountQueryMap(container.current,data.rows,encoding);},[data,JSON.stringify(encoding)]);
  useEffect(() => {
    if (!container.current || encoding.chart_type === "map" || !option || binary) return undefined;
    const instance = echarts.init(container.current);
    instance.setOption(option);
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(container.current);
    return () => {
      observer.disconnect();
      instance.dispose();
    };
  }, [data, chart.viz_type, dark, design, binary]);
  if(encoding.chart_type === "map") return <div className="chart-plot" role="img" aria-label={chart.title} ref={container}/>;
  if (encodingError) return <div className="error" role="alert">{encodingError}</div>;
  if (binary && metric && !encoding.series && !encoding.chart_type) return <DesignContext.Provider value={design}><ResponseDonut title={chart.title} values={data.rows.map(r=>({label:r[dimension],value:number(r[metric]) || 0}))}/></DesignContext.Provider>;
  if (!data.rows.length)
    return <div className="empty">No results for this chart.</div>;
  if (chart.viz_type.startsWith("big_number") || encoding.chart_type === "kpi") {
    const metric = encoding.metrics?.[0] ||
      data.columns.find((column) => number(data.rows[0][column]) !== null) ||
      data.columns[0];
    const value = number(data.rows[0][metric]);
    return (
      <div className="kpi">
        <strong>
          {value === null
            ? display(data.rows[0][metric])
            : formatValue(value,encoding.number_format,encoding.unit)}
        </strong>
        <span>{metric.replace(/_/g, " ")}</span>
      </div>
    );
  }
  if (!option)
    return (
      <>
        <Table data={data} />
        {chart.viz_type !== "table" && (
          <small className="chart-note">
            Showing the chart's data as a table. A custom renderer for this
            chart type is not available yet.
          </small>
        )}
      </>
    );
  return (
    <div
      className="chart-plot"
      role="img"
      aria-label={chart.title}
      ref={container}
    />
  );
}
function Card({
  publication,
  chart,
  item,
  edit,
  refresh,
  update,
  criteria = "{}",
}: {
  publication: Publication;
  chart: Chart;
  item: Layout;
  edit: boolean;
  refresh: number;
  update: (item: Layout) => void;
  criteria?: string;
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: chart.id, disabled: !edit });
  const [data, setData] = useState<ChartData>();
  const [error, setError] = useState("");
  const scopedCriteria = JSON.stringify(Object.fromEntries(Object.entries(JSON.parse(criteria) as Record<string,string[]>).filter(([field]) =>
    getComposition(publication)?.filters?.some(filter => filter.field === field && filter.chart_ids.includes(chart.id)))));
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    setData(undefined);
    request<ChartData>(`${api}${publication.id}/charts/${chart.id}/?criteria=${encodeURIComponent(scopedCriteria)}`, {
      signal: controller.signal,
    })
      .then(setData)
      .catch((err) => {
        if (err.name !== "AbortError") setError(err.message);
      });
    return () => controller.abort();
  }, [publication.id, chart.id, refresh, scopedCriteria]);
  return (
    <section
      ref={setNodeRef}
      className={`chart-card ${isDragging ? "dragging" : ""}`}
      style={{
        gridColumn: `span ${item.span}`,
        height: item.height,
        transform: CSS.Transform.toString(transform),
        transition,
      }}
    >
      <div className="card-header">
        <h2>{chart.title}</h2>
        {edit && (
          <button
            className="drag-handle"
            {...attributes}
            {...listeners}
            aria-label={`Move ${chart.title}`}
            title="Drag to move; Space then arrow keys to move by keyboard"
          >
            ⠿
          </button>
        )}
      </div>
      {edit && (
        <div className="card-controls">
          <label>
            Width{" "}
            <select
              aria-label={`Width of ${chart.title}`}
              value={item.span}
              onChange={(event) =>
                update({ ...item, span: Number(event.target.value) })
              }
            >
              <option value={4}>Third</option>
              <option value={6}>Half</option>
              <option value={12}>Full</option>
            </select>
          </label>
          <label>
            Height{" "}
            <select
              aria-label={`Height of ${chart.title}`}
              value={item.height}
              onChange={(event) =>
                update({ ...item, height: Number(event.target.value) })
              }
            >
              {[240, 320, 480, 640].map((height) => (
                <option key={height} value={height}>
                  {height}px
                </option>
              ))}
            </select>
          </label>
        </div>
      )}
      <div className="card-body">
        {error ? (
          <div className="error">{error}</div>
        ) : data ? (
          <Visual
            chart={chart}
            data={data}
            dark={publication.theme === "dark"}
            encoding={getComposition(publication)?.encodings?.[String(chart.id)]}
          />
        ) : (
          <div className="empty">Loading live data…</div>
        )}
      </div>
      {data && data.rows.length >= data.row_limit && (
        <small className="chart-note">
          The saved query is limited to {data.row_limit.toLocaleString()} rows.
        </small>
      )}
    </section>
  );
}
function Viewer() {
  const [criteria,setCriteria] = useState<Record<string,string[]>>({});
  const [viewportPage,setViewportPage]=useState(0);
  const [viewport,setViewport]=useState({width:window.innerWidth,height:window.innerHeight});
  useEffect(()=>{const resize=()=>setViewport({width:window.innerWidth,height:window.innerHeight});window.addEventListener("resize",resize);return()=>window.removeEventListener("resize",resize);},[]);
  const [publication, setPublication] = useState<Publication>();
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [edit, setEdit] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  );
  useEffect(() => {
    request<Publication>(`${api}${publicationId}/`)
      .then((value) => {
        setPublication(value);
        (
          window as Window & { __lifedataDashboardContext?: object }
        ).__lifedataDashboardContext = {
          page_type: "dashboard",
          dashboard_id: value.dashboard_id,
          dashboard_surface: "custom",
          publication_id: value.id,
        };
        window.dispatchEvent(new Event("lifedata:context"));
      })
      .catch((err) => setError(err.message));
  }, []);
  useEffect(()=>{
    const update=(event:Event)=>{const detail=(event as CustomEvent<{publication_id:string}>).detail;if(detail?.publication_id!==publicationId)return;
      request<Publication>(`${api}${publicationId}/`).then(value=>{setPublication(value);setRefresh(n=>n+1);}).catch(err=>setStatus(err.message));};
    window.addEventListener("lifedata:publication-updated",update);return()=>window.removeEventListener("lifedata:publication-updated",update);
  },[]);
  function save(layout: Layout[]) {
    if (!publication) return;
    setPublication({ ...publication, layout });
    setStatus("Saving layout…");
    queue.current = queue.current
      .catch(() => undefined)
      .then(() =>
        request<Publication>(`${api}${publication.id}/`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ layout }),
        }),
      )
      .then(() => setStatus("Layout saved"))
      .catch((err) => setStatus(`Layout not saved: ${err.message}`));
  }
  function dropped(event: DragEndEvent) {
    if (!publication || !event.over || event.active.id === event.over.id)
      return;
    const from = publication.layout.findIndex(
      (item) => item.chart_id === event.active.id,
    );
    const to = publication.layout.findIndex(
      (item) => item.chart_id === event.over!.id,
    );
    if (from >= 0 && to >= 0) save(arrayMove(publication.layout, from, to));
  }
  const [preferredTheme, setPreferredTheme] = useState<"light" | "dark" | null>(() => {
    try {
      const value = localStorage.getItem(`lifedata:dashboard-theme:${publicationId}`);
      return value === "light" || value === "dark" ? value : null;
    } catch { return null; }
  });
  const theme: "light" | "dark" = (preferredTheme || publication?.theme) === "dark" ? "dark" : "light";
  const themeControl = <button type="button" aria-label="Dark mode" aria-pressed={theme === "dark"}
    onClick={() => {
      const next = theme === "dark" ? "light" : "dark";
      setPreferredTheme(next);
      try { localStorage.setItem(`lifedata:dashboard-theme:${publicationId}`, next); } catch {}
    }}>{theme === "dark" ? "☀ Light mode" : "☾ Dark mode"}</button>;
  if ((publication?.configuration as { preset?: string } | undefined)?.preset === "hdss-master")
    return <MasterTemplate publicationId={publication!.id} title={publication!.title} canEdit={publication!.can_edit} />;
  if ((publication?.configuration as { preset?: string } | undefined)?.preset === "hdss-site")
    return <HdssTemplate publicationId={publication!.id} title={publication!.title} canEdit={publication!.can_edit} />;
  if (publication?.configuration?.preset === "buikwe-workspace")
    return <BuikweWorkspace publication={publication} theme={theme} themeControl={themeControl}/>;
  if (publication?.configuration?.preset === "population-discovery")
    return <Population id={publication.id} title={publication.title}
      config={publication.configuration as PopulationConfiguration} theme={theme} themeControl={themeControl} />;
  if (publication?.configuration?.preset === "facility-network")
    return (
      <Facilities
        id={publication.id}
        title={publication.title}
        theme={theme}
        themeControl={themeControl}
        config={publication.configuration as FacilityConfiguration}
      />
    );
  if(publication?.configuration?.preset === "reference-template")return <main className="reference-publication"><div className="reference-publication-bar"><a href="/lifedata-ai/dashboards/">Dashboards</a><strong>{publication.title}</strong><span className={`quality-badge ${publication.quality?.status === "verified" ? "verified" : "draft"}`}>{publication.quality?.status === "verified" ? "Verified" : "Draft"}</span><button onClick={()=>setRefresh(refresh+1)}>Refresh</button></div><iframe key={refresh} title={publication.title} className="reference-publication-frame" src={`${api}${publication.id}/template/`} /></main>;
  const viewportMode = publication?.configuration?.preset === "composed-dashboard" && getComposition(publication)?.layout_mode !== "flow" && !edit;
  const visibleLayout=(publication?.layout || []).filter(item=>!(publication?.configuration?.design?.groups || []).some(group=>group.chart_ids.slice(1).includes(item.chart_id)));
  const viewportColumns=viewport.width>=1000?2:1;
  const viewportRows=viewport.height>=880?2:1;
  const pageCapacity=viewportColumns*viewportRows;
  const pageCount=viewportMode?Math.max(1,Math.ceil(visibleLayout.length/pageCapacity)):1;
  const currentPage=Math.min(viewportPage,pageCount-1);
  const pageLayout=viewportMode?visibleLayout.slice(currentPage*pageCapacity,(currentPage+1)*pageCapacity):visibleLayout;
  const chartHeight=Math.max(240,Math.floor((viewport.height-(viewport.width<800?320:270)-(viewportRows-1)*10)/viewportRows));
  const charts = new Map(
    publication?.charts.map((chart) => [chart.id, chart]) || [],
  );
  return (
    <DesignContext.Provider value={publication?.configuration?.design || {}}><main className={`dashboard-shell vima-baseline ${viewportMode ? "vima-viewport" : ""} ${publication?.configuration?.preset === "composed-dashboard" ? `vima-adaptive skin-${getComposition(publication)?.skin || "navy-teal"}` : ""} ${theme}`} style={{"--vima-accent":publication?.configuration?.design?.colors?.accent || designTokens.accent} as React.CSSProperties}>
      <header className="dashboard-header">
        <div>
          <a className="brand" href="/lifedata-ai/dashboards/">
            LifeData<span>Dashboards</span>
          </a>
          <h1>{publication?.title || "Dashboard"} <span className={`quality-badge ${publication?.quality?.status === "verified" ? "verified" : "draft"}`}>{publication?.quality?.status === "verified" ? "Verified" : "Draft"}</span></h1>
          <p>{publication?.configuration?.preset === "composed-dashboard" ? getComposition(publication)?.subtitle || "Live insights · Built with Vima" : "Live insights · Built with Vima"} · <a href="/lifedata-ai/dashboards/?design-library=1">Design library</a></p>
        </div>
        <nav>
          {themeControl}
          <button onClick={() => setRefresh(refresh + 1)}>
            Refresh data
          </button>
          <button
            onClick={() =>
              navigator.clipboard
                .writeText(location.href)
                .then(() => setStatus("Link copied"))
                .catch(() => setStatus("Copy this page URL to share it."))
            }
          >
            Copy link
          </button>
          {publication?.can_edit && (
            <button className="primary" onClick={() => setEdit(!edit)}>
              {edit ? "Finish editing" : "Edit layout"}
            </button>
          )}
          <a href="/superset/dashboard/">Superset</a>
        </nav>
      </header>
      <div className="save-status" role="status">
        {status}
      </div>
      {error && <div className="error">{error}</div>}
      {publication?.configuration?.preset === "composed-dashboard" && <AdaptiveFilters publication={publication} criteria={criteria} setCriteria={setCriteria} refresh={refresh}/>}
      {viewportMode && <nav className="viewport-pages" aria-label="Dashboard views">{Array.from({length:pageCount},(_,i)=><button key={i} aria-current={currentPage===i} onClick={()=>setViewportPage(i)}>View {i+1}</button>)}<span>{visibleLayout.length} charts · Readable views</span></nav>}
      {publication && (
        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          onDragEnd={dropped}
        >
          <SortableContext
            items={publication.layout.map((item) => item.chart_id)}
            strategy={rectSortingStrategy}
          >
            <div className="dashboard-grid">
              {pageLayout
                .filter((item) => charts.has(item.chart_id))
                .map((item) => (
                  <React.Fragment key={item.chart_id}>
                  {!viewportMode && publication.configuration?.preset === "composed-dashboard" && getComposition(publication)?.sections?.filter(s=>s.chart_ids[0]===item.chart_id).map(s=><h2 key={s.title} className="composition-section">{s.title}</h2>)}
                  <GroupedCard
                    publication={{ ...publication, theme }}
                    group={publication.configuration?.design?.groups?.find(group=>group.chart_ids[0]===item.chart_id)}
                    chart={charts.get(item.chart_id)!}
                    item={viewportMode?{...item,span:12/viewportColumns,height:chartHeight-(publication.configuration?.design?.groups?.some(g=>g.chart_ids[0]===item.chart_id)?72:0)}:item}
                    edit={edit}
                    refresh={refresh}
                    criteria={JSON.stringify(criteria)}
                    update={(changed) =>
                      save(
                        publication.layout.map((value) =>
                          value.chart_id === changed.chart_id ? changed : value,
                        ),
                      )
                    }
                  />
                  </React.Fragment>
                ))}
            </div>
          </SortableContext>
        </DndContext>
      )}
    </main></DesignContext.Provider>
  );
}

function AdaptiveFilters({publication,criteria,setCriteria,refresh}:{publication:Publication;criteria:Record<string,string[]>;setCriteria:(v:Record<string,string[]>)=>void;refresh:number}) {
 const [filters,setFilters]=useState<{field:string;label:string;values:string[];truncated:boolean;scope_count:number}[]>([]),[error,setError]=useState("");
 const composition=publication.configuration?.preset === "composed-dashboard" ? getComposition(publication) : undefined;
 useEffect(()=>{if(!composition?.filters?.length)return;const controller=new AbortController();setError("");request<{filters:typeof filters}>(`${api}${publication.id}/filter-options/?criteria=${encodeURIComponent(JSON.stringify(criteria))}`,{signal:controller.signal}).then(v=>setFilters(v.filters)).catch(e=>{if(e.name!=="AbortError")setError(e.message);});return()=>controller.abort();},[publication.id,refresh,JSON.stringify(criteria)]);
 useEffect(()=>{syncDashboardFilters(Object.entries(criteria).map(([column,values])=>({column,operator:"IN",values})));},[JSON.stringify(criteria)]);
 if(!composition?.filters?.length)return null;
 return <section className="composition-filters" aria-label="Dashboard filters">{error&&<p role="alert">{error}</p>}{!filters.length&&!error&&<span>Loading filters…</span>}{filters.map(f=><label key={f.field}>{f.label}<select aria-label={f.label} value={criteria[f.field]?.[0] || ""} onChange={e=>{const next={...criteria};if(e.target.value)next[f.field]=[e.target.value];else delete next[f.field];setCriteria(next);}}><option value="">All values</option>{f.values.map(v=><option key={v} value={v}>{v}</option>)}</select><small>{f.scope_count} charts{f.truncated?" · first 200 options":""}</small></label>)}<button onClick={()=>setCriteria({})} disabled={!Object.keys(criteria).length}>Reset filters</button></section>;
}

function GroupedCard(props: React.ComponentProps<typeof Card> & {group?: {title:string;chart_ids:number[]}}) {
 const [active,setActive]=useState(props.group?.chart_ids[0] || props.chart.id);
 if (!props.group) return <Card {...props}/>;
 const tabs=props.group.chart_ids.map(id=>({id,label:props.publication.charts.find(c=>c.id===id)?.title || `Chart ${id}`}));
 const chart=(props.group.chart_ids.includes(active) && props.publication.charts.find(c=>c.id===active)) || props.chart;
 return <section className="vima-group" style={{gridColumn:`span ${props.item.span}`,height:props.item.height+72}}><h2 className="vima-group-title">{props.group.title}</h2><ChartTabs title={props.group.title} tabs={tabs} active={chart.id} select={setActive}/><div role="tabpanel" aria-label={chart.title} style={{minHeight:0,display:"flex",flex:1}}><Card {...props} chart={chart} item={{...props.item,chart_id:chart.id}} edit={false}/></div></section>;
}

function timeGreeting(): string {
  const hour = new Date().getHours();
  if (hour < 5) return "Working late";
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}
function initials(name: string): string {
  const cleaned = name.replace(/[._-]+/g, " ").trim();
  const parts = cleaned.split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  return parts.length === 1
    ? parts[0].slice(0, 2).toUpperCase()
    : (parts[0][0] + parts[1][0]).toUpperCase();
}
type CreatableItem = { id: number; title: string; already_published: boolean };
function Creator({
  onClose,
  onPublished,
}: {
  onClose: () => void;
  onPublished: (url: string) => void;
}) {
  const [items, setItems] = useState<CreatableItem[]>();
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<CreatableItem>();
  const [title, setTitle] = useState("");
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    request<{ items: CreatableItem[] }>(`${api}creatable/`)
      .then((value) => setItems(value.items))
      .catch((err) => setError(err.message));
  }, []);
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  function publish() {
    if (!selected || !title.trim() || busy) return;
    setBusy(true);
    setError("");
    request<Publication>(`${api}create/`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dashboard_id: selected.id,
        title: title.trim(),
        theme,
      }),
    })
      .then((publication) => onPublished(publication.url))
      .catch((err) => {
        setError(err.message);
        setBusy(false);
      });
  }
  const filtered = (items || []).filter(
    (item) =>
      !query.trim() ||
      item.title.toLowerCase().includes(query.trim().toLowerCase()),
  );
  return (
    <div className="creator-overlay" onClick={onClose}>
      <aside
        className="creator-panel"
        role="dialog"
        aria-label="Publish a dashboard"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="creator-head">
          <h2>Publish a dashboard</h2>
          <button
            className="creator-close"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        </header>
        {!selected ? (
          <>
            <p className="creator-lead">
              Pick a dashboard you can already see in Superset. Vima will give
              it its own page here.
            </p>
            <div className="creator-search">
              <SearchIcon />
              <input
                autoFocus
                aria-label="Search your Superset dashboards"
                placeholder="Search your Superset dashboards…"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </div>
            <div className="creator-list">
              {items === undefined ? (
                <div className="empty">Loading your dashboards…</div>
              ) : error ? (
                <div className="error">{error}</div>
              ) : filtered.length ? (
                filtered.map((item) => (
                  <button
                    key={item.id}
                    className="creator-row"
                    onClick={() => {
                      setSelected(item);
                      setTitle(item.title);
                    }}
                  >
                    <span>{item.title}</span>
                    {item.already_published && (
                      <span className="creator-tag">Already published</span>
                    )}
                  </button>
                ))
              ) : (
                <div className="empty">No matching dashboards in Superset.</div>
              )}
            </div>
          </>
        ) : (
          <div className="creator-form">
            <button
              className="creator-back"
              onClick={() => setSelected(undefined)}
            >
              ‹ Choose a different dashboard
            </button>
            <label className="creator-field">
              Title
              <input
                value={title}
                maxLength={255}
                onChange={(event) => setTitle(event.target.value)}
              />
            </label>
            <div className="creator-field">
              Theme
              <div className="theme-toggle">
                <button
                  type="button"
                  className={theme === "light" ? "active" : ""}
                  onClick={() => setTheme("light")}
                >
                  Light
                </button>
                <button
                  type="button"
                  className={theme === "dark" ? "active" : ""}
                  onClick={() => setTheme("dark")}
                >
                  Dark
                </button>
              </div>
            </div>
            {selected.already_published && (
              <p className="creator-note">
                You've already published this dashboard — publishing again
                updates it.
              </p>
            )}
            {error && <div className="error">{error}</div>}
            <button
              className="home-cta"
              disabled={busy || !title.trim()}
              onClick={publish}
            >
              {busy
                ? "Publishing…"
                : selected.already_published
                  ? "Update dashboard"
                  : "Publish dashboard"}
            </button>
          </div>
        )}
      </aside>
    </div>
  );
}
function Home() {
  const [library, setLibrary] = useState<Publication[]>();
  const [who, setWho] = useState<{ display_name: string }>();
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<"updated" | "name">("updated");
  const [creatorOpen, setCreatorOpen] = useState(false);
  const [hdssOpen, setHdssOpen] = useState(false);
  function reload() {
    request<{ items: Publication[] }>(api)
      .then((value) => setLibrary(value.items))
      .catch((err) => setError(err.message));
  }
  useEffect(() => {
    reload();
    request<{ display_name: string }>("/lifedata-ai/api/whoami/")
      .then(setWho)
      .catch(() => undefined);
  }, []);
  const visible = (library || [])
    .filter(
      (item) =>
        !query.trim() ||
        item.title.toLowerCase().includes(query.trim().toLowerCase()),
    )
    .sort((a, b) =>
      sort === "name"
        ? a.title.localeCompare(b.title)
        : (b.updated_at || "").localeCompare(a.updated_at || ""),
    );
  const totalCharts = (library || []).reduce(
    (sum, item) => sum + (item.chart_count ?? item.layout.length),
    0,
  );
  const today = new Date().toDateString();
  const updatedToday = (library || []).filter(
    (item) => item.updated_at && new Date(item.updated_at).toDateString() === today,
  ).length;
  const firstName = who?.display_name?.trim().split(/\s+/)[0];
  const heroLine =
    library === undefined
      ? "Loading your dashboards…"
      : library.length === 0
        ? "You haven't published anything yet."
        : `You have ${library.length} dashboard${library.length === 1 ? "" : "s"}.` +
          (updatedToday ? ` ${updatedToday} updated today.` : "");
  return (
    <main className="dashboard-shell home">
      <section className="home-hero">
        <div className="home-hero-top">
          <a className="home-wordmark" href="/lifedata-ai/dashboards/">
            LifeData
          </a>
          <a className="home-hero-link" href="/superset/dashboard/">
            Superset
          </a>
        </div>
        <div className="home-hero-main">
          <div>
            <h1>
              {timeGreeting()}
              {firstName ? `, ${firstName}` : ""}.
            </h1>
            <p>{heroLine}</p>
          </div>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            <button className="home-cta" onClick={() => setCreatorOpen(true)}>
              + New dashboard
            </button>
            <button className="home-cta" onClick={() => setHdssOpen(true)}>
              + New HDSS dashboard
            </button>
          </div>
        </div>
        {!!library?.length && (
          <div className="home-stats">
            <div>
              <strong>{library.length}</strong>
              <span>{library.length === 1 ? "dashboard" : "dashboards"}</span>
            </div>
            <div>
              <strong>{totalCharts}</strong>
              <span>{totalCharts === 1 ? "chart" : "charts"}</span>
            </div>
            <div>
              <strong>{updatedToday}</strong>
              <span>updated today</span>
            </div>
          </div>
        )}
      </section>
      <section className="home-canvas">
        {error && <div className="error">{error}</div>}
        {library === undefined ? (
          <div className="library-grid">
            {[0, 1, 2].map((key) => (
              <div
                className="library-card skeleton"
                key={key}
                aria-hidden="true"
              />
            ))}
          </div>
        ) : library.length ? (
          <>
            <div className="library-toolbar">
              <div className="library-search">
                <SearchIcon />
                <input
                  aria-label="Search dashboards"
                  placeholder="Search your dashboards…"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                />
              </div>
              <label className="library-sort">
                Sort by
                <select
                  aria-label="Sort dashboards"
                  value={sort}
                  onChange={(event) =>
                    setSort(event.target.value as "updated" | "name")
                  }
                >
                  <option value="updated">Recently updated</option>
                  <option value="name">Name A–Z</option>
                </select>
              </label>
            </div>
            {visible.length ? (
              <div className="library-grid">
                {visible.map((item) => {
                  const count = item.chart_count ?? item.layout.length;
                  return (
                    <a
                      className="library-card"
                      key={item.id}
                      href={item.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={
                        { "--accent": accentFor(item.id) } as React.CSSProperties
                      }
                    >
                      <span className="card-spine" aria-hidden="true" />
                      <div className="card-main">
                        <div className="card-top">
                          <h2>{item.title}</h2>
                          <span
                            className="card-avatar"
                            title={item.owner || ""}
                          >
                            {initials(item.owner || item.title)}
                          </span>
                        </div>
                        <p className="card-meta">
                          {count} chart{count === 1 ? "" : "s"} ·{" "}
                          {item.theme === "dark" ? "dark" : "light"}
                        </p>
                        <p className="card-updated">
                          <ClockIcon /> Updated {relativeTime(item.updated_at)}
                        </p>
                      </div>
                    </a>
                  );
                })}
              </div>
            ) : (
              <div className="empty">No dashboards match “{query}”.</div>
            )}
          </>
        ) : (
          !error && (
            <div className="empty-state">
              <h2>Nothing published yet</h2>
              <p>
                Publish a dashboard you already have in Superset, or ask Vima
                to build one from scratch.
              </p>
              <div className="empty-state-actions">
                <button
                  className="home-cta"
                  onClick={() => setCreatorOpen(true)}
                >
                  + New dashboard
                </button>
                <button onClick={() => window.__lifedataCopilot?.open()}>
                  Ask Vima
                </button>
              </div>
            </div>
          )
        )}
      </section>
      {hdssOpen && <HdssCreator onClose={() => setHdssOpen(false)} />}
      {creatorOpen && (
        <Creator
          onClose={() => setCreatorOpen(false)}
          onPublished={(url) => {
            location.href = url;
          }}
        />
      )}
    </main>
  );
}
ReactDOM.render(new URLSearchParams(location.search).has("design-library") ? <DesignLibrary /> : publicationId ? <Viewer /> : <Home />, root);
