import { useState } from "react";
import { Link } from "react-router-dom";
import { apiFetch } from "../api/client";
import { detectionSnapshotUrl } from "../api/ai";

interface Hit {
  id: number;
  camera_id: number;
  camera: string | null;
  label: string;
  confidence: number;
  created_at: string;
  attributes: Record<string, string>;
  zones: string[] | null;
  person: string | null;
  event_id: number | null;
  incident_id: number | null;
  has_snapshot: boolean;
}

interface Result {
  query: string;
  understood: { objects: string[]; colours: string[]; people: string[]; cameras: string[]; since: string | null; hours: number[] | null; places: string[] };
  results: Hit[];
}

const EXAMPLES = ["person in red shirt", "car yesterday", "package today", "Ramesh", "dog night", "person driveway"];

/** "What did the cameras see?" in plain words. */
export function SearchPage() {
  const [q, setQ] = useState("");
  const [res, setRes] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (query: string) => {
    setQ(query);
    setBusy(true);
    setError(null);
    try {
      setRes(await apiFetch<Result>(`/search?q=${encodeURIComponent(query)}`));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const u = res?.understood;
  return (
    <div className="page">
      <div className="page-header">
        <h1>Search</h1>
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          run(q);
        }}
        style={{ display: "flex", gap: 8, marginBottom: 8 }}
      >
        <input style={{ flex: 1 }} placeholder="e.g. person in red shirt yesterday" value={q} onChange={(e) => setQ(e.target.value)} />
        <button className="btn btn-primary" disabled={busy}>{busy ? "Searching…" : "Search"}</button>
      </form>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 12 }}>
        {EXAMPLES.map((x) => (
          <button key={x} className="btn btn-sm" onClick={() => run(x)}>{x}</button>
        ))}
      </div>
      <p style={{ fontSize: 12, color: "var(--text-dim)" }}>
        Searches the people, vehicles, animals and packages the AI saw: kind, clothing / vehicle colour (not at night in
        black-and-white), recognised names, camera, area or zone, and times like today, yesterday, night.
      </p>
      {error && <div className="error-text">{error}</div>}
      {u && (
        <div style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 10 }}>
          Looking for: {[...u.objects, ...u.colours, ...u.people.filter(Boolean), ...u.cameras.filter(Boolean), ...u.places].join(", ") || "everything"}
          {u.since ? ` since ${new Date(u.since).toLocaleString()}` : ""}
          {u.hours ? ` between ${u.hours[0]}:00 and ${u.hours[1]}:00` : ""} — {res?.results.length} result(s)
        </div>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: 10 }}>
        {res?.results.map((h) => (
          <div key={h.id} className="card" style={{ padding: 8 }}>
            {h.has_snapshot && <img src={detectionSnapshotUrl(h.id)} alt="" style={{ width: "100%", borderRadius: 6 }} />}
            <div style={{ fontSize: 13, marginTop: 4 }}>
              <strong>{h.person ?? h.label}</strong>
              {Object.values(h.attributes ?? {}).length ? ` · ${Object.entries(h.attributes).map(([k, v]) => (k === "colour" ? v : `${v} ${k}`)).join(", ")}` : ""}
            </div>
            <div style={{ fontSize: 12, color: "var(--text-dim)" }}>
              {h.camera} · {new Date(h.created_at).toLocaleString()}
              {h.zones?.length ? ` · ${h.zones.join(", ")}` : ""}
            </div>
            {h.event_id && <Link to={`/events/${h.event_id}`} style={{ fontSize: 12 }}>Open event</Link>}
          </div>
        ))}
      </div>
    </div>
  );
}
