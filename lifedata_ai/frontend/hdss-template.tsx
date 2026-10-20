// The hdss-site template page: the approved design (docs/design/hdss/design_hdss_template.dc.html) as React.
// Markup and inline styles follow the design; data comes from POST /lifedata-ai/dashboards/<id>/query
// (aggregates only). Two deliberate changes from the design: labels wrap instead of truncating
// ("Firew...", "45-64"), and slots without a registered source show a placeholder instead of numbers.
import React, { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import { Aggregates, DEFAULTS, GAUGE_PATH, LEVELS, LABEL, Spec, State, renderVals, serverState, slotReason } from "./hdss-model";
// The two supplied images are module static assets (not bundled, not data URIs).
const deepdiveIcon = "/lifedata-ai/static/dashboards/hdss/deepdive_icon.png";
const lifeconnectLogo = "/lifedata-ai/static/dashboards/hdss/lifeconnect_logo.png";

const FONT = "'Inter',sans-serif";
// Compact "fit one window" sizing: cards fill viewport-height grid rows; content scrolls inside a card only on very short screens.
const CARD: React.CSSProperties = { background: "#FFFFFF", border: "1px solid #E2E6EC", borderRadius: 14, padding: "10px 14px", boxSizing: "border-box", display: "flex", flexDirection: "column", minWidth: 0, minHeight: 0, overflow: "auto" };
const H2: React.CSSProperties = { margin: "0 0 4px", fontSize: 11, fontWeight: 700, color: "#7B2A3D", lineHeight: 1.2 };
const SELECT: React.CSSProperties = { fontFamily: FONT, appearance: "none", WebkitAppearance: "none", height: 28, fontSize: 10, color: "#1F2430", background: "#FFFFFF", border: "1px solid #D0D5DD", borderRadius: 8, padding: "0 28px 0 10px" };
const FILTER_LABEL: React.CSSProperties = { display: "flex", alignItems: "center", gap: 5, fontSize: 10, color: "#6B7280" };
const SELECT_WIDTH: Record<string, number> = { district: 100, county: 110, subcounty: 110, parish: 104, village: 110 };
// Three breakpoints, same 13 named areas throughout: 7 columns from 1100px (the original design's layout,
// sized to fill exactly one window), 4 columns down to 640px, one stacked column below that. Each card
// declares only its area name (gridArea), so a breakpoint can rearrange the page without the cards'
// own markup changing. Below 1100px the page scrolls normally instead of being held to one window.
const GRID_CSS = `
.hdss-frame{height:100vh;min-height:620px}
.hdss-frame button:not(:disabled),.hdss-frame select,.hdss-frame a{transition:filter .12s,background-color .12s}
.hdss-frame button:not(:disabled):hover{filter:brightness(.92)}
.hdss-frame section button:not(:disabled):hover{background-color:rgba(123,42,61,.06)!important;border-radius:6px}
.hdss-frame button:focus-visible,.hdss-frame a:focus-visible,.hdss-frame select:focus-visible,.hdss-pin:focus-visible{outline:2px solid #7B2A3D;outline-offset:2px}
.hdss-frame circle[style*="cursor"]:hover{stroke-width:15}
.hdss-pin:hover span{filter:brightness(1.15)}
.hdss-grid-wrap{flex:1;min-height:0}
.hdss-grid{display:grid;gap:10px;height:100%;box-sizing:border-box;
  grid-template-columns:repeat(7,minmax(0,1fr));
  grid-template-rows:minmax(130px,.9fr) minmax(140px,1.05fr) minmax(140px,1.05fr);
  grid-template-areas:
    "gauge age map map map fuel water"
    "edu occ map map map roof net"
    "mig mar lifepop lifepop lifepop assets assets";}
@media (max-width:1099px){
  .hdss-frame{height:auto;min-height:100vh}
  .hdss-grid-wrap{flex:none}
  .hdss-grid{height:auto;grid-template-columns:repeat(4,minmax(0,1fr));grid-template-rows:none;grid-auto-rows:minmax(220px,auto);
  grid-template-areas:
    "gauge age map map"
    "edu occ map map"
    "fuel water roof net"
    "mig mar assets assets"
    "lifepop lifepop lifepop lifepop";}}
@media (max-width:639px){.hdss-grid{grid-template-columns:1fr;
  grid-template-areas:"gauge" "age" "map" "fuel" "water" "edu" "occ" "roof" "net" "mig" "mar" "lifepop" "assets";}}
`;

type Props = { publicationId: string; title: string; canEdit: boolean };

export default function HdssTemplate({ publicationId, canEdit }: Props) {
  const [spec, setSpec] = useState<(Spec & { status: string }) | null>(null);
  const [data, setData] = useState<Aggregates | null>(null);
  const [S, setS] = useState<State>({ ...DEFAULTS, dims: {} });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const committed = JSON.stringify(serverState(S));

  useEffect(() => {
    fetch(`/lifedata-ai/api/hdss/dashboards/${publicationId}/spec/`, { credentials: "same-origin" })
      .then(async (r) => { const b = await r.json(); if (!r.ok) throw new Error(b.error || `Request failed (${r.status})`); setSpec(b); })
      .catch((e) => setError(e.message));
  }, [publicationId]);
  // One request in flight at a time; while it runs only the LATEST filter state is kept. Superset runs the
  // aggregates on few workers, so stacking a request per click would queue work nobody will look at.
  const inflight = useRef(false), pending = useRef<string | null>(null);
  const pump = () => {
    if (inflight.current || pending.current === null) return;
    const body = pending.current; pending.current = null; inflight.current = true; setBusy(true);
    fetch(`/lifedata-ai/dashboards/${publicationId}/query`, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ state: JSON.parse(body) }) })
      .then(async (r) => { const b = await r.json(); if (!r.ok) throw new Error(b.error || `Request failed (${r.status})`); if (pending.current === null) { setData(b.data); setError(""); } })
      .catch((e) => setError(e.message))
      .finally(() => { inflight.current = false; if (pending.current === null) setBusy(false); pump(); });
  };
  useEffect(() => { if (!spec) return; pending.current = committed; pump(); }, [spec, committed, publicationId]);

  if (!spec || !data) {
    return <div style={{ fontFamily: FONT, background: "#0F1220", minHeight: "100vh", color: "#FFFFFF", padding: 28 }}>{error ? <span role="alert">{error}</span> : "Loading…"}</div>;
  }
  return <HdssView spec={spec} data={data} S={S} setS={setS} busy={busy} error={error} canEdit={canEdit} publicationId={publicationId} onStatus={(status) => setSpec({ ...spec, status })} />;
}

