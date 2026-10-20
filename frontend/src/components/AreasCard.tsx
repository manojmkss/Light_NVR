import { useEffect, useState } from "react";
import { createAreaLink, deleteAreaLink, getAreas, type AreaLink } from "../api/security";

/** Areas (set per camera) and the walking times between them, which is how
 *  events on several cameras are joined into one visit. */
export function AreasCard() {
  const [areas, setAreas] = useState<{ name: string; cameras: { id: number; name: string }[] }[]>([]);
  const [links, setLinks] = useState<AreaLink[]>([]);
  const [draft, setDraft] = useState({ area_a: "", area_b: "", min_seconds: 3, max_seconds: 40 });
  const [error, setError] = useState<string | null>(null);

  const reload = () =>
    getAreas()
      .then((r) => {
        setAreas(r.areas);
        setLinks(r.links);
      })
      .catch((e) => setError((e as Error).message));
  useEffect(() => {
    reload();
  }, []);

  const named = areas.filter((a) => a.name);
  const add = async () => {
    setError(null);
    try {
      await createAreaLink(draft);
      setDraft({ ...draft, area_a: "", area_b: "" });
      reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="card">
      <strong>Areas and visits</strong>
      <p style={{ color: "var(--text-dim)", fontSize: 13 }}>
        Give each camera an area (Cameras → Edit). Cameras in the same area are joined into one visit; to join two
        different areas, say how long it takes to walk between them. One visit gets one alert (plus one more if it
        reaches a private area such as the back yard) and one summary of the whole route.
      </p>
      <div style={{ fontSize: 13, marginBottom: 8 }}>
        {areas.map((a) => (
          <div key={a.name || "-"}>
            <strong>{a.name || "No area (whole property)"}</strong>: {a.cameras.map((c) => c.name).join(", ")}
          </div>
        ))}
      </div>
      {links.map((l) => (
        <div key={l.id} style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13, padding: "2px 0" }}>
          <span style={{ flex: 1 }}>
            {l.area_a} ↔ {l.area_b} · {l.min_seconds}–{l.max_seconds}s walk
          </span>
          <button className="btn btn-sm" onClick={() => deleteAreaLink(l.id).then(reload)}>Remove</button>
        </div>
      ))}
      {named.length >= 2 ? (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center", marginTop: 8 }}>
          <select value={draft.area_a} onChange={(e) => setDraft({ ...draft, area_a: e.target.value })}>
            <option value="">Area…</option>
            {named.map((a) => <option key={a.name} value={a.name}>{a.name}</option>)}
          </select>
          <span>↔</span>
          <select value={draft.area_b} onChange={(e) => setDraft({ ...draft, area_b: e.target.value })}>
            <option value="">Area…</option>
            {named.filter((a) => a.name !== draft.area_a).map((a) => <option key={a.name} value={a.name}>{a.name}</option>)}
          </select>
          <span>walk</span>
          <input type="number" min={0} style={{ width: 70 }} value={draft.min_seconds} onChange={(e) => setDraft({ ...draft, min_seconds: Number(e.target.value) })} />
          <span>to</span>
          <input type="number" min={1} style={{ width: 70 }} value={draft.max_seconds} onChange={(e) => setDraft({ ...draft, max_seconds: Number(e.target.value) })} />
          <span>seconds</span>
          <button className="btn btn-sm btn-primary" disabled={!draft.area_a || !draft.area_b} onClick={add}>Link</button>
        </div>
      ) : (
        <div className="muted">Name at least two areas on your cameras to link them.</div>
      )}
      {error && <div className="error-text" style={{ marginTop: 8 }}>{error}</div>}
    </div>
  );
}
