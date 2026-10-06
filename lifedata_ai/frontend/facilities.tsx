import { DashboardDesign, DesignContext, responseColor, syncDashboardFilters } from "./design-system";
import React, { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import * as echarts from "echarts/core";
import { PieChart, BarChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import { SVGRenderer } from "echarts/renderers";
import type { EChartsOption } from "echarts";
import "leaflet/dist/leaflet.css";
import "./facilities.css";

type Row = Record<string, unknown>;
echarts.use([PieChart, BarChart, GridComponent, TooltipComponent, SVGRenderer]);
type ServiceFilter = { field: string; response: string };
const responseValue = (value: unknown) => {
  const valueText = String(value ?? "")
    .trim()
    .toLowerCase();
  return ["y", "yes", "true", "1"].includes(valueText)
    ? "Yes"
    : ["n", "no", "false", "0"].includes(valueText)
      ? "No"
      : "Not recorded";
};
export type FacilityConfiguration = {
  design?: DashboardDesign;
  preset: string;
  source_chart_id: number;
  network_chart_id?: number;
  region_label?: string;
  map_bounds?: [number, number, number, number];
};
const fields = ["pcn", "hub", "ownership", "hf_level"];
const labels = ["PCN", "Hub", "Ownership", "Level"];
const services = [
  ["maternal_childhealth", "Maternal & child health"],
  ["hiv_tb", "HIV & TB"],
  ["labaratory", "Laboratory"],
  ["ambulance", "Ambulance"],
  ["dental", "Dental"],
  ["surgery", "Surgery"],
  ["radiology", "Radiology"],
  ["specialised", "Specialist services"],
  ["optical", "Optical"],
  ["theatre", "Theatre"],
  ["mortuary", "Mortuary"],
];
const colors = ["#14a68c", "#577be7", "#db9c36", "#ad71c9", "#8795a9"];
const text = (v: unknown) => String(v ?? "").trim() || "Not recorded";
const nameKey = (v: unknown) =>
  String(v ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");
async function load(id: string, chart: number): Promise<Row[]> {
  const response = await fetch(
    `/lifedata-ai/api/publications/${id}/charts/${chart}/`,
    { credentials: "same-origin" },
  );
  const body = await response.json();
  if (!response.ok)
    throw new Error(body.error || "Unable to load facility data");
  return body.rows;
}
function MapView({
  rows,
  pcns,
  bounds,
}: {
  rows: Row[];
  pcns: string[];
  bounds?: [number, number, number, number];
}) {
  const [allCoordinates, setAllCoordinates] = useState(false);
  const [tileError, setTileError] = useState(false);
  const host = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map>();
  const layer = useRef<L.LayerGroup>();
  useEffect(() => {
    if (!host.current) return;
    map.current = L.map(host.current, { zoomControl: true }).setView([0, 0], 2);
    const tiles = L.tileLayer(
      "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      {
        attribution:
          '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
        maxZoom: 19,
      },
    ).addTo(map.current);
    tiles.on("tileerror", () => setTileError(true));
    tiles.on("tileload", () => setTileError(false));
    layer.current = L.layerGroup().addTo(map.current);
    const observer = new ResizeObserver(() => map.current?.invalidateSize());
    observer.observe(host.current);
    return () => {
      observer.disconnect();
      map.current?.remove();
    };
  }, []);
  useEffect(() => {
    if (!layer.current || !map.current) return;
    layer.current.clearLayers();
    const points: L.LatLngTuple[] = [];
    rows.forEach((row) => {
      const lat = Number(row.lat),
        lon = Number(row.lon);
      if (
        row.lat == null ||
        row.lon == null ||
        !Number.isFinite(lat) ||
        !Number.isFinite(lon) ||
        Math.abs(lat) > 90 ||
        Math.abs(lon) > 180
      )
        return;
      points.push([lat, lon]);
      const color =
        colors[Math.max(0, pcns.indexOf(text(row.pcn))) % colors.length];
      const popup = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = text(row.name_facility);
      popup.append(title);
      const detail = document.createElement("p");
      detail.textContent = `${text(row.hf_level)} · ${text(row.ownership)}\n${text(row.pcn)} · Hub: ${text(row.hub)}`;
      popup.append(detail);
      L.circleMarker([lat, lon], {
        radius: 7,
        color: "#fff",
        weight: 2,
        fillColor: color,
        fillOpacity: 0.9,
      })
        .bindPopup(popup)
        .addTo(layer.current!);
    });
    const countyPoints = bounds
      ? points.filter(
          ([lat, lon]) =>
            lat >= bounds[0] &&
            lat <= bounds[2] &&
            lon >= bounds[1] &&
            lon <= bounds[3],
        )
      : points;
    const extent = allCoordinates ? points : countyPoints;
    if (extent.length)
      map.current.fitBounds(L.latLngBounds(extent).pad(0.15), { maxZoom: 13 });
    else if (bounds)
      map.current.fitBounds([
        [bounds[0], bounds[1]],
        [bounds[2], bounds[3]],
      ]);
  }, [rows, pcns, allCoordinates, bounds]);
  const outside = bounds
    ? rows.filter(
        (row) =>
          !(
            Number(row.lat) >= bounds[0] &&
            Number(row.lat) <= bounds[2] &&
            Number(row.lon) >= bounds[1] &&
            Number(row.lon) <= bounds[3]
          ),
      ).length
    : 0;
  return (
    <>
      {tileError && (
        <div className="hf-map-warning" role="status">
          Basemap tiles unavailable. Facility markers remain interactive.
        </div>
      )}
      <div
        ref={host}
        className="hf-map"
        aria-label={`Map of ${rows.length} facility survey records`}
      />
      <div className="hf-map-extent">
        <span>
          {outside
            ? `${outside} coordinates outside default map extent`
            : "All selected coordinates within default extent"}
        </span>
        <button onClick={() => setAllCoordinates(!allCoordinates)}>
          {allCoordinates ? "Default view" : "Fit all coordinates"}
        </button>
      </div>
    </>
  );
}
function InteractiveChart({
  option,
  onSelect,
  label,
}: {
  option: EChartsOption;
  onSelect: (name: string) => void;
  label: string;
}) {
  const host = useRef<HTMLDivElement>(null);
  const instance = useRef<echarts.ECharts>();
  const callback = useRef(onSelect);
  callback.current = onSelect;
  useEffect(() => {
    if (!host.current) return;
    instance.current = echarts.init(host.current, undefined, {
      renderer: "svg",
    });
    instance.current.on("click", (event: { name: string }) =>
      callback.current(event.name),
    );
    const observer = new ResizeObserver(() => instance.current?.resize());
    observer.observe(host.current);
    return () => {
      observer.disconnect();
      instance.current?.dispose();
    };
  }, []);
  useEffect(() => {
    instance.current?.setOption(option, { notMerge: true });
  }, [option]);
  return (
    <div
      className="hf-interactive-chart"
      ref={host}
      role="img"
      aria-label={label}
    />
  );
}
function ServicePie({
  rows,
  field,
  label,
  selected,
  onSelect,
}: {
  rows: Row[];
  field: string;
  label: string;
  selected?: ServiceFilter;
  onSelect: (response: string) => void;
}) {
  const parent=React.useContext(DesignContext),override=parent.chart_styles?.[`field:${field}`] || {},design={...parent,...override,colors:{...parent.colors,...override.colors}};
  const values = ["Yes", "No", "Not recorded"].map((name, i) => ({
    name,
    value: rows.filter((r) => responseValue(r[field]) === name).length,
    itemStyle: { color: responseColor(name,i,design) },
  }));
  const yes = values[0].value,
    no = values[1].value,
    unknown = values[2].value;
  const option = useMemo<EChartsOption>(
    () => ({
      animationDurationUpdate: 250,
      tooltip: { trigger: "item", confine: true, formatter: "{b}: {c} ({d}%)" },
      series: [
        {
          type: "pie",
          radius: ["54%", "86%"],
          center: ["50%", "50%"],
          avoidLabelOverlap: true,
          label: { show: false },
          emptyCircleStyle: { color: "#edf2f7" },
          emphasis: {
            scaleSize: 3,
            itemStyle: { shadowBlur: 8, shadowColor: "#16345020" },
          },
          data: values,
        },
      ],
    }),
    [yes, no, unknown, design],
  );
  return (
    <article
      className={`hf-service-pie ${selected?.field === field ? "selected" : ""}`}
      data-service={field}
    >
      <h3>{label}</h3>
      <div className="hf-donut">
        <InteractiveChart
          option={option}
          onSelect={onSelect}
          label={`${label}: Yes ${yes}, No ${no}, Not recorded ${unknown}`}
        />
        <span>
          {rows.length ? Math.round((yes / rows.length) * 100) : 0}
          <small>% yes</small>
        </span>
      </div>
      <div className="hf-pie-responses">
        {values
          .filter((v) => v.name !== "Not recorded" || v.value > 0)
          .map((v) => (
            <button
              key={v.name}
              aria-label={`${label}: ${v.name}`}
              aria-pressed={
                selected?.field === field && selected.response === v.name
              }
              onClick={() => onSelect(v.name)}
            >
              <i style={{ background: v.itemStyle.color }} />
              {v.name} <b>{v.value}</b>
            </button>
          ))}
      </div>
    </article>
  );
}
function Breakdown({
  rows,
  field,
  title,
  selected,
  onSelect,
}: {
  rows: Row[];
  field: string;
  title: string;
  selected?: string;
  onSelect: (value: string) => void;
}) {
  const parent=React.useContext(DesignContext),override=parent.chart_styles?.[`field:${field}`] || {};
  const values = Array.from(new Set(rows.map((r) => text(r[field])))).sort();
  const option = useMemo<EChartsOption>(
    () => ({
      tooltip: { trigger: "item", confine: true },
      grid: { left: 8, right: 25, top: 3, bottom: 3, containLabel: true },
      xAxis: { type: "value", show: false },
      yAxis: {
        type: "category",
        data: values.map((v) =>
          v
            .replace("inpatient_outpatient", "Inpatient & outpatient")
            .replace("outpatient", "Outpatient"),
        ),
        inverse: true,
        axisTick: { show: false },
        axisLine: { show: false },
        axisLabel: {
          fontSize: 9,
          color: "#6b8093",
          width: 110,
          overflow: "truncate",
        },
      },
      series: [
        {
          type: "bar",
          barMaxWidth: 11,
          data: values.map((value, i) => ({
            name: value,
            value: rows.filter((r) => text(r[field]) === value).length,
            itemStyle: {
              color: override.color || (override.palette || colors)[i % (override.palette || colors).length],
              borderRadius: [0, 3, 3, 0],
            },
          })),
          label: { show: true, position: "right", fontSize: 9 },
          emphasis: { itemStyle: { opacity: 0.7 } },
        },
      ],
    }),
    [rows, field, parent],
  );
  return (
    <section className="hf-card hf-breakdown">
      <h2>
        {title} <small>Click to filter</small>
      </h2>
      <InteractiveChart
        option={option}
        onSelect={onSelect}
        label={`${title} chart`}
      />
      <div className="hf-accessible-options">
        {values.map((v) => (
          <button
            key={v}
            aria-label={`${title}: ${v}`}
            aria-pressed={selected === v}
            onClick={() => onSelect(v)}
          >
            {v}
          </button>
        ))}
      </div>
    </section>
  );
}
export default function Facilities({
  id,
  title,
  config,
  theme,
  themeControl,
}: {
  id: string;
  title: string;
  config: FacilityConfiguration;
  theme: "light" | "dark";
  themeControl: React.ReactNode;
}) {
  const [rows, setRows] = useState<Row[]>([]);
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [query, setQuery] = useState("");
  const [serviceFilter, setServiceFilter] = useState<ServiceFilter>();
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    Promise.all([
      load(id, config.source_chart_id),
      config.network_chart_id && config.network_chart_id !== config.source_chart_id
        ? load(id, config.network_chart_id) : Promise.resolve([] as Row[]),
    ])
      .then(([survey, network]) => {
        if (!active) return;
        setRows(
          survey.map((row) => {
            if (!config.network_chart_id || config.network_chart_id === config.source_chart_id) return row;
            const matches = network.filter(
              (n) => nameKey(n.name_facility) === nameKey(row.name_facility),
            );
            const assignments: Record<string, unknown> = {};
            ["pcn", "hub"].forEach((field) => {
              const values = Array.from(
                new Set(matches.map((n) => text(n[field]))),
              );
              assignments[field] =
                values.length === 1 ? values[0] : "Not recorded";
            });
            return { ...row, ...assignments };
          }),
        );
      })
      .catch((e) => {
        if (active) setError(e.message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [id, config.source_chart_id, config.network_chart_id, revision]);
  const filtered = useMemo(
    () =>
      rows.filter(
        (row) =>
          [...fields, "facility_type"].every(
            (f) => !filters[f] || text(row[f]) === filters[f],
          ) &&
          (!serviceFilter ||
            responseValue(row[serviceFilter.field]) ===
              serviceFilter.response) &&
          (!query ||
            text(row.name_facility)
              .toLowerCase()
              .includes(query.toLowerCase())),
      ),
    [rows, filters, query, serviceFilter],
  );
  useEffect(()=>{
    const current=Object.entries(filters).filter(([,value])=>value).map(([column,value])=>({column,operator:"IN",values:[value]}));
    if(serviceFilter)current.push({column:serviceFilter.field,operator:"response_equals",values:[serviceFilter.response]});
    if(query)current.push({column:"name_facility",operator:"contains",values:[query]});
    syncDashboardFilters(current);
  },[filters,serviceFilter,query]);
  function selectService(field: string, response: string) {
    setServiceFilter(
      serviceFilter?.field === field && serviceFilter.response === response
        ? undefined
        : { field, response },
    );
  }
  function selectCategory(field: string, value: string) {
    setFilters({ ...filters, [field]: filters[field] === value ? "" : value });
  }
  const pcns = useMemo(
    () => Array.from(new Set(rows.map((r) => text(r.pcn)))).sort(),
    [rows],
  );
  const names = new Set(filtered.map((r) => nameKey(r.name_facility))).size;
  const count = (field: string) =>
    new Set(
      filtered.map((r) => text(r[field])).filter((v) => v !== "Not recorded"),
    ).size;
  return (
    <DesignContext.Provider value={config.design || {}}><main className={`hf-shell ${theme}`}>
      <aside className="hf-rail">
        <a href="/lifedata-ai/dashboards/" aria-label="Dashboard library">
          L<span>+</span>
        </a>
        <div>▦</div>
        <small>LD</small>
      </aside>
      <div className="hf-workspace">
        <header className="hf-header">
          <div>
            <p className="hf-eyebrow">
              LIFEDATA INTELLIGENCE / FACILITY NETWORK
            </p>
            <h1>{title}</h1>
            <p>
              {config.region_label || "Facility network"} · Health
              infrastructure & service coverage
            </p>
          </div>
          <div className="hf-actions">
            {themeControl}
            <span className="hf-live">● Live data</span>
            <button onClick={() => setRevision(revision + 1)}>↻ Refresh</button>
            <a href="/lifedata-ai/dashboards/">Library ↗</a>
          </div>
        </header>
        <section className="hf-filters">
          {fields.map((field, i) => (!(["pcn", "hub"].includes(field)) || rows.some(row => text(row[field]) !== "Not recorded")) && (
            <label key={field}>
              {labels[i]}
              <select
                aria-label={labels[i]}
                value={filters[field] || ""}
                onChange={(e) =>
                  setFilters({ ...filters, [field]: e.target.value })
                }
              >
                <option value="">All {labels[i].toLowerCase()}s</option>
                {Array.from(new Set(rows.map((r) => text(r[field]))))
                  .sort()
                  .map((v) => (
                    <option key={v}>{v}</option>
                  ))}
              </select>
            </label>
          ))}
          <label className="hf-search">
            Find a facility
            <input
              aria-label="Find a facility"
              placeholder="Search facility name…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
          <button
            onClick={() => {
              setFilters({});
              setQuery("");
              setServiceFilter(undefined);
            }}
          >
            Reset
          </button>
        </section>
        {error && (
          <div role="alert" className="error">
            {error}
          </div>
        )}
        <section className="hf-kpis">
          {[
            [
              "Survey records",
              filtered.length,
              `${rows.length} records in the full dataset`,
            ],
            [
              "Distinct facility names",
              names,
              "Repeated submissions counted once by name",
            ],
            ...(rows.some(row => text(row.pcn) !== "Not recorded") ? [["Primary care networks", count("pcn"), "Recorded PCN assignments"]] : []),
            ...(rows.some(row => text(row.hub) !== "Not recorded") ? [["Linked hubs", count("hub"), "Recorded hub assignments"]] : []),
          ].map(([label, value, note], i) => (
            <div className="hf-card hf-kpi" key={String(label)}>
              <span>{label}</span>
              <strong data-testid={`kpi-${i}`} style={{color:config.design?.chart_styles?.[`field:kpi-${i}`]?.color}}>{loading ? "—" : value}</strong>
              <small>{note}</small>
            </div>
          ))}
        </section>
        <div className="hf-content">
          <section className="hf-card hf-map-card">
            <div className="hf-card-heading">
              <div>
                <h2>Facility footprint</h2>
                <p>{filtered.length} mapped survey records · Colour by PCN</p>
              </div>
              <span className="hf-tag">
                {config.region_label || "FACILITIES"}
              </span>
            </div>
            <MapView rows={filtered} pcns={pcns} bounds={config.map_bounds} />
            <div className="hf-legend">
              {pcns.map((pcn, i) => (
                <span key={pcn}>
                  <i style={{ background: colors[i % colors.length] }} />
                  {pcn}
                </span>
              ))}
            </div>
          </section>
          <section className="hf-card hf-services">
            <div className="hf-card-heading">
              <div>
                <h2>Service availability</h2>
                <p>Hover for details · Click Yes / No to filter all panels</p>
              </div>
              <span className="hf-tag">11 SERVICES</span>
            </div>
            <div className="hf-service-pies">
              {services.map(([field, label]) => (
                <ServicePie
                  key={field}
                  rows={filtered}
                  field={field}
                  label={label}
                  selected={serviceFilter}
                  onSelect={(response) => selectService(field, response)}
                />
              ))}
              <div className="hf-service-selection">
                {serviceFilter ? (
                  <>
                    <strong>
                      {services.find((s) => s[0] === serviceFilter.field)?.[1]}
                    </strong>
                    <span>{serviceFilter.response} responses selected</span>
                    <button onClick={() => setServiceFilter(undefined)}>
                      Clear service filter ×
                    </button>
                  </>
                ) : (
                  <>
                    <strong>Explore coverage</strong>
                    <span>
                      Select any slice or response to focus the entire
                      dashboard.
                    </span>
                  </>
                )}
              </div>
            </div>
          </section>
          <div className="hf-bottom">
            {[
              ["facility_type", "Facility category"],
              ["hf_level", "Facility level"],
              ["ownership", "Ownership"],
            ].map(([field, label]) => (
              <Breakdown
                key={field}
                rows={filtered}
                field={field}
                title={label}
                selected={filters[field]}
                onSelect={(value) => selectCategory(field, value)}
              />
            ))}
          </div>
        </div>
        <footer className="hf-footer">
          <span>
            {loading
              ? "Loading live Superset data…"
              : `Showing ${filtered.length} of ${rows.length} survey records · All filters apply across every panel`}
          </span>
          <span>
            PCN/hub links use exact normalised names; missing links remain “Not
            recorded”.
          </span>
        </footer>
      </div>
    </main></DesignContext.Provider>
  );
}
