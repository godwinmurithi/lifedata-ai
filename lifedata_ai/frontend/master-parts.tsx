// Parts the generated master-view.tsx uses where the design had placeholders: real maps and unbound cards.
import React, { useEffect, useMemo, useRef } from "react";
import L from "leaflet";

type Pin = { lat: number; lon: number; s: number; bg: string; bw: number; bd: string; op: number; label: string; pick: () => void };

/** The design's map box, now a Leaflet map (OpenStreetMap tiles) that refits to the current pins. Pins are
 *  keyboard-operable markers with the design's size, fill and border; selecting one filters every tab. */
export function MasterMap({ pins, label, style, className }: { pins: Pin[]; label: string; style: React.CSSProperties; className?: string }) {
  const host = useRef<HTMLDivElement>(null), map = useRef<L.Map | null>(null), layer = useRef<L.LayerGroup | null>(null);
  const latest = useRef(pins);
  latest.current = pins;
  useEffect(() => {
    if (!host.current) return;
    map.current = L.map(host.current, { zoomControl: true }).setView([0.6, 33.4], 9);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>', maxZoom: 19 }).addTo(map.current);
    layer.current = L.layerGroup().addTo(map.current);
    const observer = new ResizeObserver(() => map.current?.invalidateSize());
    observer.observe(host.current);
    return () => { observer.disconnect(); map.current?.remove(); map.current = null; };
  }, []);
  const key = useMemo(() => JSON.stringify((pins || []).map((p) => [p.lat, p.lon, p.s, p.bg, p.bw, p.bd, p.op, p.label])), [pins]);
  useEffect(() => {
    if (!map.current || !layer.current) return;
    layer.current.clearLayers();
    const valid = (pins || []).filter((p) => Number.isFinite(p.lat) && Number.isFinite(p.lon));
    valid.forEach((p, i) => {
      const pick = () => latest.current.filter((q) => Number.isFinite(q.lat) && Number.isFinite(q.lon))[i]?.pick();
      const icon = L.divIcon({ className: "master-pin", iconSize: [p.s, p.s], iconAnchor: [p.s / 2, p.s / 2],
        html: `<span style="display:block;width:${p.s}px;height:${p.s}px;border-radius:50%;background:${p.bg};border:${p.bw}px solid ${p.bd};opacity:${p.op};box-sizing:border-box"></span>` });
      const marker = L.marker([p.lat, p.lon], { icon, keyboard: true, title: p.label, alt: p.label }).on("click", pick).addTo(layer.current!);
      const el = marker.getElement();
      if (el) { el.setAttribute("aria-label", p.label); el.setAttribute("role", "button"); el.addEventListener("keydown", (e) => { if (e.key === " ") { e.preventDefault(); pick(); } }); }
    });
    if (valid.length) map.current.fitBounds(L.latLngBounds(valid.map((p) => [p.lat, p.lon] as [number, number])), { padding: [36, 36], maxZoom: 13 });
  }, [key]);
  const { backgroundImage, backgroundSize, backgroundColor, ...box } = style as any;
  return <div role="group" aria-label={label} className={className} style={{ ...box, position: "relative" }}><div ref={host} style={{ position: "absolute", inset: 0 }} /></div>;
}

const TITLES: Record<string, string> = {
  "chart.vital_events_by_month": "Vital events by month", "chart.event_mix": "Event mix", "map.events": "Where events are happening",
  "stat.ever_registered": "Ever registered", "stat.currently_pregnant": "Currently pregnant", "stat.under_five_ever": "Under-five",
  "stat.outpatient_visits": "Outpatient visits", "chart.age_gender": "Age and gender", "chart.housing": "Housing quality",
  "chart.water_sanitation": "Water & sanitation", "chart.bednet": "Bednet use",
};

