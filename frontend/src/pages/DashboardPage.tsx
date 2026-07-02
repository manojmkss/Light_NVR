import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { listCameras, snapshotUrl, updateCamera } from "../api/cameras";
import { getStorageConfig, getStorageHealth } from "../api/storage";
import { getDashboard, getSystemStatus, listEvents } from "../api/system";
import type { Camera, NvrEvent, StorageHealth, SystemStatus } from "../api/types";
import { formatBytes } from "../components/SystemStatsCard";
import { useSettings } from "../context/SettingsContext";
import { usePolling } from "../hooks/usePolling";

// ─────────────────────────────── icons ───────────────────────────────
const svg = (children: ReactNode, size = 20) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
    {children}
  </svg>
);
const IconCamera = () => svg(<><path d="M3 7h11l3-3h2v14h-2l-3-3H3z" /><circle cx="9" cy="11" r="2.5" /></>);
const IconVideo = () => svg(<><rect x="2" y="6" width="13" height="12" rx="2" /><path d="M15 10l6-3v10l-6-3z" /></>);
const IconDisk = () => svg(<><circle cx="12" cy="12" r="9" /><circle cx="12" cy="12" r="2.5" /></>);
const IconCalendar = () => svg(<><rect x="3" y="4" width="18" height="18" rx="2" /><path d="M3 9h18M8 2v4M16 2v4" /></>);
const IconShield = () => svg(<><path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z" /><path d="M9 12l2 2 4-4" /></>);
const IconPerson = () => svg(<><circle cx="12" cy="8" r="4" /><path d="M4 21c0-4 4-6 8-6s8 2 8 6" /></>);
const IconCar = () => svg(<><path d="M3 13l2-5h14l2 5v5h-3v-2H6v2H3z" /><circle cx="7.5" cy="15.5" r="1.3" /><circle cx="16.5" cy="15.5" r="1.3" /></>);
const IconRun = () => svg(<><circle cx="13" cy="5" r="2" /><path d="M6 21l3-5 4-2-2-4 5 2 2 4M9 16l-1-4 4-3" /></>);
const IconCameraOff = () => svg(<><path d="M3 7h4l3-3h2M21 7v11l-3-3H8" /><path d="M2 2l20 20" /></>);
const IconAlert = () => svg(<><path d="M12 3l9 16H3z" /><path d="M12 10v4M12 17.5v.01" /></>);
const IconInfo = () => svg(<><circle cx="12" cy="12" r="9" /><path d="M12 11v5M12 8v.01" /></>);
const IconPlay = () => svg(<><circle cx="12" cy="12" r="9" /><path d="M10 9l5 3-5 3z" /></>);
const IconSearch = () => svg(<><circle cx="11" cy="11" r="7" /><path d="M21 21l-4-4" /></>);
const IconExport = () => svg(<><path d="M12 3v12M8 7l4-4 4 4" /><path d="M4 15v4h16v-4" /></>);
const IconPlus = () => svg(<><circle cx="12" cy="12" r="9" /><path d="M12 8v8M8 12h8" /></>);
const IconBackup = () => svg(<><rect x="3" y="4" width="18" height="7" rx="1.5" /><rect x="3" y="13" width="18" height="7" rx="1.5" /><path d="M7 7.5v.01M7 16.5v.01" /></>);
const IconStar = ({ filled }: { filled?: boolean }) =>
  svg(<path d="M12 3l2.7 5.5 6 .9-4.35 4.2 1 6L12 17.8 6.65 19.6l1-6L3.3 9.4l6-.9z" fill={filled ? "currentColor" : "none"} />, 16);

// ───────────────────────────── helpers ─────────────────────────────
function withBust(url: string, tick: number): string {
  return `${url}${url.includes("?") ? "&" : "?"}_=${tick}`;
}

function healthScore(status: SystemStatus | null, health: StorageHealth | null): number {
  if (!status) return 100;
  let score = 100;
  if (status.cameras_total > 0) score -= (status.cameras_offline / status.cameras_total) * 40;
  const pct = status.storage_total_bytes ? (status.storage_used_bytes / status.storage_total_bytes) * 100 : 0;
  if (pct >= 90) score -= 20;
  else if (pct >= 80) score -= 10;
  if (health) {
    if (!health.primary.available) score -= 15;
    if (health.backup && !health.backup.available) score -= 5;
  }
  return Math.max(0, Math.round(score));
}
const healthLabel = (s: number) => (s >= 90 ? "Excellent" : s >= 70 ? "Good" : s >= 50 ? "Fair" : "Poor");

function dayLabel(iso: string, fmtDate: (v: string) => string): string {
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = (startOf(new Date()) - startOf(new Date(iso))) / 86_400_000;
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return fmtDate(iso);
}

const EVENT_META: Record<string, { icon: ReactNode; cls: string }> = {
  motion: { icon: <IconRun />, cls: "ev-motion" },
  camera_offline: { icon: <IconCameraOff />, cls: "ev-offline" },
  camera_online: { icon: <IconCamera />, cls: "ev-online" },
  low_storage: { icon: <IconDisk />, cls: "ev-warn" },
  camera_error: { icon: <IconAlert />, cls: "ev-error" },
  system: { icon: <IconInfo />, cls: "ev-info" },
};

// ───────────────────────────── donut ─────────────────────────────
function Donut({ percent, size = 118, stroke = 12, children }: { percent: number; size?: number; stroke?: number; children?: ReactNode }) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const pct = Math.min(100, Math.max(0, percent));
  const dash = (pct / 100) * c;
  const half = size / 2;
  return (
    <div className="donut" style={{ width: size, height: size }}>
      <svg width={size} height={size}>
        <circle cx={half} cy={half} r={r} fill="none" stroke="var(--border)" strokeWidth={stroke} />
        <circle
          cx={half}
          cy={half}
          r={r}
          fill="none"
          stroke="var(--accent)"
          strokeWidth={stroke}
          strokeDasharray={`${dash} ${c - dash}`}
          strokeLinecap="round"
          transform={`rotate(-90 ${half} ${half})`}
        />
      </svg>
      <div className="donut-center">{children}</div>
    </div>
  );
}