type ViewProps = { spec: Spec & { status: string }; data: Aggregates; S: State; setS: (s: State) => void; busy: boolean; error: string; canEdit: boolean; publicationId: string; onStatus: (s: string) => void };

/** Pure render of the approved layout from the view model (server-renderable for tests). */
export function HdssView({ spec, data, S, setS, busy, error, canEdit, publicationId, onStatus }: ViewProps) {
  const o = renderVals(S, data, spec, setS);
  const has = (slot: string) => spec.slots.includes(slot);
  return (
    <div className="hdss-frame" style={{ fontFamily: FONT, background: "#0F1220", display: "flex", flexDirection: "column" }}>
      <style>{GRID_CSS}</style>
      <div style={{ position: "relative", background: "#0F1220", flex: 1, minHeight: 0, display: "flex", flexDirection: "column", padding: "0 16px 10px", boxSizing: "border-box", color: "#1F2430" }} className="hdss-page">
        <svg width="0" height="0" style={{ position: "absolute" }} aria-hidden="true"><defs>
          {[["g0", "#A58B72"], ["g1", "#B35A5A"], ["g2", "#5D7191"], ["g3", "#7A6E66"], ["g4", "#4F7A62"]].map(([id, c]) => (
            <linearGradient key={id} id={id} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={c} stopOpacity="0.35" /><stop offset="1" stopColor={c} stopOpacity="0" /></linearGradient>))}
        </defs></svg>
        <StatusBar publicationId={publicationId} status={spec.status} canEdit={canEdit} onChange={onStatus} />
        <header style={{ background: "#FFFFFF", padding: "10px 20px", fontFamily: FONT, flex: "none" }}>
          <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
            <h1 style={{ margin: 0, fontSize: 18, fontWeight: 800, color: "#7B2A3D" }}>{spec.title}</h1>
            <span style={{ fontSize: 12, color: "#1F2430" }} aria-live="polite">{/* record count removed at the user's request (2026-10-20) */}{busy ? <span style={{ fontSize: 10, color: "#6B7280" }}>Updating…</span> : null}
              {/* only when small counts are hidden (real datasets with min_cell_size): explains the "<n" values */}
              {o.suppressed && <span style={{ display: "block", fontSize: 10, color: "#6B7280", textAlign: "right" }}>{o.suppressed}</span>}</span>
          </div>
          <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: "6px 10px", marginTop: 6 }}>
            <div style={{ display: "contents" }}>
              <svg aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#6B7280" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" style={{ flex: "none" }}><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33 1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82 1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg>
              {LEVELS.slice(0, 4).map((l) => o["show" + LABEL[l]] && <Select key={l} label={LABEL[l]} width={SELECT_WIDTH[l]} value={o["f" + LABEL[l]]} options={o["opt" + LABEL[l]]} onChange={o["on" + LABEL[l]]} />)}
            </div>
            <div style={{ display: "contents" }}>
              {o.showVillage && <Select label="Village" width={SELECT_WIDTH.village} value={o.fVillage} options={o.optVillage} onChange={o.onVillage} />}
              {o.showCycle && <Select label="Update cycle" width={150} value={o.fCycle} options={o.optCycle} onChange={o.onCycle} />}
              {o.showRange && <Select label="Time period" width={120} value={o.fRange} options={o.optRange} onChange={o.onRange} />}
              <button type="button" onClick={o.clearAll} style={{ fontFamily: FONT, height: 28, padding: "0 6px", fontSize: 10, fontWeight: 500, color: "#6B7280", background: "none", border: 0, cursor: "pointer" }}>Clear All</button>
            </div>
          </div>
          {error && <div role="alert" style={{ marginTop: 12, color: "#B42318", fontSize: 11 }}>{error}</div>}
        </header>
        {o.hasChips && <div style={{ display: "flex", gap: 8, flexWrap: "wrap", padding: "6px 20px", alignItems: "center", background: "#FFFFFF", borderTop: "1px solid #E2E6EC", fontFamily: FONT, flex: "none" }}>
          <span style={{ fontSize: 10, color: "#6B7280" }}>Filtered by</span>
          {o.chips.map((c: any) => <button key={c.label} type="button" onClick={c.clear} aria-label={"Remove filter " + c.label} style={{ fontFamily: FONT, display: "inline-flex", alignItems: "center", gap: 6, height: 26, padding: "0 10px", borderRadius: 13, border: "1px solid #D0D5DD", background: "#FFFFFF", fontSize: 10, color: "#1F2430", cursor: "pointer" }}>{c.label}<span aria-hidden="true" style={{ fontSize: 11, lineHeight: 1 }}>×</span></button>)}
        </div>}
        <div style={{ background: "#F1F3F6", padding: 10, display: "flex", flexDirection: "column", gap: 10, flex: 1, minHeight: 0 }}>
          {o.isEmpty && <div style={{ background: "#FFFFFF", border: "1px solid #E2E6EC", borderRadius: 18, padding: "48px 24px", textAlign: "center", fontFamily: FONT }}>
            <div style={{ fontSize: 16, fontWeight: 700, color: "#7B2A3D" }}>No person records match this selection</div>
            <div style={{ fontSize: 11, color: "#6B7280", marginTop: 6 }}>Remove a filter to see data again.</div>
            <button type="button" onClick={o.clearAll} style={{ fontFamily: FONT, marginTop: 16, height: 44, padding: "0 22px", fontSize: 11, fontWeight: 600, color: "#FFFFFF", background: "#111827", border: 0, borderRadius: 8, cursor: "pointer" }}>Clear all filters</button>
          </div>}
          {o.showBody && <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 10, flex: "none" }}>
              {o.kpis.map((k: any) => <div key={k.label} style={{ position: "relative", display: "flex" }}>
                {k.bound ? <button type="button" aria-pressed={k.pr} onClick={k.pick} style={{ fontFamily: FONT, position: "relative", width: "100%", textAlign: "left", background: "#FFFFFF", border: "2px solid " + k.bd, borderRadius: 14, padding: "8px 14px 0", overflow: "hidden", cursor: "pointer", opacity: k.op, minHeight: 72, display: "flex", flexDirection: "column", justifyContent: "space-between" }}>
                  <span style={{ display: "block", paddingRight: 38 }}>
                    <span style={{ display: "block", fontSize: 10, color: "#6B7280" }}>{k.label}</span>
                    <span style={{ display: "block", fontSize: 18, fontWeight: 700, color: "#1F2430", lineHeight: 1.1 }}>{k.value}</span>
                  </span>
                  {k.hasSpark ? <Sparkline k={k} /> : <span style={{ height: 26 }} />}
                </button> : <Unbound title={k.label} reason={k.reason} style={{ width: "100%", minHeight: 72, borderRadius: 14 }} />}
                <a href={k.link} onClick={k.dive} aria-label={k.dlabel} title={k.dlabel} style={{ position: "absolute", right: 8, top: 8, width: 30, height: 30, display: "block", borderRadius: "50%", textDecoration: "none", lineHeight: 0 }}>
                  <img alt="" src={deepdiveIcon} width={30} height={30} style={{ display: "block", width: 30, height: 30, objectFit: "contain" }} />
                </a>
              </div>)}
            </div>
            {o.hasNote && <div role="status" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, background: "#EAF2FD", border: "1px solid #BCD0EE", borderRadius: 10, padding: "6px 12px", fontFamily: FONT, fontSize: 10, color: "#1F2430", flex: "none" }}>
              <span>{o.noteText}</span>
              <button type="button" onClick={o.closeNote} aria-label="Dismiss" style={{ fontFamily: FONT, border: 0, background: "none", fontSize: 16, lineHeight: 1, cursor: "pointer", color: "#1F2430" }}>×</button>
            </div>}
            {/* The card grid reflows at three breakpoints (7 cols / 4 cols / 1 col) instead of forcing
                horizontal scroll on narrow screens; at 7 columns it fills the rest of the window. */}
            <div className="hdss-grid-wrap">
              <div className="hdss-grid">
                <section style={{ ...CARD, gridArea: "gauge" }}>
                  <Chips items={o.unitChips} />
                  <div style={{ position: "relative", flex: 1, display: "flex", flexDirection: "column", justifyContent: "center", minHeight: 0 }}>
                    <svg role="img" aria-label="Share of all records currently selected" viewBox="0 0 220 150" width="100%" style={{ display: "block", maxWidth: 180, maxHeight: "100%", margin: "0 auto" }}>
                      <path d={GAUGE_PATH} fill="none" stroke="#E8EAEE" strokeWidth="14" strokeLinecap="round" pathLength={100} />
                      <path d={GAUGE_PATH} fill="none" stroke="#7B2A3D" strokeWidth="14" strokeLinecap="round" pathLength={100} strokeDasharray={o.gaugeDash} />
                    </svg>
                    <span style={{ position: "absolute", left: "50%", top: "56%", transform: "translate(-50%,-50%)", fontSize: 16, fontWeight: 700, color: "#7B2A3D" }}>{o.gaugeValue}</span>
                  </div>
                  <div style={{ fontSize: 12, fontWeight: 700, color: "#7B2A3D", marginTop: 2 }}>{o.gaugeLabel}</div>
                </section>
                <section style={{ ...CARD, gridArea: "age" }}>
                  <h2 style={H2}>Age and gender</h2>
                  {o.hasAge ? <div role="group" aria-label="Age and gender. Select a bar to filter by age band and sex." style={{ display: "flex", flexDirection: "column", justifyContent: "space-around", flex: 1, minHeight: 0, marginTop: 2 }}>
                    <div aria-hidden="true" style={{ display: "flex", justifyContent: "space-between", fontSize: 10, fontWeight: 600, flex: "none" }}>
                      <span style={{ color: "#5E7186" }}>{o.ageSexes[0]}</span><span style={{ color: "#946A4C" }}>{o.ageSexes[1]}</span>
                    </div>
                    {o.ageRows.map((r: any) => <div key={r.a} style={{ display: "flex", alignItems: "center", width: "100%", flex: "1 1 0", minHeight: 14, maxHeight: 30 }}>
                      <button type="button" aria-label={r.mlabel} aria-pressed={r.mpr} onClick={r.pm} style={{ fontFamily: FONT, flex: "1 1 0", minWidth: 0, display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 4, background: "none", border: 0, padding: "0 3px 0 0", cursor: "pointer", opacity: r.mo }}>
                        <span style={{ marginRight: "auto", fontSize: 10, fontWeight: 600, color: "#1F2430" }}>{r.mn}</span>
                        <span style={{ display: "block", width: r.mw + "%", maxWidth: "75%", height: 10, borderRadius: 5, background: "#5E7186" }} /></button>
                      {/* fix: the band label sizes to its text and never wraps ("45-64" wrapped at 42px) */}
                      <button type="button" aria-label={r.alabel} aria-pressed={r.apr} onClick={r.pa} style={{ fontFamily: FONT, flex: "none", minWidth: 34, padding: "0 2px", whiteSpace: "nowrap", textAlign: "center", fontSize: 10, fontWeight: 500, color: "#1F2430", background: "none", border: 0, cursor: "pointer", opacity: r.ao }}>{r.a}</button>
                      <button type="button" aria-label={r.flabel} aria-pressed={r.fpr} onClick={r.pf} style={{ fontFamily: FONT, flex: "1 1 0", minWidth: 0, display: "flex", alignItems: "center", gap: 4, background: "none", border: 0, padding: "0 0 0 3px", cursor: "pointer", opacity: r.fo }}>
                        <span style={{ display: "block", width: r.fw + "%", maxWidth: "75%", height: 10, borderRadius: 5, background: "#946A4C" }} />
                        <span style={{ marginLeft: "auto", fontSize: 10, fontWeight: 600, color: "#1F2430" }}>{r.fn}</span></button>
                    </div>)}
                  </div> : <UnboundBody reason={slotReason(spec, "card.age_gender") || "No data source registered"} />}
                </section>
                <section style={{ ...CARD, gridArea: "map" }}>
                  <h2 style={H2}>Coordinates</h2>
                  {o.hasMap ? <VillageMap pins={o.mapPins} count={o.mapCount} /> : <UnboundBody reason={slotReason(spec, "card.coordinates") || "No data source registered"} />}
                </section>
                <PillCard area="fuel" title="Cooking fuel" rows={o.fuelRows} color="#CC6A73" slot="card.cooking_fuel" spec={spec} />
                <PillCard area="water" title="Water source" rows={o.waterRows} color="#3F5FA9" slot="card.water_source" spec={spec} />
                <PillCard area="edu" title="Education" rows={o.eduRows} color="#B5995A" slot="card.education" spec={spec} />
                <PillCard area="occ" title="Occupation" rows={o.occRows} color="#5FA48A" slot="card.occupation" spec={spec} />
                <DonutCard area="roof" o={o} prefix="roof" slot="card.roof_floor" spec={spec} />
                <DonutCard area="net" o={o} prefix="net" slot="card.bednet_toilet" spec={spec} />
                <DonutCard area="mig" o={o} prefix="mig" slot="card.migration_water" spec={spec} />
                <DonutCard area="mar" o={o} prefix="mar" title="Marital status" slot="card.marital" spec={spec} />
                <div style={{ gridArea: "lifepop", display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, minHeight: 0 }}>
                  <section style={CARD}>
                    <h2 style={{ margin: 0, fontSize: 14, fontWeight: 700, color: "#7B2A3D", letterSpacing: 0.5 }}>LIFE CONNECT</h2>
                    <div style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 6 }}>
                      <img alt="LifeConnect" width={485} height={112} src={lifeconnectLogo} style={{ display: "block", width: "100%", maxWidth: 190, height: "auto" }} />
                    </div>
                    <div style={{ textAlign: "center", fontSize: 10, color: "#B79A52" }}>SMS/Email/Zoom/Webinar</div>
                  </section>
                  <section style={CARD}>
                    <div role="group" aria-label="Population and households. Select a bar to switch the gauge." style={{ position: "relative", flex: 1, display: "flex", alignItems: "flex-end", justifyContent: "space-around", gap: 18, padding: "22px 10px 0 40px", minHeight: 0 }}>
                      {o.popRef && <><div style={{ position: "absolute", left: 0, right: 0, top: 22, borderTop: "1.5px dashed #9AA1AD" }} />
                        <span style={{ position: "absolute", left: 0, top: 12, fontSize: 10, color: "#6B7280" }}>{o.popRef}</span></>}
                      <div style={{ position: "absolute", left: 34, right: 0, bottom: 22, borderTop: "1px solid #D0D5DD" }} />
                      {o.popBars.map((b: any) => <button key={b.l} type="button" aria-label={b.label} aria-pressed={b.pr} onClick={b.pick} style={{ fontFamily: FONT, position: "relative", flex: 1, height: "100%", display: "flex", flexDirection: "column", justifyContent: "flex-end", alignItems: "center", background: "none", border: 0, padding: "0 0 22px", cursor: "pointer" }}>
                        <span style={{ display: "block", fontSize: 11, fontWeight: 700, color: "#7B2A3D", marginBottom: 2 }}>{b.n}</span>
                        <span style={{ display: "block", width: "100%", height: b.h + "%", maxHeight: "100%", background: b.c, minHeight: 3 }} />
                        <span style={{ position: "absolute", bottom: 2, left: 0, right: 0, textAlign: "center", fontSize: 10, color: "#6B7280" }}>{b.l}</span>
                      </button>)}
                    </div>
                    <div style={{ textAlign: "center", fontSize: 11, fontWeight: 700, color: "#7B2A3D", marginTop: 4, lineHeight: 1.2 }}>Population and Household Data</div>
                  </section>
                </div>
                <section style={{ ...CARD, gridArea: "assets" }}>
                  <h2 style={H2}>Household assets</h2>
                  {has("card.assets") && o.assetRows.length ? <div role="group" aria-label="Household assets. Select a bar to filter by it." style={{ display: "flex", flexDirection: "column", gap: 1, marginTop: 0, flex: 1, justifyContent: "space-around" }}>
                    {o.assetRows.map((r: any) => <button key={r.l} type="button" aria-label={r.label} aria-pressed={r.pr} onClick={r.pick} style={{ fontFamily: FONT, display: "flex", alignItems: "center", gap: 10, width: "100%", background: "none", border: 0, padding: 0, cursor: "pointer", opacity: r.op }}>
                      <span style={{ display: "block", flex: "0 1 auto", minWidth: 62, maxWidth: "40%", textAlign: "right", fontSize: 10, color: "#1F2430", lineHeight: 1.1 }}>{r.l}</span>
                      <span style={{ display: "block", flex: 1, height: 13, borderRadius: 4, background: "#EEF0F3", overflow: "hidden", minWidth: 0 }}><span style={{ display: "block", width: r.w + "%", height: 13, background: "#63A58A", borderRadius: 5 }} /></span>
                      <span style={{ display: "block", minWidth: 34, textAlign: "left", fontSize: 11, fontWeight: 700, color: "#1F2430" }}>{r.n}</span>
                    </button>)}
                  </div> : <UnboundBody reason={slotReason(spec, "card.assets") || "No data source registered"} />}
                </section>
              </div>
            </div>
          </>}
        </div>
      </div>
    </div>
  );
}

