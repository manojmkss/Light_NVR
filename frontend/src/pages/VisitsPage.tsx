import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { listCameras } from "../api/cameras";
import { buildMediaUrl } from "../api/client";
import { listIncidentsWithEvents, type IncidentWithEvents } from "../api/security";
import { PlaybackPopup } from "../components/PlaybackPopup";
import { useSettings } from "../context/SettingsContext";

type Period = "today" | "yesterday" | "week";

const PERIODS: { key: Period; label: string }[] = [
  { key: "today", label: "Today" },
  { key: "yesterday", label: "Yesterday" },
  { key: "week", label: "Last 7 days" },
];

// The kinds of event worth listing under a visit (health/system events are on
// the Events page).
const VISIT_EVENT_TYPES = new Set([
  "motion", "motion_update", "motion_summary", "arrival", "incident_summary", "loitering", "line_cross",
  "package_left", "package_removed", "tamper",
]);

function range(period: Period): [Date, Date] {
  const midnight = new Date();
  midnight.setHours(0, 0, 0, 0);
  const day = 24 * 3600 * 1000;
  if (period === "today") return [midnight, new Date(midnight.getTime() + day)];
  if (period === "yesterday") return [new Date(midnight.getTime() - day), midnight];
  return [new Date(midnight.getTime() - 6 * day), new Date(midnight.getTime() + day)];
}

function duration(i: IncidentWithEvents): string {
  if (!i.ended_at) return "ongoing";
  const s = Math.max(0, Math.round((new Date(i.ended_at).getTime() - new Date(i.started_at).getTime()) / 1000));
  return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
}

/** Something was actually seen (AI-confirmed object or a recognised face) -
 *  as opposed to motion only: shadows, rain, headlights, swaying plants. */
