import { useEffect, useState } from "react";
import { getMode, MODE_LABEL, setMode, type ModeInfo, type SecurityMode } from "../api/security";
import { useAuth } from "../context/AuthContext";

const MODE_COLOR: Record<SecurityMode, string> = {
  home: "#3fb950",
  away: "#f85149",
  night: "#a371f7",
  disarmed: "var(--text-dim)",
};

/** Header control: shows the arm mode in force; admins can change it (until
 *  the next scheduled change, or "Hold" to keep it). */
export function ModeSwitcher() {
  const { isAdmin } = useAuth();
  const [info, setInfo] = useState<ModeInfo | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = () => getMode().then((m) => alive && setInfo(m)).catch(() => undefined);
    load();
    const t = setInterval(load, 30000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  if (!info) return null;

  const choose = async (mode: SecurityMode, hold: boolean) => {
    setBusy(true);
    try {
      setInfo(await setMode(mode, hold));
      setOpen(false);
    } finally {
      setBusy(false);
    }
  };

  const until = info.next_change_at && !info.hold ? new Date(info.next_change_at) : null;

  return (
    <div style={{ position: "relative" }}>
      <button
        className="btn btn-sm"
        onClick={() => isAdmin && setOpen((o) => !o)}
        title={info.source === "schedule" ? "Set by schedule" : `Set by ${info.set_by ?? info.source}`}
        style={{ borderColor: MODE_COLOR[info.mode], color: MODE_COLOR[info.mode] }}
      >
        ● {MODE_LABEL[info.mode]}
      </button>
      {open && (
        <div
          className="card"
          style={{ position: "absolute", right: 0, top: "110%", zIndex: 50, minWidth: 230, padding: 10 }}
        >
          <div style={{ fontSize: 12, color: "var(--text-dim)", marginBottom: 6 }}>
            {info.source === "schedule" ? "Following the schedule" : "Set by hand"}
            {until ? ` · next change ${until.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}` : ""}
          </div>
          {(["home", "away", "night", "disarmed"] as SecurityMode[]).map((m) => (
            <div key={m} style={{ display: "flex", gap: 6, marginBottom: 4 }}>
              <button className="btn btn-sm" style={{ flex: 1, textAlign: "left" }} disabled={busy} onClick={() => choose(m, false)}>
                <span style={{ color: MODE_COLOR[m] }}>●</span> {MODE_LABEL[m]}
              </button>
              <button className="btn btn-sm" disabled={busy} title="Keep this mode until I change it (ignore the schedule)" onClick={() => choose(m, true)}>
                Hold
              </button>
            </div>
          ))}
          <div style={{ fontSize: 11, color: "var(--text-dim)", marginTop: 4 }}>
            Modes change who gets alerted, never what is recorded. They take effect through the alert rules in
            Settings → Modes.
          </div>
        </div>
      )}
    </div>
  );
}