// ─────────────────────── favorite camera tile ───────────────────────
function FavoriteTile({
  cam,
  onLive,
  onPlayback,
  onUnfavorite,
}: {
  cam: Camera;
  onLive: () => void;
  onPlayback: () => void;
  onUnfavorite: () => void;
}) {
  const [tick, setTick] = useState(0);
  const [errored, setErrored] = useState(false);
  useEffect(() => {
    // Refresh the still image slowly (30s) - this is a snapshot, never a stream.
    const t = setInterval(() => setTick((n) => n + 1), 30_000);
    return () => clearInterval(t);
  }, []);
  const online = cam.status === "online";
  const showOffline = errored || cam.status === "offline";

  return (
    <div className="fav-tile">
      <div className="fav-thumb">
        {showOffline ? (
          <div className="fav-offline">
            <IconCameraOff />
            <span>{cam.name}: offline</span>
          </div>
        ) : (
          <img src={withBust(snapshotUrl(cam.id), tick)} alt={cam.name} onError={() => setErrored(true)} onLoad={() => setErrored(false)} />
        )}
        <span className={`fav-badge ${online ? "on" : "off"}`}>● {online ? "Live" : "Offline"}</span>
        <span className="fav-title">{cam.name}</span>
        <button className="fav-star" title="Remove from favorites" onClick={onUnfavorite}>
          <IconStar filled />
        </button>
      </div>
      <div className="fav-actions">
        <button title="Live view" onClick={onLive}>
          <IconVideo />
        </button>
        <button title="Playback" onClick={onPlayback}>
          <IconPlay />
        </button>
        <button title="More" onClick={onPlayback}>
          ⋯
        </button>
      </div>
    </div>
  );
}

// ─────────────────────────── heatmap ───────────────────────────
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const HEAT_STOPS = ["#1f2f4d", "#1d6a96", "#17a2a2", "#c9a227", "#e0662b"];
function heatColor(count: number, max: number): string {
  if (count <= 0) return "rgba(255,255,255,0.045)";
  const t = max > 0 ? count / max : 0;
  return HEAT_STOPS[Math.min(HEAT_STOPS.length - 1, Math.floor(t * (HEAT_STOPS.length - 1) + 0.5))];
}