/** A card whose slot has no registered source: same card style, the registered reason instead of numbers. */
export function Unbound({ o, name }: { o: any; name: string }) {
  return <section className="mf-card mf-unbound" aria-label={(TITLES[name] || name) + ": not available"} style={{ background: "#FDFCFA", border: "1px dashed #D4CABB", borderRadius: 18, padding: "16px 20px",
    boxSizing: "border-box", display: "flex", flexDirection: "column", gap: 6, minWidth: 0, flex: "1 1 0", minHeight: 110, fontFamily: "'Inter',sans-serif" }}>
    <div style={{ fontSize: 15, color: "#66615A" }}>{TITLES[name] || name}</div>
    <div style={{ fontSize: 13, color: "#8A857E" }}>{o.slotReason(name) || "No data source registered"}</div>
  </section>;
}

/** "Fit one window" sizing for the generated view (classes added by tools/dc2tsx.mjs, step 7). The design's inline
 *  styles stay; these rules (!important beats inline) let rows share the window height, shrink cards and type, and
 *  scale charts. Below 900px wide or 560px high the page scrolls instead, with the design's natural sizes. */
export const MASTER_FIT_CSS = `
.mf-frame{height:100vh;display:flex;flex-direction:column;overflow:hidden;background:#0E0F11}
.mf-top{flex:none;display:flex;align-items:center;gap:14px;max-width:1440px;width:100%;margin:0 auto;padding:2px 16px;box-sizing:border-box;font-family:'Inter',sans-serif}
.mf-top h1{margin:0;font-size:16px;font-weight:700;color:#FFFFFF}
.mf-frame .mf-root{flex:1 1 auto;min-height:0!important;width:100%;padding:0 16px 8px!important;display:flex;flex-direction:column}
.mf-page{flex:1 1 auto;min-height:0!important;display:flex;flex-direction:column}
.mf-tabs{flex:none}
.mf-tab{padding:8px 16px 6px!important;font-size:14px!important}
.mf-filters{flex:none;flex-direction:row!important;flex-wrap:wrap;align-items:center;gap:6px 18px!important;padding:7px 14px!important}
.mf-frow{gap:6px 16px!important}
.mf-flabel{font-size:13px!important;gap:8px!important}
.mf-flabel svg{width:18px;height:18px}
.mf-select{height:30px!important;font-size:13px!important}
.mf-select[style*="width: 150px"]{width:120px!important}
.mf-select[style*="width: 190px"]{width:140px!important}
.mf-select[style*="width: 230px"]{width:205px!important}
.mf-clear{height:30px!important;font-size:13px!important}
.mf-chips{flex:none;padding:6px 12px 0!important;gap:6px!important}
.mf-chips button{height:24px!important;font-size:12px!important}
.mf-help{flex:none;font-size:12px!important}
.mf-body{flex:1 1 auto;min-height:0;padding:8px 12px!important;gap:8px!important}
.mf-kpis{flex:none;gap:8px!important;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))!important}
.mf-kpi{padding:5px 12px!important;gap:10px!important;border-radius:12px!important}
.mf-ico{width:32px!important;height:32px!important;border-radius:9px!important}
.mf-ico svg,.mf-sico svg{width:18px;height:18px}
.mf-kpi div[style*="font-size: 14px"]{font-size:12px!important}
.mf-kval{font-size:21px!important}
.mf-row{flex:1 1 0;min-height:0;flex-wrap:nowrap!important;gap:8px!important}
.mf-row:has(.mf-map),.mf-row:has(.mf-unbound[aria-label^="Where events"]){flex-grow:1.45}
.mf-row>*,.mf-col{min-width:0!important}
.mf-col{min-height:0;gap:8px!important}
.mf-col>.mf-card:has(.mf-lbox){flex-grow:1.8!important}
.mf-col>.mf-card:has(>.mf-donut){display:grid!important;grid-template-columns:minmax(0,42%) minmax(0,1fr);grid-template-rows:auto minmax(0,1fr);column-gap:10px}
.mf-col>.mf-card:has(>.mf-donut)>.mf-head{grid-column:1/-1}
.mf-col>.mf-card:has(>.mf-donut)>.mf-legend{grid-template-columns:1fr!important;align-content:center;padding-top:0!important}
.mf-unbound{padding:9px 14px!important;gap:3px!important}
.mf-unbound>div:first-child{font-size:14px!important}
.mf-unbound>div+div{font-size:12px!important;line-height:1.35;overflow:hidden}
.mf-card{padding:9px 14px!important;border-radius:14px!important;min-height:0!important;overflow:hidden}
.mf-head{margin-bottom:4px!important;flex:none}
.mf-h2{font-size:14px!important}
.mf-stat{min-height:0!important;padding:6px 14px!important;border-radius:14px!important;overflow:hidden}
.mf-sico{width:34px!important;height:34px!important;border-radius:10px!important}
.mf-stat div[style*="font-size: 15px"]{font-size:12px!important}
.mf-sval{font-size:22px!important;margin-top:0!important}
.mf-legend{flex:none;padding-top:3px!important;gap:0 8px!important;margin-top:0!important}
.mf-legend button{font-size:12px!important;padding:0 3px!important;gap:5px!important;line-height:1.3!important}
.mf-legend i{width:9px!important;height:9px!important}
.mf-map{flex:1 1 0!important;min-height:0!important;height:auto!important}
.mf-lbox{flex:1 1 0;min-height:0;display:flex;justify-content:center}
.mf-linner{height:100%;max-width:100%;display:flex;flex-direction:column}
.mf-linner>svg{flex:1 1 0;min-height:0;width:100%;height:100%;margin:0!important}
.mf-axis{height:18px!important;flex:none}
.mf-axis button{font-size:11px!important;padding:1px 4px!important}
.mf-donut{flex:1 1 0;min-height:0;width:100%!important;height:100%!important;margin:0!important}
.mf-pyr{flex:1 1 0;min-height:0;align-items:stretch!important}
.mf-pyrow{flex:1 1 0;min-height:0;height:auto!important;gap:6px!important}
.mf-pyrow>span{width:24px!important;font-size:12px!important;flex:none}
.mf-pyrow>button{align-self:stretch;align-items:center}
.mf-pyrow>button:nth-of-type(2){width:42px!important;font-size:12px!important;flex:none}
.mf-pyrow>button>span{height:60%!important;max-height:18px}
.mf-card:has(.mf-bar)>[role=group]{flex:1 1 0;min-height:0}
.mf-bar{height:auto!important;flex:1 1 0;min-height:0}
.mf-bar span[style*="font-size"]{font-size:12px!important}
.mf-bar span[style*="width: 84px"]{width:76px!important}
.mf-barfill{height:70%!important;max-height:30px}
.mf-scatter{flex:1 1 0;min-height:0;margin-top:0!important}
.mf-scatter>div:last-child>div{height:100%!important}
.mf-scatter span{font-size:11px!important}
.mf-tablecard{flex:none;padding:8px 14px!important;border-radius:14px!important;overflow:hidden}
.mf-tablewrap{flex:none;overflow-x:auto!important;overflow-y:hidden}
.mf-tablewrap *{font-size:13px!important}
.mf-tablewrap>div>div{padding-top:2px!important;padding-bottom:2px!important}
.mf-tablewrap button{padding-top:0!important;padding-bottom:0!important}
.mf-tablewrap button[aria-pressed]{padding:0 8px!important}
@media (max-width:1450px){
  .mf-select[style*="width: 150px"]{width:100px!important}
  .mf-select[style*="width: 190px"]{width:118px!important}
  .mf-select[style*="width: 230px"]{width:185px!important}
  .mf-flabel{font-size:12px!important}
  .mf-filters,.mf-frow{gap:6px 12px!important}
}
@media (max-height:700px){
  .mf-col>.mf-card:has(>.mf-donut)>.mf-legend button{font-size:11px!important;line-height:1.2!important}
}
@media (max-width:900px),(max-height:560px){
  .mf-frame{height:auto;min-height:100vh;overflow:visible}
  .mf-row{flex:none;flex-wrap:wrap!important}
  .mf-row>.mf-card,.mf-row>.mf-col,.mf-row>.mf-unbound{flex:1 1 280px!important;min-height:260px!important}
  .mf-map{min-height:280px!important}
  .mf-tablecard{flex:none}
}
`;
