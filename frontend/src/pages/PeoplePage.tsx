import { useEffect, useState } from "react";
import { listCameras } from "../api/cameras";
import { PlaybackPopup } from "../components/PlaybackPopup";
import {
  assignGroup,
  createPerson,
  deletePerson,
  discardGroup,
  faceCaptureStats,
  faceReadiness,
  downloadFaceModels,
  faceModels,
  forgetAllFaces,
  listPeople,
  listUnknownGroups,
  sampleCropUrl,
  updatePerson,
  uploadPhotos,
  type FaceCaptureStats,
  type FaceReadiness,
  type FaceGroup,
  type Person,
  type PersonCategory,
  type PersonInput,
  type PersonNotify,
} from "../api/faces";

const TRUST: { key: PersonCategory; label: string; hint: string }[] = [
  { key: "household", label: "Family", hint: "lives here; may make Home-mode alerts quieter" },
  { key: "familiar", label: "Familiar", hint: "milkman, cook, driver, friends, relatives" },
  { key: "stranger", label: "Stranger", hint: "seen before but not trusted - treated like an unknown face" },
  { key: "watchlist", label: "Suspicious", hint: "always urgent, in every mode" },
];

const NOTIFY: { key: PersonNotify; label: string }[] = [
  { key: "always", label: "Yes" },
  { key: "away", label: "Only when Away" },
  { key: "never", label: "No" },
];

/** What "Notify on arrival" starts at when a trust level is picked. */
const DEFAULT_NOTIFY: Record<PersonCategory, PersonNotify> = {
  household: "always",
  familiar: "away",
  stranger: "always",
  watchlist: "always",
};

const RELATIONS = [
  "Mother", "Father", "Spouse", "Son", "Daughter", "Grandparent", "Milkman", "Cook", "Maid", "Driver",
  "Gardener", "Delivery", "Security guard", "Neighbour", "Friend", "Relative",
];

const STRENGTH: Record<Person["strength"], { dots: string; label: string; color: string }> = {
  weak: { dots: "●○○", label: "Weak", color: "var(--danger)" },
  good: { dots: "●●○", label: "Good", color: "var(--warning, #c90)" },
  strong: { dots: "●●●", label: "Strong", color: "var(--success, #2a2)" },
};

const blank = (category: PersonCategory = "familiar"): PersonInput => ({
  name: "",
  note: "",
  category,
  notify: DEFAULT_NOTIFY[category],
  arrival_alert: true,
  arrival_cooldown_s: 1800,
});

const toInput = (p: Person): PersonInput => ({
  name: p.name,
  note: p.note,
  category: p.category,
  notify: p.notify,
  arrival_alert: p.arrival_alert,
  arrival_cooldown_s: p.arrival_cooldown_s,
});

const when = (iso: string) =>
  new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

/** "mostly around 7 am" when most sightings fall in the same hour. */
function usualTime(times: string[]): string {
  if (times.length < 3) return "";
  const hours = new Map<number, number>();
  times.forEach((t) => {
    const h = new Date(t).getHours();
    hours.set(h, (hours.get(h) ?? 0) + 1);
  });
  const [hour, n] = [...hours.entries()].sort((a, b) => b[1] - a[1])[0];
  if (n / times.length < 0.5) return "";
  const label = new Date(2000, 0, 1, hour).toLocaleTimeString([], { hour: "numeric" });
  return `mostly around ${label}`;
}

function TrustSelect({ value, onChange }: { value: PersonCategory; onChange: (v: PersonCategory) => void }) {
  return (
    <select value={value} title={TRUST.find((t) => t.key === value)?.hint} onChange={(e) => onChange(e.target.value as PersonCategory)}>
      {TRUST.map((t) => <option key={t.key} value={t.key} title={t.hint}>{t.label}</option>)}
    </select>
  );
}

function NotifySelect({ value, category, onChange }: { value: PersonNotify; category: PersonCategory; onChange: (v: PersonNotify) => void }) {
  if (category === "watchlist") return <span style={{ fontSize: 13, color: "var(--danger)" }}>Always (urgent)</span>;
  return (
    <select value={value} onChange={(e) => onChange(e.target.value as PersonNotify)}>
      {NOTIFY.map((n) => <option key={n.key} value={n.key}>{n.label}</option>)}
    </select>
  );
}

/** What a click on a face opens: that camera, at that moment. */
type PlayAt = { cameraId: number; at: number; title?: string };