function Select({ label, width, value, options, onChange }: { label: string; width: number; value: string; options: { v: string }[]; onChange: (e: any) => void }) {
  return <label style={FILTER_LABEL}>{label}<span style={{ position: "relative", display: "inline-flex", alignItems: "center" }}>
    <select style={{ ...SELECT, width }} value={value} onChange={onChange}>{options.map((opt) => <option key={opt.v} value={opt.v} label={opt.v}>{opt.v}</option>)}</select>
    <span style={{ position: "absolute", right: 10, pointerEvents: "none", display: "flex" }}><svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#1F2430" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flex: "none" }}><path d="m6 9 6 6 6-6" /></svg></span>
  </span></label>;
}

function Chips({ items }: { items: any[] }) {
  return <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 4, flex: "none" }}>
    {items.map((c) => <button key={c.label} type="button" aria-pressed={c.pr} onClick={c.pick} style={{ fontFamily: FONT, fontSize: 10, fontWeight: 500, padding: "4px 8px", borderRadius: 7, border: 0, background: c.bg, color: c.color, cursor: "pointer", whiteSpace: "nowrap" }}>{c.label}</button>)}
  </div>;
}

function PillCard({ area, title, rows, color, slot, spec }: { area: string; title: string; rows: any[]; color: string; slot: string; spec: Spec }) {
  return <section style={{ ...CARD, gridArea: area }}>
    <h2 style={H2}>{title}</h2>
    {spec.slots.includes(slot) && rows.length ? <div role="group" aria-label={title + ". Select a row to filter by it."} style={{ display: "flex", flexDirection: "column", gap: 2, marginTop: 0, flex: 1, justifyContent: "space-around" }}>
      {/* horizontal bar chart: label | bar (length = count / largest count) | value */}
      {rows.map((r) => <button key={r.l} type="button" aria-label={r.label} aria-pressed={r.pr} onClick={r.pick} style={{ fontFamily: FONT, display: "flex", alignItems: "center", gap: 6, width: "100%", background: "none", border: 0, padding: 0, cursor: "pointer", opacity: r.op }}>
        {/* labels wrap only between words, never mid-word ("Firew...", "Charc...") */}
        <span style={{ display: "block", flex: "0 0 38%", minWidth: 0, textAlign: "right", fontSize: 10, color: "#1F2430", whiteSpace: "normal", overflowWrap: "normal", wordBreak: "normal", lineHeight: 1.1 }}>{r.l}</span>
        <span style={{ display: "block", flex: 1, minWidth: 0, height: 10, borderRadius: 3, background: "#ECEEF2", overflow: "hidden" }}><span style={{ display: "block", width: r.h + "%", height: "100%", background: color, borderRadius: 3, outline: r.pr ? "2px solid #7B2A3D" : "none" }} /></span>
        <span style={{ display: "block", minWidth: 26, flex: "none", textAlign: "left", fontSize: 10, fontWeight: 700, color: "#1F2430" }}>{r.n}</span>
      </button>)}
    </div> : <UnboundBody reason={slotReason(spec, slot) || "No data source registered"} />}
  </section>;
}

