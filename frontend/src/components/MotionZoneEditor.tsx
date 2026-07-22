import { useMemo, useState } from "react";
import { buildMediaUrl } from "../api/client";
import { updateCamera } from "../api/cameras";
import type { Camera, MotionZone } from "../api/types";

// exclude = ignore motion here (red); include = only watch here (green).
const COLORS = {
  exclude: { stroke: "#e05555", fill: "rgba(224,85,85,0.28)" },
  include: { stroke: "#3fbf78", fill: "rgba(63,191,120,0.28)" },
} as const;

interface Props {
  camera: Camera;
  onClose: () => void;
  onSaved: () => void;
}

type Draft = { kind: MotionZone["kind"]; points: number[][] };

export function MotionZoneEditor({ camera, onClose, onSaved }: Props) {
  const [zones, setZones] = useState<MotionZone[]>(camera.motion_zones ?? []);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [snapshotFailed, setSnapshotFailed] = useState(false);

  // Cache-busted once per mount so the snapshot is fresh but stable while editing.
  const snapshotSrc = useMemo(
    () => buildMediaUrl(`/cameras/${camera.id}/snapshot.jpg?k=${Date.now()}`),
    [camera.id],
  );

  const pts = (points: number[][]) => points.map(([x, y]) => `${x},${y}`).join(" ");

  const handleClick = (e: React.MouseEvent<SVGSVGElement>) => {
    if (!draft) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const x = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    const y = Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height));
    setDraft({ ...draft, points: [...draft.points, [x, y]] });
  };

  const finishDraft = () => {
    if (draft && draft.points.length >= 3) {
      setZones((z) => [...z, { kind: draft.kind, points: draft.points }]);
    }
    setDraft(null);
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await updateCamera(camera.id, { motion_zones: zones });
      onSaved();
    } catch (err) {
      setError((err as Error).message);
      setSaving(false);
    }
  };

  const hasInclude = zones.some((z) => z.kind === "include");

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" style={{ maxWidth: 720 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Motion zones — {camera.name}</h2>
          <button className="close-btn" onClick={onClose}>×</button>
        </div>

        <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: -6 }}>
          Draw regions to <strong style={{ color: COLORS.exclude.stroke }}>ignore</strong> (a waving tree, a
          busy road) or, if you add any, to <strong style={{ color: COLORS.include.stroke }}>watch only</strong>.
          Click to place points; click <em>Finish</em> to close a shape (3+ points). Empty = the whole frame is watched.
        </p>

        <div
          style={{
            position: "relative",
            width: "100%",
            aspectRatio: "16 / 9",
            background: "#111",
            borderRadius: 8,
            overflow: "hidden",
            cursor: draft ? "crosshair" : "default",
          }}
        >
          {!snapshotFailed ? (
            <img
              src={snapshotSrc}
              alt=""
              onError={() => setSnapshotFailed(true)}
              style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "fill" }}
            />
          ) : (
            <div style={{ position: "absolute", inset: 0, display: "grid", placeItems: "center", color: "var(--text-dim)", fontSize: 13, textAlign: "center", padding: 20 }}>
              No live snapshot (camera offline). You can still draw zones — they'll apply once it's back.
            </div>
          )}

          <svg
            viewBox="0 0 1 1"
            preserveAspectRatio="none"
            onClick={handleClick}
            style={{ position: "absolute", inset: 0, width: "100%", height: "100%" }}
          >
            {zones.map((z, i) => (
              <polygon
                key={i}
                points={pts(z.points)}
                fill={COLORS[z.kind].fill}
                stroke={COLORS[z.kind].stroke}
                strokeWidth={2}
                vectorEffect="non-scaling-stroke"
              />
            ))}
            {draft && draft.points.length > 0 && (
              <>
                <polyline
                  points={pts(draft.points)}
                  fill="none"
                  stroke={COLORS[draft.kind].stroke}
                  strokeWidth={2}
                  strokeDasharray="4 3"
                  vectorEffect="non-scaling-stroke"
                />
                {draft.points.map(([x, y], i) => (
                  <circle key={i} cx={x} cy={y} r={0.008} fill={COLORS[draft.kind].stroke} />
                ))}
              </>
            )}
          </svg>
        </div>

        {/* Controls */}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 12 }}>
          {!draft ? (
            <>
              <button className="btn btn-sm" onClick={() => setDraft({ kind: "exclude", points: [] })}>
                + Ignore zone
              </button>
              <button className="btn btn-sm" onClick={() => setDraft({ kind: "include", points: [] })}>
                + Watch-only zone
              </button>
              {zones.length > 0 && (
                <button className="btn btn-sm" onClick={() => setZones([])}>
                  Clear all
                </button>
              )}
            </>
          ) : (
            <>
              <span style={{ fontSize: 13, color: "var(--text-dim)", alignSelf: "center" }}>
                Drawing {draft.kind === "exclude" ? "ignore" : "watch-only"} zone — {draft.points.length} point(s)
              </span>
              <button className="btn btn-sm btn-primary" disabled={draft.points.length < 3} onClick={finishDraft}>
                Finish
              </button>
              <button className="btn btn-sm" onClick={() => setDraft(null)}>
                Cancel
              </button>
            </>
          )}
        </div>

        {/* Zone list */}
        {zones.length > 0 && (
          <div style={{ marginTop: 10, fontSize: 13 }}>
            {zones.map((z, i) => (
              <div key={i} style={{ display: "flex", alignItems: "center", gap: 8, padding: "3px 0" }}>
                <span style={{ width: 10, height: 10, borderRadius: 2, background: COLORS[z.kind].stroke }} />
                <span style={{ flex: 1 }}>
                  {z.kind === "exclude" ? "Ignore" : "Watch only"} · {z.points.length} points
                </span>
                <button className="btn btn-sm" onClick={() => setZones((zs) => zs.filter((_, j) => j !== i))}>
                  Delete
                </button>
              </div>
            ))}
            {hasInclude && (
              <p style={{ color: "var(--text-dim)", fontSize: 12, marginTop: 4 }}>
                Watch-only zones are set, so motion is detected <em>only</em> inside them (minus any ignore zones).
              </p>
            )}
          </div>
        )}

        {error && <div className="error-text" style={{ marginTop: 8 }}>{error}</div>}
        <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
          <button className="btn btn-primary" disabled={saving || draft !== null} onClick={save}>
            {saving ? "Saving…" : "Save zones"}
          </button>
          <button className="btn" onClick={onClose}>Cancel</button>
        </div>
      </div>
    </div>
  );
}