function Face({ sampleId, size = 48, play, onPlay }: {
  sampleId: number | null;
  size?: number;
  play?: PlayAt | null;
  onPlay?: (p: PlayAt) => void;
}) {
  const style = { width: size, height: size, objectFit: "cover" as const, borderRadius: 6, background: "var(--bg)", display: "block" };
  if (!sampleId) return <div style={style} />;
  const img = <img src={sampleCropUrl(sampleId)} alt="" style={style} />;
  if (!play || !onPlay) return img;
  return (
    <button
      type="button"
      title="Play this moment"
      onClick={() => onPlay(play)}
      style={{ padding: 0, border: "none", background: "none", cursor: "pointer", position: "relative" }}
    >
      {img}
      <span style={{ position: "absolute", right: 3, bottom: 3, fontSize: 10, background: "rgba(0,0,0,0.6)", color: "#fff", borderRadius: 3, padding: "0 3px" }}>▶</span>
    </button>
  );
}

/** A text cell that saves when you leave it (or press Enter). */
function EditCell({ value, placeholder, list, onSave }: { value: string; placeholder: string; list?: string; onSave: (v: string) => void }) {
  const [v, setV] = useState(value);
  useEffect(() => setV(value), [value]);
  return (
    <input
      value={v}
      placeholder={placeholder}
      list={list}
      style={{ width: "100%", minWidth: 110 }}
      onChange={(e) => setV(e.target.value)}
      onBlur={() => v.trim() !== value && onSave(v.trim())}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
    />
  );
}