function DonutCard({ area, o, prefix, title, slot, spec }: { area: string; o: any; prefix: string; title?: string; slot: string; spec: Spec }) {
  const bound = spec.slots.includes(slot) && (prefix === "mar" ? o.marBound : o[prefix + "Bound"]);
  return <section style={{ ...CARD, gridArea: area }}>
    {title ? <h2 style={H2}>{title}</h2> : bound ? <Chips items={o[prefix + "Chips"]} /> : null}
    {bound ? <div style={{ flex: 1, minHeight: 0, display: "flex", alignItems: "center", justifyContent: "center" }}><div style={{ position: "relative", height: "100%", maxHeight: 150, aspectRatio: "160 / 150", maxWidth: "100%" }}>
      {/* slices are mouse shortcuts; the labels beside them are the keyboard-operable buttons */}
      <svg role="img" aria-label={(title || "Breakdown") + " donut. Select a label to filter by it."} viewBox="0 0 160 150" width="100%" height="100%" style={{ display: "block" }}>
        {o[prefix + "Segs"].map((s: any, i: number) => <circle key={i} cx="80" cy="75" r="27" fill="none" stroke={s.c} strokeWidth="13" pathLength={100} strokeDasharray={s.dash} strokeDashoffset={s.off} transform="rotate(-90 80 75)" opacity={s.op} onClick={s.pick} style={{ cursor: "pointer" }} />)}
        {o[prefix + "Leaders"].map((l: any, i: number) => <path key={i} d={l.d} fill="none" stroke={l.c} strokeWidth="1" opacity={l.op} />)}
      </svg>
      <span style={{ position: "absolute", left: "50%", top: "50%", transform: "translate(-50%,-50%)", fontSize: 13, fontWeight: 700, color: "#7B2A3D" }}>{o[prefix + "Total"]}</span>
      {o[prefix + "Labels"].map((g: any) => <button key={g.name} type="button" aria-label={g.label} aria-pressed={g.pr} onClick={g.pick} style={{ fontFamily: FONT, position: "absolute", left: (50 + g.dx / 160 * 100) + "%", top: (50 + g.dy / 150 * 100) + "%", transform: g.tr, background: "none", border: 0, padding: 0, textAlign: "center", lineHeight: 1.15, cursor: "pointer", opacity: g.op, maxWidth: 96, whiteSpace: "nowrap" }}>
        <span style={{ display: "block", fontSize: 10, fontWeight: 600, color: g.c }}>{g.name}</span>
        <span style={{ display: "block", fontSize: 10, color: "#1F2430" }}>{g.n}</span>
      </button>)}
    </div></div> : <UnboundBody reason={slotReason(spec, slot) || "No data source registered"} />}
  </section>;
}

