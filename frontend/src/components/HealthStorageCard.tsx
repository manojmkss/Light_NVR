import { useEffect, useState } from "react";
import { cleanUpNow, getHealthStorage, type HealthStorage } from "../api/system";
import { useSettings } from "../context/SettingsContext";
import { formatBytes } from "./SystemStatsCard";

const ROLE_NAMES: Record<string, string> = { system: "System", docker: "Docker", nvr: "NVR" };

function roleLabel(roles: string[]): string {
  const names = roles.map((r) => (r.startsWith("recordings") ? "Recordings" : ROLE_NAMES[r] ?? r));
  return [...new Set(names)].join(" + ");
}

function barColour(percent: number): string {
  if (percent >= 90) return "var(--danger)";
  if (percent >= 80) return "#d29922";
  return "var(--success)";
}

/** Settings -> System: what's using the disk, the NVR's last clean-up, and
 *  what the host guardian (scripts/guardian, a service outside Docker) has
 *  been checking and recovering. */
export function HealthStorageCard() {
  const { fmtDateTime } = useSettings();
  const [data, setData] = useState<HealthStorage | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [cleaning, setCleaning] = useState(false);
  const [cleaned, setCleaned] = useState<string | null>(null);

  const load = () =>
    getHealthStorage()
      .then((r) => {
        setData(r);
        setErr(null);
      })
      .catch((e) => setErr((e as Error).message));

  useEffect(() => {
    load();
    const t = window.setInterval(load, 60000);
    return () => window.clearInterval(t);
  }, []);

  const clean = async () => {
    setCleaning(true);
    setCleaned(null);
    setErr(null);
    try {
      const r = await cleanUpNow();
      const parts = [`freed ${formatBytes(r.freed_bytes ?? 0)}`];
      if (r.pictures?.files) parts.push(`${r.pictures.files} old alert pictures`);
      if (r.reconcile?.dropped) parts.push(`${r.reconcile.dropped} recordings whose file had gone`);
      if (r.reconcile?.registered) parts.push(`${r.reconcile.registered} recordings found on disk added back`);
      if (r.recordings_removed_for_space) parts.push(`${r.recordings_removed_for_space} oldest recordings for space`);
      setCleaned(`Done - ${parts.join(", ")}.`);
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setCleaning(false);
    }
  };

  const at = (epoch: number | null | undefined) => (epoch ? fmtDateTime(new Date(epoch * 1000).toISOString()) : "—");
  const d = data?.disk ?? {};
  const g = data?.guardian ?? null;
  const last = data?.housekeeping.last;
  const usedPct = d.total ? (100 * (d.used ?? 0)) / d.total : 0;
  const other = Math.max(0, (d.used ?? 0) - (d.recordings ?? 0) - (d.pictures ?? 0) - (d.logs ?? 0));

  let guardianLine: { text: string; colour: string };
  if (!g) {
    guardianLine = {
      text: "Not installed on this machine (it comes with the Linux install/update scripts).",
      colour: "var(--text-dim)",
    };
  } else if (!g.fresh) {
    guardianLine = { text: `Not running - last check ${at(g.at)}.`, colour: "#d29922" };
  } else if (g.paused) {
    guardianLine = { text: "Paused while an update runs.", colour: "var(--text-dim)" };
  } else if (g.problems.length) {
    guardianLine = { text: `Watching - ${g.problems.join("; ")}.`, colour: "#d29922" };
  } else {
    guardianLine = { text: "Running - everything looks healthy.", colour: "var(--success)" };
  }

  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>Health & storage</h3>
      <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 0 }}>
        The NVR cleans up after itself every 6 hours: old alert pictures, logs and leftovers, and recordings whose
        file has gone. The guardian, a small service on the host, restarts anything that hangs, cleans system logs
        and unused Docker images daily, and tells you on Telegram if the NVR itself is down. It never restarts the
        machine.
      </p>

      {err && <div className="error-text">{err}</div>}

      {d.total ? (
        <div style={{ marginBottom: 16 }}>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, marginBottom: 4 }}>
            <span>Recordings disk</span>
            <span style={{ color: "var(--text-dim)" }}>
              {formatBytes(d.free ?? 0)} free of {formatBytes(d.total)}
            </span>
          </div>
          <div className="progress-bar">
            <div className="progress-bar-fill" style={{ width: `${Math.min(100, usedPct)}%`, background: barColour(usedPct) }} />
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 16px", fontSize: 12, color: "var(--text-dim)", marginTop: 6 }}>
            <span>Recordings {formatBytes(d.recordings ?? 0)}</span>
            <span>Alert pictures {formatBytes(d.pictures ?? 0)}</span>
            <span>Logs {formatBytes(d.logs ?? 0)}</span>
            <span>Everything else {formatBytes(other)}</span>
          </div>
        </div>
      ) : null}

      {g?.fresh && g.disks.length > 0 && (
        <div style={{ marginBottom: 16, fontSize: 13 }}>
          {g.disks.map((disk) => (
            <div key={disk.path} style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 4 }}>
              <span style={{ minWidth: 150 }}>{roleLabel(disk.roles)}</span>
              <div className="progress-bar" style={{ flex: 1, margin: 0 }}>
                <div className="progress-bar-fill" style={{ width: `${Math.min(100, disk.percent)}%`, background: barColour(disk.percent) }} />
              </div>
              <span style={{ color: "var(--text-dim)", minWidth: 90, textAlign: "right" }}>
                {disk.percent.toFixed(0)}% · {formatBytes(disk.free)} free
              </span>
            </div>
          ))}
        </div>
      )}

      <div style={{ fontSize: 13, display: "grid", gap: 6, marginBottom: 12 }}>
        <div>
          Guardian: <span style={{ color: guardianLine.colour }}>{guardianLine.text}</span>
        </div>
        <div style={{ color: "var(--text-dim)" }}>
          Last clean-up by the NVR: {data?.housekeeping.last_run ? fmtDateTime(data.housekeeping.last_run) : "not yet"}
          {last?.freed_bytes ? ` - freed ${formatBytes(last.freed_bytes)}` : ""}
        </div>
        {g?.last_clean && (
          <div style={{ color: "var(--text-dim)" }}>
            Last host clean-up: {at(g.last_clean.at)} ({g.last_clean.reason}) - freed {formatBytes(g.last_clean.freed_bytes)}
          </div>
        )}
      </div>

      {g && g.recoveries.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>Recent recoveries</div>
          {[...g.recoveries].reverse().map((r) => (
            <div key={`${r.at}-${r.what}`} style={{ display: "flex", gap: 8, fontSize: 12, marginBottom: 3 }}>
              <span>{r.ok ? "✅" : "⚠️"}</span>
              <span style={{ color: "var(--text-dim)", minWidth: 130 }}>{at(r.at)}</span>
              <span>
                {r.what} <span style={{ color: "var(--text-dim)" }}>- {r.why}</span>
              </span>
            </div>
          ))}
        </div>
      )}

      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <button className="btn btn-sm" disabled={cleaning} onClick={clean}>
          {cleaning ? "Cleaning up…" : "Clean up now"}
        </button>
        {cleaned && <span style={{ fontSize: 12, color: "var(--text-dim)" }}>{cleaned}</span>}
      </div>
    </div>
  );
}
