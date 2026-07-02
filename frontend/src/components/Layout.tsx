import { useEffect, useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { getSystemStatus } from "../api/system";
import type { SystemStatus } from "../api/types";
import { useAuth } from "../context/AuthContext";
import { usePolling } from "../hooks/usePolling";
import { formatUptime } from "./SystemStatsCard";

const APP_VERSION = "1.0.0";

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", icon: "▦" },
  { to: "/live", label: "Live View", icon: "▶" },
  { to: "/playback", label: "Playback & Export", icon: "⏱" },
  { to: "/cameras", label: "Cameras", icon: "▣" },
  { to: "/recordings", label: "Recording Files", icon: "▤" },
  { to: "/settings", label: "Settings", icon: "⚙" },
];

const TITLES: Record<string, string> = {
  "/": "Dashboard",
  "/live": "Live View",
  "/playback": "Playback & Export",
  "/cameras": "Cameras",
  "/recordings": "Recording Files Management",
  "/settings": "Settings",
};

function quickHealth(status: SystemStatus | null): number {
  if (!status) return 100;
  let s = 100;
  if (status.cameras_total > 0) s -= (status.cameras_offline / status.cameras_total) * 40;
  const pct = status.storage_total_bytes ? (status.storage_used_bytes / status.storage_total_bytes) * 100 : 0;
  if (pct >= 90) s -= 20;
  else if (pct >= 80) s -= 10;
  return Math.max(0, Math.round(s));
}
const healthLabel = (s: number) => (s >= 90 ? "Excellent" : s >= 70 ? "Good" : s >= 50 ? "Fair" : "Poor");

function Sparkline({ values }: { values: number[] }) {
  const w = 190;
  const h = 34;
  if (values.length < 2) return <div className="sh-spark" style={{ height: h }} />;
  const step = w / (values.length - 1);
  const pts = values.map((v, i) => `${(i * step).toFixed(1)},${(h - (Math.min(100, v) / 100) * h).toFixed(1)}`).join(" ");
  return (
    <svg className="sh-spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
      <polyline points={pts} fill="none" stroke="var(--success)" strokeWidth="1.5" />
    </svg>
  );
}

export function Layout({ children }: { children: ReactNode }) {
  const { user, logout } = useAuth();
  const location = useLocation();
  const { data: status } = usePolling(getSystemStatus, 5000);
  const [cpuHistory, setCpuHistory] = useState<number[]>([]);

  useEffect(() => {
    if (status) setCpuHistory((h) => [...h.slice(-19), status.cpu_percent]);
  }, [status]);

  const health = quickHealth(status ?? null);
  const title = TITLES[location.pathname] ?? "LightNVR";

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-brand">LightNVR</div>
        <nav className="sidebar-nav">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) => `sidebar-link${isActive ? " active" : ""}`}
            >
              <span>{item.icon}</span>
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-health">
          <div className="sh-title">System Health</div>
          <div className="sh-score">{health}%</div>
          <div className="sh-label">{healthLabel(health)}</div>
          <Sparkline values={cpuHistory} />
          <div className="sh-uptime">
            Uptime
            <strong>{status ? formatUptime(status.uptime_seconds) : "—"}</strong>
          </div>
        </div>

        <div className="sidebar-footer">
          <div>{user?.username}</div>
          <div style={{ marginBottom: 10, textTransform: "capitalize" }}>{user?.role}</div>
          <button className="btn btn-sm" onClick={logout} style={{ width: "100%" }}>
            Sign out
          </button>
          <div className="sidebar-version">v{APP_VERSION}</div>
        </div>
      </aside>

      <div className="main-content">
        <div className="topbar">
          <strong>{title}</strong>
          <button className="btn btn-sm" onClick={logout}>
            Sign out
          </button>
        </div>
        {children}
      </div>

      <nav className="bottom-nav">
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === "/"}
            className={({ isActive }) => (isActive ? "active" : "")}
          >
            <span>{item.icon}</span>
            {item.label}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