function UnboundBody({ reason }: { reason: string }) {
  return <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", textAlign: "center", fontSize: 10, color: "#6B7280", border: "1px dashed #D0D5DD", borderRadius: 12, padding: 12 }}>{reason}</div>;
}
function Unbound({ title, reason, style }: { title: string; reason: string; style: React.CSSProperties }) {
  return <div style={{ ...CARD, ...style, justifyContent: "space-between" }}><span style={{ fontSize: 11, color: "#6B7280" }}>{title}</span><span style={{ fontSize: 10, color: "#6B7280" }}>{reason}</span></div>;
}

function VillageMap({ pins, count }: { pins: any[]; count: string }) {
  const host = useRef<HTMLDivElement>(null), map = useRef<L.Map | null>(null), layer = useRef<L.LayerGroup | null>(null);
  const latest = useRef(pins);
  latest.current = pins; // markers are rebuilt only when pins move; clicks always use the current handlers
  useEffect(() => {
    if (!host.current) return;
    map.current = L.map(host.current, { zoomControl: true, attributionControl: true }).setView([0, 0], 2);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>', maxZoom: 19 }).addTo(map.current);
    layer.current = L.layerGroup().addTo(map.current);
    const observer = new ResizeObserver(() => map.current?.invalidateSize());
    observer.observe(host.current);
    return () => { observer.disconnect(); map.current?.remove(); map.current = null; };
  }, []);
  const key = useMemo(() => JSON.stringify(pins.map((p) => [p.lat, p.lon, p.s, p.bw, p.label])), [pins]);
  useEffect(() => {
    if (!map.current || !layer.current) return;
    layer.current.clearLayers();
    pins.forEach((p, i) => {
      const pick = () => latest.current[i]?.pick();
      const icon = L.divIcon({ className: "hdss-pin", iconSize: [p.s, p.s], iconAnchor: [p.s / 2, p.s / 2],
        html: `<span style="display:block;width:${p.s}px;height:${p.s}px;border-radius:50%;background:${p.bg};border:${p.bw}px solid ${p.bd};box-sizing:border-box"></span>` });
      const marker = L.marker([p.lat, p.lon], { icon, keyboard: true, title: p.label, alt: p.label }).on("click", pick).addTo(layer.current!);
      const el = marker.getElement();
      if (el) { el.setAttribute("aria-label", p.label); el.setAttribute("role", "button"); el.addEventListener("keydown", (e) => { if (e.key === " ") { e.preventDefault(); pick(); } }); }
    });
    // the map zooms to the selection
    if (pins.length) map.current.fitBounds(L.latLngBounds(pins.map((p) => [p.lat, p.lon] as [number, number])), { padding: [60, 60], maxZoom: 14 });
  }, [key]);
  return <div role="group" aria-label="Map of village locations. Select a village to filter every card." style={{ position: "relative", flex: 1, minHeight: 0, borderRadius: 12, border: "1px solid #E2E6EC", overflow: "hidden" }}>
    <div ref={host} style={{ position: "absolute", inset: 0 }} />
    <div style={{ position: "absolute", left: "50%", bottom: 10, transform: "translateX(-50%)", fontSize: 10, color: "#6B7280", zIndex: 500, background: "rgba(255,255,255,0.85)", padding: "1px 8px", borderRadius: 6 }}>{count}</div>
  </div>;
}