function Heatmap({ grid }: { grid: number[][] }) {
  const max = useMemo(() => grid.reduce((m, row) => Math.max(m, ...row), 0), [grid]);
  return (
    <div className="heatmap">
      {grid.map((row, d) => (
        <div className="heat-row" key={d}>
          <span className="heat-day">{DAYS[d]}</span>
          <div className="heat-cells">
            {row.map((count, h) => (
              <span
                key={h}
                className="heat-cell"
                style={{ background: heatColor(count, max) }}
                title={`${DAYS[d]} ${h}:00 — ${count} event${count === 1 ? "" : "s"}`}
              />
            ))}
          </div>
        </div>
      ))}
      <div className="heat-axis">
        <span>12 AM</span>
        <span>6 AM</span>
        <span>12 PM</span>
        <span>6 PM</span>
      </div>
      <div className="heat-legend">
        <span>Low</span>
        {HEAT_STOPS.map((c) => (
          <span key={c} className="heat-sw" style={{ background: c }} />
        ))}
        <span>High</span>
      </div>
    </div>
  );
}

// ─────────────────────────── page ───────────────────────────
export function DashboardPage() {
  const { fmtTime, fmtDate } = useSettings();
  const navigate = useNavigate();
  const { data: status } = usePolling(getSystemStatus, 5000);
  const { data: cameras } = usePolling(listCameras, 10000);
  const { data: events } = usePolling(() => listEvents(undefined, 8), 10000);
  const { data: storageHealth } = usePolling(getStorageHealth, 15000);
  const { data: storageConfig } = usePolling(getStorageConfig, 60000);
  const { data: dash } = usePolling(getDashboard, 30000);

  const camList = cameras ?? [];
  const enabledCams = camList.filter((c) => c.enabled);
  const recordingCams = enabledCams.filter((c) => c.recording_mode !== "off");
  const favorites = camList.filter((c) => c.is_favorite);
  const favToShow = (favorites.length ? favorites : enabledCams).slice(0, 4);

  const storagePercent = status && status.storage_total_bytes ? (status.storage_used_bytes / status.storage_total_bytes) * 100 : 0;
  const health = healthScore(status ?? null, storageHealth ?? null);
  const retentionDays = storageConfig?.default_retention_days;
  const remainingDays =
    dash?.storage_days_to_full != null && retentionDays != null ? Math.min(retentionDays, Math.round(dash.storage_days_to_full)) : retentionDays;

  const recordingValue = camList.length === 0 ? "—" : recordingCams.length === enabledCams.length && enabledCams.length > 0 ? "Active" : recordingCams.length > 0 ? "Partial" : "Off";
  const recordingSub =
    camList.length === 0 ? "No cameras" : recordingCams.length === enabledCams.length && enabledCams.length > 0 ? "All cameras recording" : `${recordingCams.length}/${enabledCams.length} recording`;

  const camName = (id: number | null) => camList.find((c) => c.id === id)?.name ?? "System";

  // Derived alerts bar
  const alerts: string[] = [];
  for (const c of camList.filter((c) => c.status === "offline").slice(0, 2)) alerts.push(`${c.name} camera is offline`);
  if (storagePercent >= 85) alerts.push(`Storage usage is above ${Math.round(storagePercent)}%`);
  if (dash && dash.recording_failures_today > 0) alerts.push(`${dash.recording_failures_today} recording failure(s) today`);

  const toggleFav = async (cam: Camera) => {
    await updateCamera(cam.id, { is_favorite: false });
  };

  return (
    <div className="page dashboard">
      {/* ── Top stat row ── */}
      <div className="dash-stat-row">
        <div className="dash-stat">
          <div className="ds-icon ds-blue"><IconCamera /></div>
          <div className="ds-body">
            <div className="ds-label">Cameras</div>
            <div className="ds-value">{status ? `${status.cameras_online} / ${status.cameras_total}` : "—"}</div>
            <div className="ds-sub ds-ok">Online</div>
          </div>
        </div>

        <div className="dash-stat">
          <div className="ds-icon ds-green"><IconVideo /></div>
          <div className="ds-body">
            <div className="ds-label">Recording</div>
            <div className={`ds-value ${recordingValue === "Active" ? "ds-ok" : ""}`}>{recordingValue}</div>
            <div className="ds-sub">{recordingSub}</div>
          </div>
        </div>

        <div className="dash-stat">
          <Donut percent={storagePercent} size={58} stroke={7}>
            <span className="donut-mini">{Math.round(storagePercent)}%</span>
          </Donut>
          <div className="ds-body">
            <div className="ds-label">Storage Used</div>
            <div className="ds-value">{Math.round(storagePercent)}%</div>
            <div className="ds-sub">{status ? `${formatBytes(status.storage_used_bytes)} / ${formatBytes(status.storage_total_bytes)}` : "—"}</div>
          </div>
        </div>

        <div className="dash-stat">
          <div className="ds-icon ds-amber"><IconCalendar /></div>
          <div className="ds-body">
            <div className="ds-label">Retention</div>
            <div className="ds-value">{retentionDays != null ? `${retentionDays} Days` : "—"}</div>
            <div className="ds-sub">{remainingDays != null ? `~${remainingDays} days remaining` : "Estimated remaining"}</div>
          </div>
        </div>

        <div className="dash-stat">
          <div className="ds-icon ds-teal"><IconShield /></div>
          <div className="ds-body">
            <div className="ds-label">System Health</div>
            <div className="ds-value ds-ok">{health}%</div>
            <div className="ds-sub">{healthLabel(health)}</div>
          </div>
        </div>
      </div>

      {/* ── Smart summary ── */}
      <section className="dash-panel">
        <header className="panel-head">
          <h3>Smart Summary — Today</h3>
        </header>
        <div className="summary-grid">
          <div className="sum-box">
            <span className="sum-icon ds-blue"><IconPerson /></span>
            <div className="sum-num">{dash?.person_detections_today ?? 0}</div>
            <div className="sum-label">Person Detections</div>
            <div className="sum-tag">AI not enabled</div>
          </div>
          <div className="sum-box">
            <span className="sum-icon ds-purple"><IconCar /></span>
            <div className="sum-num">{dash?.vehicle_detections_today ?? 0}</div>
            <div className="sum-label">Vehicle Detections</div>
            <div className="sum-tag">AI not enabled</div>
          </div>
          <div className="sum-box">
            <span className="sum-icon ds-amber"><IconRun /></span>
            <div className="sum-num">{dash?.motion_events_today ?? "—"}</div>
            <div className="sum-label">Motion Events</div>
          </div>
          <div className="sum-box">
            <span className={`sum-icon ${dash && dash.recording_failures_today > 0 ? "ds-red" : "ds-green"}`}><IconShield /></span>
            <div className="sum-num">{dash?.recording_failures_today ?? "—"}</div>
            <div className="sum-label">Recording Failures</div>
          </div>
          <div className="sum-box">
            <span className={`sum-icon ${dash && dash.cameras_offline > 0 ? "ds-red" : "ds-green"}`}><IconCameraOff /></span>
            <div className="sum-num">{dash?.cameras_offline ?? "—"}</div>
            <div className="sum-label">Camera Offline</div>
          </div>
        </div>
      </section>

      {/* ── Bottom row ── */}
      <div className="dash-bottom">
        <section className="dash-panel">
          <header className="panel-head"><h3>Storage Overview</h3></header>
          <div className="storage-overview">
            <Donut percent={storagePercent}>
              <span className="donut-big">{Math.round(storagePercent)}%</span>
            </Donut>
            <div className="storage-rows">
              <div><span>Total Storage</span><strong>{status ? formatBytes(status.storage_total_bytes) : "—"}</strong></div>
              <div><span>Used Storage</span><strong>{status ? formatBytes(status.storage_used_bytes) : "—"}</strong></div>
              <div><span>Free Storage</span><strong>{status ? formatBytes(status.storage_free_bytes) : "—"}</strong></div>
              <div className="storage-full"><span>Estimated Full Date</span><strong>{dash?.storage_full_date ?? "—"}</strong></div>
            </div>
          </div>
        </section>

        <section className="dash-panel">
          <header className="panel-head"><h3>Event Heatmap <span className="muted">(This Week)</span></h3></header>
          {dash ? <Heatmap grid={dash.heatmap} /> : <div className="panel-empty">Loading…</div>}
        </section>

        <section className="dash-panel">
          <header className="panel-head">
            <h3>Camera Health</h3>
            <Link to="/cameras">View All ›</Link>
          </header>
          <ul className="health-list">
            {camList.slice(0, 6).map((c) => {
              const label = c.status === "offline" ? "Offline" : c.status === "online" ? "Excellent" : "Idle";
              const cls = c.status === "offline" ? "hl-off" : c.status === "online" ? "hl-ok" : "hl-idle";
              return (
                <li key={c.id}>
                  <span className="hl-name"><IconCamera /> {c.name}</span>
                  <span className={`hl-status ${cls}`}>{label} ●</span>
                </li>
              );
            })}
            {camList.length === 0 && <li className="panel-empty">No cameras.</li>}
          </ul>
        </section>

        <section className="dash-panel">
          <header className="panel-head"><h3>Quick Actions</h3></header>
          <div className="quick-grid">
            <button onClick={() => navigate("/live")}><span className="qa-icon ds-blue"><IconVideo /></span>Live View</button>
            <button onClick={() => navigate("/playback")}><span className="qa-icon ds-green"><IconPlay /></span>Playback</button>
            <button onClick={() => navigate("/recordings")}><span className="qa-icon ds-teal"><IconSearch /></span>Search</button>
            <button onClick={() => navigate("/playback")}><span className="qa-icon ds-amber"><IconExport /></span>Export Video</button>
            <button onClick={() => navigate("/cameras")}><span className="qa-icon ds-green"><IconPlus /></span>Add Camera</button>
            <button onClick={() => navigate("/settings")}><span className="qa-icon ds-blue"><IconBackup /></span>System Backup</button>
          </div>
        </section>
      </div>

      {/* ── Favorites + Recent events ── */}
      <div className="dash-mid">
        <section className="dash-panel">
          <header className="panel-head">
            <h3>Favorite Cameras</h3>
            <Link to="/cameras">View All Cameras ›</Link>
          </header>
          {favToShow.length === 0 ? (
            <div className="panel-empty">No cameras yet. Add one from the Cameras page.</div>
          ) : (
            <div className="fav-grid">
              {favToShow.map((cam) => (
                <FavoriteTile
                  key={cam.id}
                  cam={cam}
                  onLive={() => navigate("/live")}
                  onPlayback={() => navigate("/playback")}
                  onUnfavorite={() => toggleFav(cam)}
                />
              ))}
            </div>
          )}
        </section>

        <section className="dash-panel">
          <header className="panel-head">
            <h3>Recent Events</h3>
            <Link to="/recordings">View All ›</Link>
          </header>
          <ul className="event-list">
            {(events ?? []).map((ev: NvrEvent) => {
              const meta = EVENT_META[ev.type] ?? EVENT_META.system;
              return (
                <li key={ev.id}>
                  <span className={`ev-icon ${meta.cls}`}>{meta.icon}</span>
                  <div className="ev-body">
                    <div className="ev-msg">{ev.message}</div>
                    <div className="ev-loc">{camName(ev.camera_id)}</div>
                  </div>
                  <div className="ev-time">
                    <div>{fmtTime(ev.created_at)}</div>
                    <div className="ev-day">{dayLabel(ev.created_at, fmtDate)}</div>
                  </div>
                </li>
              );
            })}
            {events && events.length === 0 && <li className="panel-empty">No events yet.</li>}
          </ul>
        </section>
      </div>

      {/* ── Alerts bar ── */}
      <div className={`dash-alerts ${alerts.length ? "has-alerts" : ""}`}>
        <span className="da-title">
          <IconAlert /> {alerts.length ? `Important Alerts` : "All systems normal"}
          {alerts.length > 0 && <span className="da-count">{alerts.length}</span>}
        </span>
        <span className="da-msgs">{alerts.length ? alerts.join("  •  ") : "No active alerts."}</span>
        <Link to="/recordings">View All Alerts ›</Link>
      </div>
    </div>
  );
}
