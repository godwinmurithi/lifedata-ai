// The hdss-master page: fetches its effective spec, keeps ONE filter state for all three tabs, asks the server for
// aggregates (POST /lifedata-ai/dashboards/<id>/query) and renders the design (master-view.tsx, generated).
import React, { useEffect, useRef, useState } from "react";
import { Spec, State, initialState, renderVals, serverState } from "./master-model";
import { MasterView } from "./master-view";
import { MASTER_FIT_CSS } from "./master-parts";
import { StatusBar } from "./hdss-template";

export default function MasterTemplate({ publicationId, canEdit }: { publicationId: string; title: string; canEdit: boolean }) {
  const [spec, setSpec] = useState<(Spec & { status: string }) | null>(null);
  const [data, setData] = useState<any>(null);
  const [S, setS] = useState<State | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    fetch(`/lifedata-ai/api/hdss/dashboards/${publicationId}/spec/`, { credentials: "same-origin" })
      .then(async (r) => { const b = await r.json(); if (!r.ok) throw new Error(b.error || `Request failed (${r.status})`); setSpec(b); setS(initialState(b)); })
      .catch((e) => setError(e.message));
  }, [publicationId]);
  // one request in flight; while it runs only the latest filter state is kept (tab switches need no request)
  const committed = S ? JSON.stringify(serverState(S)) : "";
  const inflight = useRef(false), pending = useRef<string | null>(null);
  const pump = () => {
    if (inflight.current || pending.current === null) return;
    const body = pending.current; pending.current = null; inflight.current = true; setBusy(true);
    fetch(`/lifedata-ai/dashboards/${publicationId}/query`, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state: JSON.parse(body) }) })
      .then(async (r) => { const b = await r.json(); if (!r.ok) throw new Error(b.error || `Request failed (${r.status})`); if (pending.current === null) { setData(b.data); setError(""); } })
      .catch((e) => setError(e.message))
      .finally(() => { inflight.current = false; if (pending.current === null) setBusy(false); pump(); });
  };
  useEffect(() => { if (!spec || !S) return; pending.current = committed; pump(); }, [spec, committed]);
  if (!spec || !S || !data) {
    return <div style={{ fontFamily: "'Inter',sans-serif", background: "#0E0F11", minHeight: "100vh", color: "#FFFFFF", padding: 28 }}>{error ? <span role="alert">{error}</span> : "Loading…"}</div>;
  }
  const o = renderVals(S, data, spec, setS);
  return <div className="mf-frame">
    <style>{MASTER_FIT_CSS}</style>
    <div className="mf-top">
      <StatusBar publicationId={publicationId} status={spec.status} canEdit={canEdit} onChange={(status) => setSpec({ ...spec, status })} />
      <h1>{spec.title}</h1>
      {busy && <span style={{ fontSize: 12, color: "#B9B9B9" }}>Updating…</span>}
      {error && <span role="alert" style={{ fontSize: 13, color: "#FDA29B" }}>{error}</span>}
    </div>
    <MasterView o={o} />
  </div>;
}