function StatusBar({ publicationId, status, canEdit, onChange }: { publicationId: string; status: string; canEdit: boolean; onChange: (s: string) => void }) {
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const change = (next: string) => {
    setBusy(true); setError("");
    fetch(`/lifedata-ai/api/hdss/dashboards/${publicationId}/status/`, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: next }) })
      .then(async (r) => { const b = await r.json(); if (!r.ok) throw new Error(b.error || "Not saved"); onChange(b.status); })
      .catch((e) => setError(e.message)).finally(() => setBusy(false));
  };
  return <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "6px 0", fontFamily: FONT, fontSize: 10, color: "#C9CED8", flex: "none" }}>
    <a href="/lifedata-ai/dashboards/" style={{ color: "#C9CED8" }}>Dashboards</a>
    <span style={{ padding: "2px 10px", borderRadius: 999, background: status === "published" ? "#1F6F4A" : "#6B4E16", color: "#FFFFFF" }}>{status === "published" ? "Published" : "Draft"}</span>
    {canEdit && <button type="button" disabled={busy} onClick={() => change(status === "published" ? "draft" : "published")} style={{ fontFamily: FONT, fontSize: 10, padding: "4px 12px", borderRadius: 8, border: "1px solid #3A4152", background: "transparent", color: "#FFFFFF", cursor: "pointer" }}>
      {status === "published" ? "Return to draft" : "Publish"}</button>}
    {error && <span role="alert" style={{ color: "#FDA29B" }}>{error}</span>}
  </div>;
}