const isReal = (i: IncidentWithEvents) => Object.keys(i.counts ?? {}).length > 0 || (i.identities?.length ?? 0) > 0;

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : word.endsWith("s") ? "es" : "s"}`;

/** Visits: motion on one or more cameras that belongs together, in order -
 *  with a plain-language summary of the period on top. */
export function VisitsPage() {
  const { timezone, fmtTime, fmtDateTime } = useSettings();
  const [period, setPeriod] = useState<Period>("today");
  const [realOnly, setRealOnly] = useState(true);
  const [items, setItems] = useState<IncidentWithEvents[]>([]);
  const [cameraNames, setCameraNames] = useState<Record<number, string>>({});
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [loaded, setLoaded] = useState(false);
  const [playAt, setPlayAt] = useState<{ cameraId: number; at: number; title?: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listCameras().then((cs) => setCameraNames(Object.fromEntries(cs.map((c) => [c.id, c.name])))).catch(() => {});
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoaded(false);
    const load = () => {
      const [start, end] = range(period);
      listIncidentsWithEvents(start, end)
        .then((r) => {
          if (cancelled) return;
          setItems(r);
          setError(null);
          setLoaded(true);
        })
        .catch((e) => !cancelled && setError((e as Error).message));
    };
    load();
    const t = setInterval(load, 15000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [period]);

  const real = useMemo(() => items.filter(isReal), [items]);
  const shown = realOnly ? real : items;
  const hourOf = useMemo(() => {
    const f = new Intl.DateTimeFormat([], { hour: "numeric", hourCycle: "h23", timeZone: timezone || undefined });
    return (iso: string) => Number(f.format(new Date(iso)));
  }, [timezone]);

  const digest = useMemo(() => {
    const objects: Record<string, number> = {};
    const people: Record<string, number> = {};
    const cameras: Record<string, number> = {};
    const hours: Record<string, number> = {};
    let unknownFaces = 0;
    for (const i of real) {
      for (const [label, n] of Object.entries(i.counts ?? {})) objects[label] = (objects[label] ?? 0) + n;
      for (const name of i.identities ?? []) people[name] = (people[name] ?? 0) + 1;
      for (const id of i.camera_ids ?? []) {
        const name = cameraNames[id] ?? `Camera ${id}`;
        cameras[name] = (cameras[name] ?? 0) + 1;
      }
      const h = String(hourOf(i.started_at));
      hours[h] = (hours[h] ?? 0) + 1;
      if (i.events.some((e) => (e.payload as { unknown_faces?: boolean } | null)?.unknown_faces)) unknownFaces += 1;
    }
    const top = (rec: Record<string, number>) => Object.entries(rec).sort((a, b) => b[1] - a[1]);
    return { objects: top(objects), people: top(people), cameras: top(cameras), busiest: top(hours)[0], unknownFaces };
  }, [real, cameraNames, hourOf]);

  const toggle = (id: number) =>
    setExpanded((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const hourLabel = (h: number) =>
    `${new Date(2000, 0, 1, h).toLocaleTimeString([], { hour: "numeric" })}–${new Date(2000, 0, 1, (h + 1) % 24).toLocaleTimeString([], { hour: "numeric" })}`;
  const periodLabel = PERIODS.find((p) => p.key === period)!.label;
  const motionOnly = items.length - real.length;

  return (
    <div className="page" style={{ maxWidth: 1000 }}>
      <div className="page-header" style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <h1 style={{ marginRight: "auto" }}>Visits</h1>
        <div style={{ display: "flex", gap: 4 }}>
          {PERIODS.map((p) => (
            <button key={p.key} className={`btn btn-sm${period === p.key ? " btn-primary" : ""}`} onClick={() => setPeriod(p.key)}>
              {p.label}
            </button>
          ))}
        </div>
        <label style={{ fontSize: 13, display: "flex", gap: 6, alignItems: "center" }}>
          <input type="checkbox" checked={realOnly} onChange={(e) => setRealOnly(e.target.checked)} />
          Real activity only
        </label>
      </div>
      {error && <div className="error-text">{error}</div>}

      <div className="card" style={{ marginBottom: 14 }}>
        <strong>{periodLabel}</strong>
        {!loaded ? (
          <p style={{ color: "var(--text-dim)", fontSize: 13 }}>Loading…</p>
        ) : !items.length ? (
          <p style={{ color: "var(--text-dim)", fontSize: 13, marginBottom: 0 }}>
            No visits. A visit is created when motion detection is on for a camera (Cameras → edit → Motion
            detection) and something moves; events on cameras in linked areas join the same visit.
          </p>
        ) : (
          <div style={{ fontSize: 14, lineHeight: 1.7, marginTop: 6 }}>
            <div>
              <strong>{plural(real.length, "visit")}</strong> with real activity
              {motionOnly > 0 && (
                <span style={{ color: "var(--text-dim)" }}>
                  {" "}· {motionOnly} motion-only (shadows, light, plants…){realOnly ? " hidden" : ""}
                </span>
              )}
            </div>
            {digest.objects.length > 0 && (
              <div>Seen: {digest.objects.map(([label, n]) => plural(n, label)).join(", ")}</div>
            )}
            {(digest.people.length > 0 || digest.unknownFaces > 0) && (
              <div>
                People:{" "}
                {[
                  ...digest.people.map(([name, n]) => `${name} (${n} visit${n === 1 ? "" : "s"})`),
                  ...(digest.unknownFaces ? [`unknown faces in ${plural(digest.unknownFaces, "visit")}`] : []),
                ].join(", ")}
              </div>
            )}
            {digest.cameras.length > 0 && (
              <div>Cameras: {digest.cameras.map(([name, n]) => `${name} ${n}`).join(" · ")}</div>
            )}
            {digest.busiest && real.length > 2 && <div>Busiest: {hourLabel(Number(digest.busiest[0]))}</div>}
            {real.length > 0 && (
              <div style={{ color: "var(--text-dim)", fontSize: 13 }}>
                First {fmtTime(real[real.length - 1].started_at)} · last {fmtTime(real[0].started_at)}
              </div>
            )}
          </div>
        )}
      </div>

      <div style={{ display: "grid", gap: 10 }}>
        {shown.map((i) => {
          const counts = Object.entries(i.counts ?? {}).map(([k, n]) => plural(n, k)).join(", ");
          const events = i.events.filter((e) => VISIT_EVENT_TYPES.has(e.type));
          const open = expanded.has(i.id);
          const list = open ? events : events.slice(0, 4);
          const summary = (i.summary ?? "").split("\n").filter(Boolean);
          return (
            <div key={i.id} className="card">
              <div style={{ display: "flex", gap: 12, alignItems: "flex-start", flexWrap: "wrap" }}>
                {i.snapshot_path && (
                  <img
                    src={buildMediaUrl(`/security/incidents/${i.id}/snapshot.jpg`)}
                    alt=""
                    title={i.camera_ids?.length ? "Play this visit" : undefined}
                    style={{ width: 140, borderRadius: 6, cursor: i.camera_ids?.length ? "pointer" : undefined }}
                    onClick={() =>
                      i.camera_ids?.length &&
                      setPlayAt({ cameraId: i.camera_ids[0], at: new Date(i.started_at).getTime(), title: "Visit" })
                    }
                  />
                )}
                <div style={{ flex: 1, minWidth: 240 }}>
                  <div>
                    <strong>{period === "week" ? fmtDateTime(i.started_at) : fmtTime(i.started_at)}</strong> · {duration(i)}
                    {i.status === "open" && <span style={{ color: "#d29922" }}> · in progress</span>}
                    {i.mode && <span style={{ color: "var(--text-dim)" }}> · {i.mode} mode</span>}
                  </div>
                  <div style={{ fontSize: 13 }}>{(i.areas_path ?? []).join(" → ") || "—"}</div>
                  <div style={{ fontSize: 13 }}>
                    {counts || <span style={{ color: "var(--text-dim)" }}>motion only - nothing recognised</span>}
                    {i.identities?.length ? <strong> · {i.identities.join(", ")}</strong> : ""}
                  </div>
                  {summary.length > 0 && (
                    <div style={{ fontSize: 13, color: "var(--text-dim)", marginTop: 4, whiteSpace: "pre-wrap" }}>
                      {(open ? summary : summary.slice(0, 3)).join("\n")}
                    </div>
                  )}
                </div>
              </div>
              {events.length > 0 && (
                <div style={{ marginTop: 8, borderTop: "1px solid var(--border)", paddingTop: 6 }}>
                  {list.map((e) => (
                    <div key={e.id} style={{ display: "flex", gap: 8, fontSize: 13, padding: "2px 0" }}>
                      <span style={{ minWidth: 70, color: "var(--text-dim)" }}>{fmtTime(e.created_at)}</span>
                      {e.camera_id != null ? (
                        <button
                          className="btn btn-sm"
                          title="Play this moment"
                          style={{ padding: "0 6px", lineHeight: "18px" }}
                          onClick={() => setPlayAt({ cameraId: e.camera_id!, at: new Date(e.created_at).getTime() })}
                        >
                          ▶
                        </button>
                      ) : (
                        <span style={{ width: 26 }} />
                      )}
                      <Link to={`/events/${e.id}`} style={{ flex: 1 }}>
                        {e.message.split("\n")[0]}
                      </Link>
                    </div>
                  ))}
                </div>
              )}
              {(events.length > 4 || summary.length > 3) && (
                <button className="btn btn-sm" style={{ marginTop: 6 }} onClick={() => toggle(i.id)}>
                  {open ? "Show less" : `Show all${events.length > 4 ? ` ${events.length} events` : ""}`}
                </button>
              )}
            </div>
          );
        })}
        {playAt && (
          <PlaybackPopup
            cameraId={playAt.cameraId}
            cameraName={cameraNames[playAt.cameraId]}
            at={playAt.at}
            title={playAt.title}
            onClose={() => setPlayAt(null)}
          />
        )}
        {loaded && items.length > 0 && !shown.length && (
          <div className="card" style={{ color: "var(--text-dim)", fontSize: 13 }}>
            Only motion-only visits in this period - nothing was recognised. Untick "Real activity only" to see them.
          </div>
        )}
      </div>
    </div>
  );
}
