import { useEffect, useMemo, useState } from "react";
import { buildMediaUrl } from "../api/client";
import {
  bulkDeleteRecordings,
  bulkDownloadZipUrl,
  deleteRecording,
  listRecordings,
  recordingDownloadUrl,
} from "../api/recordings";
import { listCameras } from "../api/cameras";
import type { Camera, Recording } from "../api/types";
import { useAuth } from "../context/AuthContext";
import { useSettings } from "../context/SettingsContext";
import { localDateTimeToUtcIso, partsInTz } from "../utils/datetime";

const PAGE_SIZE = 200;

type ViewMode = "tiles" | "details" | "preview";

function formatDuration(seconds: number | null): string {
  if (!seconds) return "—";
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function formatSize(bytes: number | null): string {
  if (!bytes) return "—";
  const mb = bytes / 1024 ** 2;
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb.toFixed(0)} MB`;
}

function minutesOfDay(iso: string, tz: string): number {
  const p = partsInTz(new Date(iso).getTime(), tz);
  return p.hour * 60 + p.minute;
}

function parseHm(hm: string): number | null {
  if (!hm) return null;
  const [h, m] = hm.split(":").map(Number);
  if (Number.isNaN(h)) return null;
  return h * 60 + (m || 0);
}

/** Force the browser to download a URL. The server sets Content-Disposition,
 *  so this works even though the anchor points at the API. */
function triggerDownload(url: string) {
  const a = document.createElement("a");
  a.href = url;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

export function RecordingsPage() {
  const { isAdmin } = useAuth();
  const { fmtDateTime, timezone } = useSettings();

  const [cameras, setCameras] = useState<Camera[]>([]);
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [loading, setLoading] = useState(true);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);

  // Filters
  const [cameraFilter, setCameraFilter] = useState("");
  const [triggerFilter, setTriggerFilter] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [startTime, setStartTime] = useState("");
  const [endTime, setEndTime] = useState("");

  // Selection + view
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [viewMode, setViewMode] = useState<ViewMode>(
    () => (localStorage.getItem("lightnvr_rec_view") as ViewMode) || "tiles",
  );
  const [preview, setPreview] = useState<Recording | null>(null);
  const [playing, setPlaying] = useState<Recording | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    listCameras().then(setCameras);
  }, []);

  useEffect(() => {
    localStorage.setItem("lightnvr_rec_view", viewMode);
  }, [viewMode]);

  const load = (reset: boolean) => {
    const off = reset ? 0 : offset;
    setLoading(true);
    listRecordings({
      camera_id: cameraFilter ? Number(cameraFilter) : undefined,
      trigger: triggerFilter || undefined,
      start: startDate ? localDateTimeToUtcIso(startDate, "00:00:00", timezone) : undefined,
      end: endDate ? localDateTimeToUtcIso(endDate, "23:59:59", timezone) : undefined,
      limit: PAGE_SIZE,
      offset: off,
    })
      .then((rows) => {
        setRecordings((prev) => (reset ? rows : [...prev, ...rows]));
        setHasMore(rows.length === PAGE_SIZE);
        setOffset(off + rows.length);
      })
      .finally(() => setLoading(false));
  };

  // Refetch whenever a server-side filter changes. Time-of-day is applied
  // client-side (below), so it isn't a dependency here.
  useEffect(() => {
    setSelected(new Set());
    load(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cameraFilter, triggerFilter, startDate, endDate]);

  // Time-of-day window filter, applied to whatever is loaded. Supports an
  // overnight window (e.g. 22:00 → 06:00) by wrapping around midnight.
  const visible = useMemo(() => {
    const lo = parseHm(startTime);
    const hi = parseHm(endTime);
    if (lo === null && hi === null) return recordings;
    return recordings.filter((r) => {
      const t = minutesOfDay(r.started_at, timezone);
      const a = lo ?? 0;
      const b = hi ?? 24 * 60;
      return a <= b ? t >= a && t <= b : t >= a || t <= b;
    });
  }, [recordings, startTime, endTime, timezone]);

  const cameraName = (id: number) => cameras.find((c) => c.id === id)?.name ?? `Camera ${id}`;

  const anyFilter =
    cameraFilter || triggerFilter || startDate || endDate || startTime || endTime;

  const clearFilters = () => {
    setCameraFilter("");
    setTriggerFilter("");
    setStartDate("");
    setEndDate("");
    setStartTime("");
    setEndTime("");
  };

  // ── Selection ──
  const toggleOne = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const allSelected = visible.length > 0 && visible.every((r) => selected.has(r.id));
  const toggleAll = () =>
    setSelected(allSelected ? new Set() : new Set(visible.map((r) => r.id)));

  // ── Bulk actions ──
  const selectedIds = () => Array.from(selected);
  const selectedSize = () =>
    visible.filter((r) => selected.has(r.id)).reduce((sum, r) => sum + (r.size_bytes || 0), 0);

  const handleBulkDelete = async () => {
    const ids = selectedIds();
    if (ids.length === 0) return;
    if (!window.confirm(`Delete ${ids.length} recording${ids.length > 1 ? "s" : ""}? This cannot be undone.`))
      return;
    setBusy(true);
    try {
      await bulkDeleteRecordings(ids);
      setSelected(new Set());
      setPreview((p) => (p && ids.includes(p.id) ? null : p));
      load(true);
    } finally {
      setBusy(false);
    }
  };

  const handleBulkDownload = () => {
    const ids = selectedIds();
    if (ids.length === 0) return;
    if (ids.length === 1) triggerDownload(recordingDownloadUrl(ids[0]));
    else triggerDownload(bulkDownloadZipUrl(ids));
  };

  const handleDeleteOne = async (id: number) => {
    if (!window.confirm("Delete this recording? This cannot be undone.")) return;
    setBusy(true);
    try {
      await deleteRecording(id);
      setSelected((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
      setPlaying((p) => (p?.id === id ? null : p));
      setPreview((p) => (p?.id === id ? null : p));
      load(true);
    } finally {
      setBusy(false);
    }
  };

  const selCount = visible.filter((r) => selected.has(r.id)).length;

  return (
    <div className="page">
      <div className="page-header">
        <h1>Recording Files Management</h1>
        <div className="rec-viewtoggle">
          <button
            className={`btn btn-sm ${viewMode === "tiles" ? "btn-primary" : ""}`}
            onClick={() => setViewMode("tiles")}
            title="Tiles"
          >
            ▦ Tiles
          </button>
          <button
            className={`btn btn-sm ${viewMode === "details" ? "btn-primary" : ""}`}
            onClick={() => setViewMode("details")}
            title="Details"
          >
            ☰ Details
          </button>
          <button
            className={`btn btn-sm ${viewMode === "preview" ? "btn-primary" : ""}`}
            onClick={() => setViewMode("preview")}
            title="Preview pane"
          >
            ▧ Preview
          </button>
        </div>
      </div>

      {/* ── Filters ── */}
      <div className="rec-filters">
        <div className="rec-filter-group">
          <label>Camera</label>
          <select value={cameraFilter} onChange={(e) => setCameraFilter(e.target.value)}>
            <option value="">All cameras</option>
            {cameras.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </div>

        <div className="rec-filter-group">
          <label>Trigger</label>
          <select value={triggerFilter} onChange={(e) => setTriggerFilter(e.target.value)}>
            <option value="">All triggers</option>
            <option value="continuous">Continuous</option>
            <option value="motion">Motion</option>
          </select>
        </div>

        <div className="rec-filter-group">
          <label>Date range</label>
          <div className="rec-range">
            <input type="date" value={startDate} max={endDate || undefined} onChange={(e) => setStartDate(e.target.value)} />
            <span>→</span>
            <input type="date" value={endDate} min={startDate || undefined} onChange={(e) => setEndDate(e.target.value)} />
          </div>
        </div>

        <div className="rec-filter-group">
          <label>Time of day</label>
          <div className="rec-range">
            <input type="time" value={startTime} onChange={(e) => setStartTime(e.target.value)} />
            <span>→</span>
            <input type="time" value={endTime} onChange={(e) => setEndTime(e.target.value)} />
          </div>
        </div>

        <div className="rec-filter-group">
          <label>&nbsp;</label>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn btn-sm" onClick={() => load(true)} disabled={loading}>
              Refresh
            </button>
            {anyFilter && (
              <button className="btn btn-sm" onClick={clearFilters}>
                Clear
              </button>
            )}
          </div>
        </div>
      </div>

      {/* ── Bulk action bar ── */}
      {selCount > 0 && (
        <div className="rec-bulkbar">
          <label className="rec-selall">
            <input type="checkbox" checked={allSelected} onChange={toggleAll} />
            {selCount} selected · {formatSize(selectedSize())}
          </label>
          <div style={{ flex: 1 }} />
          <button className="btn btn-sm" onClick={handleBulkDownload} disabled={busy}>
            ⬇ Download {selCount > 1 ? "(zip)" : ""}
          </button>
          {isAdmin && (
            <button className="btn btn-sm btn-danger" onClick={handleBulkDelete} disabled={busy}>
              🗑 Delete
            </button>
          )}
          <button className="btn btn-sm" onClick={() => setSelected(new Set())}>
            Clear selection
          </button>
        </div>
      )}

      {/* ── Content ── */}
      {loading && recordings.length === 0 ? (
        <p style={{ color: "var(--text-dim)" }}>Loading...</p>
      ) : visible.length === 0 ? (
        <div className="empty-state">No recordings match these filters.</div>
      ) : viewMode === "tiles" ? (
        <TilesView
          items={visible}
          selected={selected}
          onToggle={toggleOne}
          onOpen={setPlaying}
          cameraName={cameraName}
          fmtDateTime={fmtDateTime}
        />
      ) : viewMode === "details" ? (
        <DetailsView
          items={visible}
          selected={selected}
          onToggle={toggleOne}
          allSelected={allSelected}
          onToggleAll={toggleAll}
          onOpen={setPlaying}
          onDownload={(id) => triggerDownload(recordingDownloadUrl(id))}
          onDelete={handleDeleteOne}
          isAdmin={isAdmin}
          cameraName={cameraName}
          fmtDateTime={fmtDateTime}
        />
      ) : (
        <PreviewView
          items={visible}
          selected={selected}
          onToggle={toggleOne}
          preview={preview}
          setPreview={setPreview}
          onDownload={(id) => triggerDownload(recordingDownloadUrl(id))}
          onDelete={handleDeleteOne}
          isAdmin={isAdmin}
          cameraName={cameraName}
          fmtDateTime={fmtDateTime}
        />
      )}

      {hasMore && (
        <div style={{ textAlign: "center", marginTop: 16 }}>
          <button className="btn btn-sm" onClick={() => load(false)} disabled={loading}>
            {loading ? "Loading..." : "Load more"}
          </button>
        </div>
      )}

      {/* ── Playback modal ── */}
      {playing && (
        <div className="modal-backdrop" onClick={() => setPlaying(null)}>
          <div className="modal" style={{ maxWidth: 800 }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>
                {cameraName(playing.camera_id)} — {fmtDateTime(playing.started_at)}
              </h2>
              <button className="close-btn" onClick={() => setPlaying(null)}>
                ×
              </button>
            </div>
            <video
              controls
              autoPlay
              style={{ width: "100%", borderRadius: 8, background: "#000" }}
              src={buildMediaUrl(`/recordings/${playing.id}/video`)}
            />
            <div className="modal-actions">
              <button className="btn" onClick={() => triggerDownload(recordingDownloadUrl(playing.id))}>
                ⬇ Download
              </button>
              {isAdmin && (
                <button className="btn btn-danger" onClick={() => handleDeleteOne(playing.id)}>
                  Delete recording
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ─────────────────────────── Tiles ───────────────────────────
function TilesView({
  items,
  selected,
  onToggle,
  onOpen,
  cameraName,
  fmtDateTime,
}: {
  items: Recording[];
  selected: Set<number>;
  onToggle: (id: number) => void;
  onOpen: (r: Recording) => void;
  cameraName: (id: number) => string;
  fmtDateTime: (v: string) => string;
}) {
  return (
    <div className="recording-grid">
      {items.map((rec) => (
        <div
          key={rec.id}
          className={`recording-card ${selected.has(rec.id) ? "rec-selected" : ""}`}
          onClick={() => onOpen(rec)}
        >
          <input
            type="checkbox"
            className="rec-check"
            checked={selected.has(rec.id)}
            onClick={(e) => e.stopPropagation()}
            onChange={() => onToggle(rec.id)}
          />
          {rec.thumbnail_path ? (
            <img src={buildMediaUrl(`/recordings/${rec.id}/thumbnail.jpg`)} alt="" />
          ) : (
            <div style={{ aspectRatio: "16/9", background: "#000" }} />
          )}
          <div className="recording-meta">
            <div>{cameraName(rec.camera_id)}</div>
            <div className="muted">
              {fmtDateTime(rec.started_at)} · {formatDuration(rec.duration_seconds)} · {formatSize(rec.size_bytes)}
            </div>
            <div className="muted" style={{ textTransform: "capitalize" }}>
              {rec.trigger}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ─────────────────────────── Details ───────────────────────────
function DetailsView({
  items,
  selected,
  onToggle,
  allSelected,
  onToggleAll,
  onOpen,
  onDownload,
  onDelete,
  isAdmin,
  cameraName,
  fmtDateTime,
}: {
  items: Recording[];
  selected: Set<number>;
  onToggle: (id: number) => void;
  allSelected: boolean;
  onToggleAll: () => void;
  onOpen: (r: Recording) => void;
  onDownload: (id: number) => void;
  onDelete: (id: number) => void;
  isAdmin: boolean;
  cameraName: (id: number) => string;
  fmtDateTime: (v: string) => string;
}) {
  return (
    <div className="table-wrap">
      <table className="rec-table data-table">
        <thead>
          <tr>
            <th style={{ width: 32 }}>
              <input type="checkbox" checked={allSelected} onChange={onToggleAll} />
            </th>
            <th>Camera</th>
            <th>Started</th>
            <th>Duration</th>
            <th>Size</th>
            <th>Trigger</th>
            <th style={{ textAlign: "right" }}>Actions</th>
          </tr>
        </thead>
        <tbody>
          {items.map((rec) => (
            <tr
              key={rec.id}
              className={selected.has(rec.id) ? "rec-selected" : ""}
              onClick={() => onToggle(rec.id)}
            >
              <td onClick={(e) => e.stopPropagation()}>
                <input type="checkbox" checked={selected.has(rec.id)} onChange={() => onToggle(rec.id)} />
              </td>
              <td>{cameraName(rec.camera_id)}</td>
              <td>{fmtDateTime(rec.started_at)}</td>
              <td>{formatDuration(rec.duration_seconds)}</td>
              <td>{formatSize(rec.size_bytes)}</td>
              <td style={{ textTransform: "capitalize" }}>{rec.trigger}</td>
              <td onClick={(e) => e.stopPropagation()} style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                <button className="btn btn-sm" onClick={() => onOpen(rec)} title="Play">
                  ▶
                </button>{" "}
                <button className="btn btn-sm" onClick={() => onDownload(rec.id)} title="Download">
                  ⬇
                </button>{" "}
                {isAdmin && (
                  <button className="btn btn-sm btn-danger" onClick={() => onDelete(rec.id)} title="Delete">
                    🗑
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─────────────────────────── Preview pane ───────────────────────────
function PreviewView({
  items,
  selected,
  onToggle,
  preview,
  setPreview,
  onDownload,
  onDelete,
  isAdmin,
  cameraName,
  fmtDateTime,
}: {
  items: Recording[];
  selected: Set<number>;
  onToggle: (id: number) => void;
  preview: Recording | null;
  setPreview: (r: Recording | null) => void;
  onDownload: (id: number) => void;
  onDelete: (id: number) => void;
  isAdmin: boolean;
  cameraName: (id: number) => string;
  fmtDateTime: (v: string) => string;
}) {
  // Default the preview to the first item if nothing is selected yet.
  const active = preview && items.some((r) => r.id === preview.id) ? preview : items[0] ?? null;

  return (
    <div className="rec-split">
      <div className="rec-list">
        {items.map((rec) => (
          <div
            key={rec.id}
            className={`rec-list-item ${active?.id === rec.id ? "active" : ""} ${
              selected.has(rec.id) ? "rec-selected" : ""
            }`}
            onClick={() => setPreview(rec)}
          >
            <input
              type="checkbox"
              checked={selected.has(rec.id)}
              onClick={(e) => e.stopPropagation()}
              onChange={() => onToggle(rec.id)}
            />
            <div style={{ minWidth: 0 }}>
              <div className="rec-list-title">{cameraName(rec.camera_id)}</div>
              <div className="muted rec-list-sub">
                {fmtDateTime(rec.started_at)} · {formatDuration(rec.duration_seconds)}
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="rec-preview-pane">
        {active ? (
          <>
            <video
              key={active.id}
              controls
              autoPlay
              className="rec-preview-video"
              poster={active.thumbnail_path ? buildMediaUrl(`/recordings/${active.id}/thumbnail.jpg`) : undefined}
              src={buildMediaUrl(`/recordings/${active.id}/video`)}
            />
            <div className="rec-preview-info">
              <h3>{cameraName(active.camera_id)}</h3>
              <div className="rec-kv">
                <span>Started</span>
                <strong>{fmtDateTime(active.started_at)}</strong>
              </div>
              <div className="rec-kv">
                <span>Duration</span>
                <strong>{formatDuration(active.duration_seconds)}</strong>
              </div>
              <div className="rec-kv">
                <span>Size</span>
                <strong>{formatSize(active.size_bytes)}</strong>
              </div>
              <div className="rec-kv">
                <span>Trigger</span>
                <strong style={{ textTransform: "capitalize" }}>{active.trigger}</strong>
              </div>
              <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
                <button className="btn btn-sm" onClick={() => onDownload(active.id)}>
                  ⬇ Download
                </button>
                {isAdmin && (
                  <button className="btn btn-sm btn-danger" onClick={() => onDelete(active.id)}>
                    Delete
                  </button>
                )}
              </div>
            </div>
          </>
        ) : (
          <div className="empty-state">Select a recording to preview.</div>
        )}
      </div>
    </div>
  );
}