type TemplateDataset = { key: string; label: string; site: string; synthetic: boolean; bound_slots: number; slots: number };
/** "New HDSS dashboard" on the /dashboards/ index: registered datasets only, saved as a draft. */
export function HdssCreator({ onClose }: { onClose: () => void }) {
  const [datasets, setDatasets] = useState<TemplateDataset[]>();
  const [selected, setSelected] = useState<TemplateDataset>();
  const [title, setTitle] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ url: string; unbound: string[] }>();
  useEffect(() => {
    fetch("/lifedata-ai/api/hdss/templates/", { credentials: "same-origin" })
      .then(async (r) => { const b = await r.json(); if (!r.ok) throw new Error(b.error || "Request failed"); setDatasets(b.templates[0].datasets); })
      .catch((e) => setError(e.message));
  }, []);
  const create = () => {
    if (!selected || !title.trim() || busy) return;
    setBusy(true); setError("");
    fetch("/lifedata-ai/api/hdss/dashboards/", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dataset: selected.key, title: title.trim() }) })
      .then(async (r) => { const b = await r.json(); if (!r.ok) throw new Error(b.error || "Not created"); setResult({ url: b.url, unbound: b.unbound }); })
      .catch((e) => setError(e.message)).finally(() => setBusy(false));
  };
  return <div className="creator-overlay" onClick={onClose}>
    <aside className="creator-panel" role="dialog" aria-label="New HDSS dashboard" onClick={(e) => e.stopPropagation()}>
      <header className="creator-head"><h2>New HDSS dashboard</h2><button className="creator-close" onClick={onClose} aria-label="Close">×</button></header>
      {result ? <div className="creator-form">
        <p className="creator-lead">Saved as a draft. Only you can see it until you publish it.</p>
        {result.unbound.length ? <><p className="creator-note">These parts have no registered source and stay hidden:</p><ul className="creator-note">{result.unbound.map((u) => <li key={u}>{u}</li>)}</ul></> : null}
        <a className="home-cta" href={result.url}>Open the draft</a>
      </div> : !selected ? <>
        <p className="creator-lead">The approved HDSS page, built from a registered site dataset. Every card is also a filter; parts without a registered source are hidden, never invented.</p>
        <div className="creator-list">
          {datasets === undefined ? <div className="empty">{error || "Loading registered datasets…"}</div> : datasets.length ? datasets.map((d) =>
            <button key={d.key} className="creator-row" onClick={() => { setSelected(d); setTitle(d.site + " dashboard"); }}>
              <span>{d.label}</span><span className="creator-tag">{d.bound_slots}/{d.slots} parts bound{d.synthetic ? " · illustrative" : ""}</span>
            </button>) : <div className="empty">No registered HDSS dataset you can read.</div>}
        </div>
      </> : <div className="creator-form">
        <button className="creator-back" onClick={() => setSelected(undefined)}>‹ Choose a different dataset</button>
        <label className="creator-field">Title<input value={title} maxLength={120} onChange={(e) => setTitle(e.target.value)} /></label>
        {error && <div className="error">{error}</div>}
        <button className="home-cta" disabled={busy || !title.trim()} onClick={create}>{busy ? "Creating…" : "Create draft"}</button>
      </div>}
    </aside>
  </div>;
}

