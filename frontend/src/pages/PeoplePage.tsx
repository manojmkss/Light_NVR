import { useEffect, useState } from "react";
import {
  assignSample,
  createPerson,
  deletePerson,
  deleteSample,
  downloadFaceModels,
  faceModels,
  forgetAllFaces,
  listPeople,
  listSamples,
  sampleCropUrl,
  updatePerson,
  uploadPhotos,
  type FaceSampleInfo,
  type Person,
  type PersonCategory,
} from "../api/faces";

const CATEGORIES: { key: PersonCategory; label: string; hint: string }[] = [
  { key: "household", label: "Household", hint: "family; may make Home-mode alerts quieter" },
  { key: "staff", label: "Regular", hint: "milkman, cook, driver…" },
  { key: "visitor", label: "Visitor", hint: "friends and relatives" },
  { key: "watchlist", label: "Watch list", hint: "always urgent" },
];

/** People LightNVR should recognise, and recent faces to name. */
export function PeoplePage() {
  const [people, setPeople] = useState<Person[]>([]);
  const [unknown, setUnknown] = useState<FaceSampleInfo[]>([]);
  const [models, setModels] = useState<{ name: string; present: boolean; size_mb: number; license: string }[]>([]);
  const [draft, setDraft] = useState({ name: "", note: "", category: "staff" as PersonCategory });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = () => {
    listPeople().then(setPeople).catch((e) => setError((e as Error).message));
    listSamples({ unassigned: true, limit: 60 }).then(setUnknown).catch(() => setUnknown([]));
    faceModels().then((r) => setModels(r.models)).catch(() => setModels([]));
  };
  useEffect(reload, []);

  const modelsReady = models.length > 0 && models.every((m) => m.present);

  const add = async () => {
    setError(null);
    try {
      await createPerson({ name: draft.name.trim(), note: draft.note.trim() || null, category: draft.category, arrival_alert: true, arrival_cooldown_s: 1800 });
      setDraft({ ...draft, name: "", note: "" });
      reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const upload = async (p: Person, files: FileList | null) => {
    if (!files?.length) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await uploadPhotos(p.id, Array.from(files));
      const ok = r.results.filter((x) => x.ok).length;
      const bad = r.results.filter((x) => !x.ok).map((x) => `${x.file}: ${x.reason}`);
      setMsg(`${p.name}: ${ok} photo(s) added${bad.length ? ` · skipped ${bad.join("; ")}` : ""}`);
      reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page" style={{ maxWidth: 1000 }}>
      <div className="page-header">
        <h1>People</h1>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <p style={{ marginTop: 0, fontSize: 13, color: "var(--text-dim)" }}>
          Name the people you want LightNVR to recognise — then alerts say <em>"Ramesh (milkman) arrived at Gate"</em>{" "}
          and faces it doesn't know are flagged. Everything stays on this NVR. Turn it on in Settings → AI
          (Recognition, with the privacy acknowledgement). A recognised household member can make Home-mode alerts
          quieter, but never Away or Night alerts.
        </p>
        {!modelsReady && (
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <button
              className="btn btn-sm btn-primary"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                setError(null);
                try {
                  await downloadFaceModels();
                  reload();
                } catch (e) {
                  setError((e as Error).message);
                } finally {
                  setBusy(false);
                }
              }}
            >
              {busy ? "Downloading…" : "Download face models"}
            </button>
            <span style={{ fontSize: 12, color: "var(--text-dim)" }}>
              {models.map((m) => `${m.name} ${m.size_mb} MB (${m.license})`).join(" · ")}
            </span>
          </div>
        )}
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <strong>Add a person</strong>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8 }}>
          <input placeholder="Name" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
          <input placeholder="Note (e.g. milkman)" value={draft.note} onChange={(e) => setDraft({ ...draft, note: e.target.value })} />
          <select value={draft.category} onChange={(e) => setDraft({ ...draft, category: e.target.value as PersonCategory })}>
            {CATEGORIES.map((c) => <option key={c.key} value={c.key}>{c.label} — {c.hint}</option>)}
          </select>
          <button className="btn btn-primary btn-sm" disabled={!draft.name.trim()} onClick={add}>Add</button>
        </div>
      </div>

      {error && <div className="error-text" style={{ marginBottom: 10 }}>{error}</div>}
      {msg && <div style={{ marginBottom: 10, fontSize: 13 }}>{msg}</div>}

      <div style={{ display: "grid", gap: 10, marginBottom: 16 }}>
        {people.map((p) => (
          <div key={p.id} className="card" style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            <div style={{ flex: 1, minWidth: 200 }}>
              <strong>{p.name}</strong> {p.note && <span style={{ color: "var(--text-dim)" }}>({p.note})</span>}
              <div style={{ fontSize: 12, color: "var(--text-dim)" }}>
                {CATEGORIES.find((c) => c.key === p.category)?.label} · {p.samples} face sample(s)
                {p.samples < 3 && " · add 3+ photos (front and slightly turned) for reliable recognition"}
              </div>
            </div>
            <label style={{ fontSize: 13, display: "flex", gap: 4, alignItems: "center" }}>
              <input
                type="checkbox"
                checked={p.arrival_alert}
                onChange={(e) => updatePerson(p.id, { ...p, arrival_alert: e.target.checked }).then(reload)}
              />
              Alert when they arrive
            </label>
            <label className="btn btn-sm" style={{ cursor: "pointer" }}>
              Add photos
              <input type="file" accept="image/*" multiple style={{ display: "none" }} onChange={(e) => upload(p, e.target.files)} />
            </label>
            <button
              className="btn btn-sm"
              onClick={() => window.confirm(`Delete ${p.name} and all their face data?`) && deletePerson(p.id).then(reload)}
            >
              Delete
            </button>
          </div>
        ))}
      </div>

      <div className="card">
        <strong>Recent faces — who is this?</strong>
        <p style={{ fontSize: 13, color: "var(--text-dim)" }}>
          Faces seen on your cameras. Adding one to a person teaches LightNVR what they look like; nothing is added
          automatically. Unnamed faces are deleted after a few days.
        </p>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
          {unknown.map((f) => (
            <div key={f.id} style={{ width: 140, fontSize: 12 }}>
              <img src={sampleCropUrl(f.id)} alt="" style={{ width: 140, height: 140, objectFit: "cover", borderRadius: 6 }} />
              <div style={{ color: "var(--text-dim)" }}>{new Date(f.created_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</div>
              {f.suggestion && (
                <button className="btn btn-sm" style={{ width: "100%" }} onClick={() => assignSample(f.id, f.suggestion!.person_id).then(reload)}>
                  {f.suggestion.band === "known" ? "Yes, " : "Possibly "}
                  {f.suggestion.name}
                </button>
              )}
              <select
                value=""
                style={{ width: "100%", marginTop: 4 }}
                onChange={(e) => e.target.value && assignSample(f.id, Number(e.target.value)).then(reload)}
              >
                <option value="">This is…</option>
                {people.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
              <button className="btn btn-sm" style={{ width: "100%", marginTop: 4 }} onClick={() => deleteSample(f.id).then(reload)}>
                Discard
              </button>
            </div>
          ))}
          {!unknown.length && <div style={{ color: "var(--text-dim)", fontSize: 13 }}>No faces waiting.</div>}
        </div>
      </div>

      <div style={{ marginTop: 16 }}>
        <button
          className="btn btn-sm"
          onClick={() => window.confirm("Delete ALL people and face data? This cannot be undone.") && forgetAllFaces().then(reload)}
        >
          Forget all faces
        </button>
      </div>
    </div>
  );
}
