import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { buildMediaUrl } from "../api/client";
import { getIncident, listIncidents, type Incident } from "../api/security";
import type { NvrEvent } from "../api/types";

function duration(i: Incident): string {
  if (!i.ended_at) return "ongoing";
  const s = Math.max(0, Math.round((new Date(i.ended_at).getTime() - new Date(i.started_at).getTime()) / 1000));
  return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
}

/** Visits: motion on one or more cameras that belongs together, in order. */
export function VisitsPage() {
  const [items, setItems] = useState<Incident[]>([]);
  const [open, setOpen] = useState<number | null>(null);
  const [events, setEvents] = useState<NvrEvent[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = () => listIncidents(100).then(setItems).catch((e) => setError((e as Error).message));
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, []);

  const toggle = (id: number) => {
    if (open === id) {
      setOpen(null);
      return;
    }
    setOpen(id);
    getIncident(id).then((r) => setEvents(r.events)).catch(() => setEvents([]));
  };

  return (
    <div className="page">
      <div className="page-header">
        <h1>Visits</h1>
      </div>
      {error && <div className="error-text">{error}</div>}
      {!items.length && <p style={{ color: "var(--text-dim)" }}>No visits yet. Each motion event becomes a visit; events on cameras in linked areas join the same one.</p>}
      <div style={{ display: "grid", gap: 10 }}>
        {items.map((i) => {
          const counts = Object.entries(i.counts ?? {}).map(([k, n]) => `${n} ${k}`).join(", ");
          return (
            <div key={i.id} className="card" style={{ cursor: "pointer" }} onClick={() => toggle(i.id)}>
              <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
                {i.snapshot_path && (
                  <img src={buildMediaUrl(`/security/incidents/${i.id}/snapshot.jpg`)} alt="" style={{ width: 120, borderRadius: 6 }} />
                )}
                <div style={{ flex: 1 }}>
                  <div>
                    <strong>{new Date(i.started_at).toLocaleString()}</strong> · {duration(i)}
                    {i.status === "open" && <span style={{ color: "#d29922" }}> · in progress</span>}
                  </div>
                  <div style={{ fontSize: 13 }}>{(i.areas_path ?? []).join(" → ") || "—"}</div>
                  <div style={{ fontSize: 13, color: "var(--text-dim)" }}>
                    {counts || "motion only"}
                    {i.identities?.length ? ` · ${i.identities.join(", ")}` : ""}
                    {i.mode ? ` · ${i.mode} mode` : ""}
                  </div>
                </div>
              </div>
              {open === i.id && (
                <div style={{ marginTop: 10 }} onClick={(e) => e.stopPropagation()}>
                  {i.summary && <pre style={{ whiteSpace: "pre-wrap", fontFamily: "inherit", fontSize: 13 }}>{i.summary}</pre>}
                  {events.map((e) => (
                    <div key={e.id} style={{ display: "flex", gap: 8, fontSize: 13, padding: "2px 0" }}>
                      <span style={{ minWidth: 80, color: "var(--text-dim)" }}>{new Date(e.created_at).toLocaleTimeString()}</span>
                      <Link to={`/events/${e.id}`} style={{ flex: 1 }}>{e.message.split("\n")[0]}</Link>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