/** People LightNVR recognises, and faces it has seen but can't name yet. */
export function PeoplePage() {
  const [people, setPeople] = useState<Person[]>([]);
  const [groups, setGroups] = useState<FaceGroup[]>([]);
  const [cameraNames, setCameraNames] = useState<Record<number, string>>({});
  const [capture, setCapture] = useState<Record<string, FaceCaptureStats>>({});
  const [ready, setReady] = useState<FaceReadiness | null>(null);
  const [models, setModels] = useState<{ name: string; present: boolean; size_mb: number; license: string }[]>([]);
  const [draft, setDraft] = useState<PersonInput>(blank());
  const [playAt, setPlayAt] = useState<PlayAt | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = () => {
    listPeople().then(setPeople).catch((e) => setError((e as Error).message));
    listUnknownGroups().then(setGroups).catch(() => setGroups([]));
    faceModels().then((r) => setModels(r.models)).catch(() => setModels([]));
    faceCaptureStats().then(setCapture).catch(() => setCapture({}));
    faceReadiness().then(setReady).catch(() => setReady(null));
  };
  useEffect(() => {
    reload();
    listCameras().then((cs) => setCameraNames(Object.fromEntries(cs.map((c) => [c.id, c.name])))).catch(() => {});
  }, []);

  const modelsReady = models.length > 0 && models.every((m) => m.present);

  const run = async (fn: () => Promise<unknown>, done?: string) => {
    setError(null);
    setMsg(null);
    try {
      await fn();
      if (done) setMsg(done);
      reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const save = (p: Person, change: Partial<PersonInput>) => {
    const next = { ...toInput(p), ...change };
    if (change.category && !change.notify && p.category !== change.category) next.notify = DEFAULT_NOTIFY[change.category];
    run(() => updatePerson(p.id, next));
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
    <div className="page" style={{ maxWidth: 1100 }}>
      <div className="page-header">
        <h1>People</h1>
      </div>
      <datalist id="relations">{RELATIONS.map((r) => <option key={r} value={r} />)}</datalist>

      <div className="card" style={{ marginBottom: 16 }}>
        <p style={{ marginTop: 0, fontSize: 13, color: "var(--text-dim)" }}>
          Faces your cameras see appear under <strong>Who is this?</strong> — the same person's sightings are grouped,
          so you name each person once. Then alerts say <em>"Ramesh (milkman) arrived at Gate"</em>. Everything stays on
          this NVR; turn it on in Settings → AI (Recognition, with the privacy acknowledgement). Family can make
          Home-mode alerts quieter, never Away or Night alerts. Strangers are treated like unknown faces; suspicious
          people are always urgent.
        </p>
        {!modelsReady && (
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <button
              className="btn btn-sm btn-primary"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                await run(downloadFaceModels);
                setBusy(false);
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

      {error && <div className="error-text" style={{ marginBottom: 10 }}>{error}</div>}
      {msg && <div style={{ marginBottom: 10, fontSize: 13 }}>{msg}</div>}

      <Readiness ready={ready} />
      <CaptureHealth stats={capture} cameraNames={cameraNames} />

      <h2 style={{ fontSize: 16, margin: "0 0 8px" }}>Who is this?</h2>
      <div style={{ display: "grid", gap: 10, marginBottom: 20 }}>
        {groups.map((g) => (
          <WhoIsThis
            key={g.sample_ids[0]}
            group={g}
            people={people}
            cameraNames={cameraNames}
            onAssign={(target, done) => run(() => assignGroup(g.sample_ids, target), done)}
            onIgnore={() => run(() => discardGroup(g.sample_ids))}
            onPlay={setPlayAt}
          />
        ))}
        {!groups.length && (
          <div className="card" style={{ color: "var(--text-dim)", fontSize: 13 }}>
            No new faces. Unnamed faces are deleted after a few days.
          </div>
        )}
      </div>

      <h2 style={{ fontSize: 16, margin: "0 0 8px" }}>Known people</h2>
      <div className="table-wrap" style={{ marginBottom: 16 }}>
        <table>
          <thead>
            <tr>
              <th></th>
              <th>Name</th>
              <th>Relation</th>
              <th>Trust</th>
              <th>Notify on arrival</th>
              <th>Recognition</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {people.map((p) => (
              <tr key={p.id}>
                <td style={{ width: 56 }}>
                  <Face
                    sampleId={p.cover_sample_id}
                    play={p.cover_camera_id != null && p.cover_seen_at ? { cameraId: p.cover_camera_id, at: new Date(p.cover_seen_at).getTime(), title: p.name } : null}
                    onPlay={setPlayAt}
                  />
                </td>
                <td>
                  <EditCell value={p.name} placeholder="Name" onSave={(name) => save(p, { name })} />
                  {p.last_seen && <div style={{ fontSize: 11, color: "var(--text-dim)" }}>last seen {when(p.last_seen)}</div>}
                </td>
                <td><EditCell value={p.note ?? ""} placeholder="e.g. milkman" list="relations" onSave={(note) => save(p, { note })} /></td>
                <td><TrustSelect value={p.category} onChange={(category) => save(p, { category })} /></td>
                <td><NotifySelect value={p.notify} category={p.category} onChange={(notify) => save(p, { notify })} /></td>
                <td title={p.strength_hint || `${p.samples} face sample(s)`}>
                  <span style={{ color: STRENGTH[p.strength].color, letterSpacing: 1 }}>{STRENGTH[p.strength].dots}</span>{" "}
                  <span style={{ fontSize: 13 }}>{STRENGTH[p.strength].label}</span>
                  {p.strength_hint && <div style={{ fontSize: 11, color: "var(--text-dim)", maxWidth: 180 }}>{p.strength_hint}</div>}
                </td>
                <td style={{ whiteSpace: "nowrap" }}>
                  <label className="btn btn-sm" style={{ cursor: "pointer" }}>
                    Add photos
                    <input type="file" accept="image/*" multiple style={{ display: "none" }} onChange={(e) => upload(p, e.target.files)} />
                  </label>{" "}
                  <button
                    className="btn btn-sm"
                    onClick={() => window.confirm(`Delete ${p.name} and all their face data?`) && run(() => deletePerson(p.id))}
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
            <tr>
              <td style={{ fontSize: 12, color: "var(--text-dim)" }}>New</td>
              <td><input placeholder="Name" value={draft.name} style={{ width: "100%", minWidth: 110 }} onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></td>
              <td><input placeholder="e.g. milkman" list="relations" value={draft.note ?? ""} style={{ width: "100%", minWidth: 110 }} onChange={(e) => setDraft({ ...draft, note: e.target.value })} /></td>
              <td><TrustSelect value={draft.category} onChange={(category) => setDraft({ ...draft, category, notify: DEFAULT_NOTIFY[category] })} /></td>
              <td><NotifySelect value={draft.notify} category={draft.category} onChange={(notify) => setDraft({ ...draft, notify })} /></td>
              <td colSpan={2}>
                <button
                  className="btn btn-primary btn-sm"
                  disabled={!draft.name.trim() && (draft.category === "household" || draft.category === "familiar")}
                  onClick={() => run(async () => {
                    const p = await createPerson(draft);
                    setDraft(blank(draft.category));
                    return p;
                  }, "Added. Now use “Add photos” (3+ clear photos), or pick them under Who is this? when the cameras see them.")}
                >
                  Add person
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <button
        className="btn btn-sm"
        title="Family members and their photos are kept - delete them one by one if you really want them gone"
        onClick={() =>
          window.confirm(
            "Delete everyone except Family, and all unnamed faces?\n\nFamily members and their photos are kept. This cannot be undone.",
          ) && run(() => forgetAllFaces(), "Done - Family kept.")
        }
      >
        Forget everyone except Family
      </button>

      {playAt && (
        <PlaybackPopup
          cameraId={playAt.cameraId}
          cameraName={cameraNames[playAt.cameraId]}
          at={playAt.at}
          title={playAt.title}
          onClose={() => setPlayAt(null)}
        />
      )}
    </div>
  );
}

function WhoIsThis({
  group,
  people,
  cameraNames,
  onAssign,
  onIgnore,
  onPlay,
}: {
  group: FaceGroup;
  people: Person[];
  cameraNames: Record<number, string>;
  onAssign: (target: { person_id: number } | { person: PersonInput }, done: string) => void;
  onIgnore: () => void;
  onPlay: (p: PlayAt) => void;
}) {
  const [form, setForm] = useState<PersonInput>(blank());
  const sug = group.suggestion;
  const where = group.camera_ids.map((id) => cameraNames[id] ?? `camera ${id}`).join(", ");
  const usual = usualTime(group.seen_at);
  const seen = group.sightings === 1 ? `Seen once, ${when(group.last_seen)}` : `Seen ${group.sightings}× · ${when(group.first_seen)} – ${when(group.last_seen)}`;
  // Best faces first; each one plays the moment it was taken.
  const thumbs = (group.faces.length ? group.faces : group.sample_ids.map((id) => ({ id, camera_id: null, seen_at: null })))
    .slice(0, 4);
  const quick = (category: PersonCategory, label: string) =>
    onAssign({ person: { ...blank(category), name: "" } }, `Saved as ${label}.`);

  return (
    <div className="card" style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "flex-start" }}>
      <div style={{ display: "flex", gap: 4 }}>
        {thumbs.map((f, i) => (
          <Face
            key={f.id}
            sampleId={f.id}
            size={i === 0 ? 96 : 46}
            play={f.camera_id != null && f.seen_at ? { cameraId: f.camera_id, at: new Date(f.seen_at).getTime() } : null}
            onPlay={onPlay}
          />
        ))}
      </div>
      <div style={{ flex: 1, minWidth: 260 }}>
        <div style={{ fontSize: 13 }}>
          {seen}
          {usual && ` · ${usual}`}
          {where && ` · ${where}`}
        </div>
        {sug && (
          <button className="btn btn-sm btn-primary" style={{ marginTop: 6 }} onClick={() => onAssign({ person_id: sug.person_id }, `Added to ${sug.name}.`)}>
            {sug.band === "known" ? "Yes, it's " : "Possibly "}
            {sug.name}
          </button>
        )}
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 8, alignItems: "center" }}>
          <input placeholder="Name" value={form.name} style={{ width: 130 }} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <input placeholder="Relation" list="relations" value={form.note ?? ""} style={{ width: 120 }} onChange={(e) => setForm({ ...form, note: e.target.value })} />
          <TrustSelect value={form.category} onChange={(category) => setForm({ ...form, category, notify: DEFAULT_NOTIFY[category] })} />
          <NotifySelect value={form.notify} category={form.category} onChange={(notify) => setForm({ ...form, notify })} />
          <button
            className="btn btn-sm btn-primary"
            disabled={!form.name.trim() && (form.category === "household" || form.category === "familiar")}
            onClick={() => onAssign({ person: form }, `Saved ${form.name.trim() || "them"}.`)}
          >
            Save
          </button>
        </div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 8, alignItems: "center" }}>
          {people.length > 0 && (
            <select value="" onChange={(e) => e.target.value && onAssign({ person_id: Number(e.target.value) }, "Added.")}>
              <option value="">Add to someone known…</option>
              {people.map((p) => <option key={p.id} value={p.id}>{p.name}{p.note ? ` (${p.note})` : ""}</option>)}
            </select>
          )}
          <button className="btn btn-sm" title="Keep recognising them, treated like an unknown face" onClick={() => quick("stranger", "a stranger")}>
            Stranger
          </button>
          <button className="btn btn-sm" style={{ color: "var(--danger)" }} title="Always urgent when seen again" onClick={() => quick("watchlist", "suspicious")}>
            Suspicious
          </button>
          <button className="btn btn-sm" title="Delete these faces now" onClick={onIgnore}>Ignore</button>
        </div>
      </div>
    </div>
  );
}

/** Today's face capture per camera, with the likely reason when few faces are
 *  usable - so "it isn't capturing faces" has an answer on the page. */
function CaptureHealth({ stats, cameraNames }: { stats: Record<string, FaceCaptureStats>; cameraNames: Record<number, string> }) {
  const rows = Object.entries(stats).filter(([, s]) => s.checked > 0);
  if (!rows.length) {
    return (
      <div className="card" style={{ marginBottom: 16, fontSize: 13, color: "var(--text-dim)" }}>
        <strong style={{ color: "var(--text)" }}>Face capture today</strong>
        <div style={{ marginTop: 4 }}>
          No one has been checked for a face yet today. Faces are looked for while a <em>person</em> is seen during a
          motion event, so motion detection and AI must be on for the camera.
        </div>
      </div>
    );
  }
  const advice = (s: FaceCaptureStats): string => {
    if (s.usable / s.checked >= 0.2) return "Working well.";
    const worst = Math.max(s.no_face, s.too_small, s.low_quality);
    if (worst === s.no_face)
      return "People rarely face this camera - it may be mounted too high or looking down. Faces need the camera at about head height, angled no more than ~30° down.";
    if (worst === s.too_small)
      return `Faces are too small (typically ${s.median_face_px ?? "?"}px; 40px needed). Use the camera's full-resolution main stream, zoom in, or aim it where people come close.`;
    return "Faces are blurry, dark or turned away. Better light at night, a faster shutter (WDR/motion settings on the camera) or a spot where people face the camera helps.";
  };
  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <strong>Face capture today</strong>
      <div className="table-wrap" style={{ marginTop: 8, border: "none" }}>
        <table>
          <thead>
            <tr>
              <th>Camera</th>
              <th>People checked</th>
              <th>Usable faces</th>
              <th>Not facing camera</th>
              <th>Too small</th>
              <th>Blurry / turned</th>
              <th>What to do</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([id, s]) => (
              <tr key={id}>
                <td>
                  {cameraNames[Number(id)] ?? `Camera ${id}`}
                  <div style={{ fontSize: 11, color: "var(--text-dim)" }}>searched at {s.frame_width}px wide</div>
                </td>
                <td>{s.checked}</td>
                <td><strong>{s.usable}</strong></td>
                <td>{s.no_face}</td>
                <td>{s.too_small}</td>
                <td>{s.low_quality}</td>
                <td style={{ fontSize: 12, maxWidth: 320 }}>{advice(s)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** Everything that has to be on before any face can be captured - shown
 *  whenever something is off, so "it isn't capturing anyone" has an answer. */
function Readiness({ ready }: { ready: FaceReadiness | null }) {
  if (!ready) return null;
  const noMotion = ready.cameras.filter((c) => !c.motion);
  if (ready.ready && !noMotion.length) return null;
  return (
    <div className="card" style={{ marginBottom: 16, borderColor: "var(--warning)" }}>
      <strong>Faces can't be captured yet</strong>
      <p style={{ fontSize: 13, color: "var(--text-dim)", margin: "4px 0 8px" }}>
        A face is only looked for when a camera's motion detection starts an event, AI confirms a person in it, and
        face recognition is on. Fix the red items:
      </p>
      <div style={{ display: "grid", gap: 4, fontSize: 13 }}>
        {ready.checks.map((c) => (
          <div key={c.key} style={{ display: "flex", gap: 8 }}>
            <span style={{ color: c.ok ? "var(--success)" : "var(--danger)", width: 16 }}>{c.ok ? "✓" : "✗"}</span>
            <span style={{ flex: 1 }}>{c.label}</span>
            {!c.ok && <span style={{ color: "var(--text-dim)" }}>{c.fix}</span>}
          </div>
        ))}
        {ready.cameras.map((c) => (
          <div key={`cam-${c.id}`} style={{ display: "flex", gap: 8 }}>
            <span style={{ color: c.motion ? "var(--success)" : "var(--danger)", width: 16 }}>{c.motion ? "✓" : "✗"}</span>
            <span style={{ flex: 1 }}>Motion detection on '{c.name}'</span>
            {!c.motion && (
              <span style={{ color: "var(--text-dim)" }}>
                Cameras → edit → Motion detection (keeps {c.recording_mode} recording)
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