/** KPI trend line over the displayed time window; hover (or touch) shows the figure for that time bucket. */
function Sparkline({ k }: { k: any }) {
  const [hover, setHover] = useState<number | null>(null);
  const pts: { x: number; y: number; label: string; value: string }[] = k.points || [];
  const pick = (clientX: number, el: HTMLElement) => {
    if (!pts.length) return;
    const r = el.getBoundingClientRect(), x = (clientX - r.left) / r.width * 300;
    let best = 0; pts.forEach((p, i) => { if (Math.abs(p.x - x) < Math.abs(pts[best].x - x)) best = i; });
    setHover(best);
  };
  const p = hover === null ? null : pts[hover];
  const total = pts.length ? pts[0].label + " – " + pts[pts.length - 1].label : "";
  return <span style={{ position: "relative", display: "block", margin: "2px -14px 0", width: "calc(100% + 28px)" }}
    onMouseMove={(e) => pick(e.clientX, e.currentTarget)} onMouseLeave={() => setHover(null)}
    onTouchStart={(e) => pick(e.touches[0].clientX, e.currentTarget)} onTouchEnd={() => setHover(null)}>
    <span style={{ position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0 0 0 0)" }}>{k.label} trend {total}: {pts.map((q) => q.label + " " + q.value).join(", ")}</span>
    <svg aria-hidden="true" viewBox="0 0 300 60" preserveAspectRatio="none" width="100%" height="26" style={{ display: "block", width: "100%" }}>
      <path d={k.area} fill={k.fill} stroke="none" /><path d={k.line} fill="none" stroke={k.stroke} strokeWidth="2" style={{ vectorEffect: "non-scaling-stroke" } as React.CSSProperties} />
      {p && <line x1={p.x} x2={p.x} y1={0} y2={60} stroke="#1F2430" strokeOpacity="0.35" strokeWidth="1" style={{ vectorEffect: "non-scaling-stroke" } as React.CSSProperties} />}
    </svg>
    {p && <span style={{ position: "absolute", left: (p.x / 300 * 100) + "%", top: (p.y / 60 * 26) + "px", width: 7, height: 7, marginLeft: -3.5, marginTop: -3.5, borderRadius: "50%", background: k.stroke, boxShadow: "0 0 0 2px #FFFFFF", pointerEvents: "none" }} />}
    {p && <span role="status" style={{ position: "absolute", bottom: "100%", left: (p.x / 300 * 100) + "%", transform: p.x < 60 ? "translateX(0)" : p.x > 240 ? "translateX(-100%)" : "translateX(-50%)",
      marginBottom: 2, padding: "2px 6px", borderRadius: 5, background: "#1F2430", color: "#FFFFFF", fontSize: 10, fontWeight: 600, whiteSpace: "nowrap", pointerEvents: "none", zIndex: 2 }}>{p.label}: {p.value}</span>}
  </span>;
}
