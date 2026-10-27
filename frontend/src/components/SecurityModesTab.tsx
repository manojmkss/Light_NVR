import { useEffect, useState } from "react";
import {
  addRulePresets,
  createRule,
  createSchedule,
  deleteRule,
  deleteSchedule,
  listRules,
  listSchedules,
  MODE_LABEL,
  previewRules,
  updateRule,
  updateSchedule,
  type AlertRule,
  type ModeSchedule,
  type RuleAction,
  type SecurityMode,
} from "../api/security";
import { CameraGroupsCard } from "./CameraGroupsCard";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MODES: SecurityMode[] = ["home", "away", "night", "disarmed"];
const KINDS: { key: string; label: string }[] = [
  { key: "motion", label: "First alert of an event" },
  { key: "motion_update", label: "Another person arrived" },
  { key: "motion_summary", label: "End-of-event summary" },
  { key: "loitering", label: "Loitering" },
  { key: "line_cross", label: "Line crossed" },
  { key: "arrival", label: "Known person arrived" },
  { key: "incident_summary", label: "Visit summary (after the AI review)" },
  { key: "tamper", label: "Camera tampered / covered" },
];
const ACTIONS: { key: RuleAction; label: string }[] = [
  { key: "notify", label: "Notify" },
  { key: "notify_high", label: "Urgent (repeat until acknowledged)" },
  { key: "silent", label: "Don't notify (still recorded)" },
];

function toggle<T>(list: T[] | null, v: T): T[] | null {
  const cur = list ?? [];
  const next = cur.includes(v) ? cur.filter((x) => x !== v) : [...cur, v];
  return next.length ? next : null;
}

function Chips<T extends string>({ all, value, label, onChange }: {
  all: T[]; value: T[] | null; label: (v: T) => string; onChange: (v: T[] | null) => void;
}) {
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
      {all.map((v) => {
        const on = (value ?? []).includes(v);
        return (
          <button key={v} type="button" className={`btn btn-sm ${on ? "btn-primary" : ""}`} onClick={() => onChange(toggle(value, v))}>
            {label(v)}
          </button>
        );
      })}
    </div>
  );
}

export function SecurityModesTab() {
  const [schedules, setSchedules] = useState<ModeSchedule[]>([]);
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [preview, setPreview] = useState<Awaited<ReturnType<typeof previewRules>> | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = () => {
    listSchedules().then(setSchedules).catch((e) => setError((e as Error).message));
    listRules().then(setRules).catch((e) => setError((e as Error).message));
  };
  useEffect(reload, []);

  const saveSchedule = async (s: ModeSchedule) => {
    const { id, ...body } = s;
    await updateSchedule(id, body);
    reload();
  };
  const saveRule = async (r: AlertRule) => {
    const { id, ...body } = r;
    try {
      await updateRule(id, body);
      reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const editRule = (r: AlertRule, patch: Partial<AlertRule>) => setRules((rs) => rs.map((x) => (x.id === r.id ? { ...x, ...patch } : x)));

  return (
    <div style={{ display: "grid", gap: 16, maxWidth: 820 }}>
      <div className="card">
        <strong>How modes work</strong>
        <p style={{ color: "var(--text-dim)", fontSize: 13, marginBottom: 0 }}>
          Home, Away, Night and Disarmed decide <em>who gets alerted about what</em>, through the rules below.
          Recording never changes. With no rules switched on, alerts work exactly as before. Switch modes from the
          button in the sidebar, from Home Assistant, or automatically with a schedule.
        </p>
      </div>

      <div className="card">
        <strong>Schedule</strong>
        <p style={{ color: "var(--text-dim)", fontSize: 13 }}>
          At each time, switch to that mode. A mode you pick by hand lasts until the next scheduled change (or use
          "Hold" to keep it).
        </p>
        {schedules.map((s) => (
          <div key={s.id} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", padding: "4px 0" }}>
            <input type="checkbox" checked={s.enabled} onChange={(e) => saveSchedule({ ...s, enabled: e.target.checked })} />
            <div style={{ display: "flex", gap: 2 }}>
              {DAYS.map((d, i) => (
                <button
                  key={d}
                  className={`btn btn-sm ${(s.days_mask >> i) & 1 ? "btn-primary" : ""}`}
                  onClick={() => {
                    const mask = s.days_mask ^ (1 << i);
                    if (mask) saveSchedule({ ...s, days_mask: mask });
                  }}
                >
                  {d}
                </button>
              ))}
            </div>
            <input type="time" value={s.at_hhmm} onChange={(e) => saveSchedule({ ...s, at_hhmm: e.target.value })} />
            <span>→</span>
            <select value={s.mode} onChange={(e) => saveSchedule({ ...s, mode: e.target.value as SecurityMode })}>
              {MODES.map((m) => <option key={m} value={m}>{MODE_LABEL[m]}</option>)}
            </select>
            <button className="btn btn-sm" onClick={() => deleteSchedule(s.id).then(reload)}>Delete</button>
          </div>
        ))}
        <button
          className="btn btn-sm"
          style={{ marginTop: 8 }}
          onClick={() => createSchedule({ days_mask: 127, at_hhmm: schedules.length ? "07:00" : "23:00", mode: schedules.length ? "home" : "night", enabled: true }).then(reload)}
        >
          + Add time
        </button>
      </div>

      <div className="card">
        <strong>Alert rules</strong>
        <p style={{ color: "var(--text-dim)", fontSize: 13 }}>
          Checked top to bottom; the first switched-on rule that matches an alert decides what happens. Empty filters
          match everything. Camera offline / errors / storage alerts are never affected.
        </p>
        <div style={{ display: "flex", gap: 8, marginBottom: 10, flexWrap: "wrap" }}>
          <button className="btn btn-sm" onClick={() => addRulePresets().then(setRules)}>
            + Add suggested rules (switched off)
          </button>
          <button
            className="btn btn-sm"
            onClick={() => createRule({ name: "New rule", position: (rules.length ? rules[rules.length - 1].position : 0) + 10, enabled: false, modes: null, camera_ids: null, kinds: null, objects: null, zones: null, identities: null, action: "notify", cooldown_s: null, webhook_url: null }).then(reload)}
          >
            + Blank rule
          </button>
          <button className="btn btn-sm" onClick={() => previewRules().then(setPreview)}>
            What would happen?
          </button>
        </div>
        {rules.map((r) => (
          <details key={r.id} style={{ borderTop: "1px solid var(--border)", padding: "8px 0" }}>
            <summary style={{ cursor: "pointer", display: "flex", gap: 8, alignItems: "center" }}>
              <input
                type="checkbox"
                checked={r.enabled}
                onClick={(e) => e.stopPropagation()}
                onChange={(e) => saveRule({ ...r, enabled: e.target.checked })}
              />
              <span style={{ flex: 1, opacity: r.enabled ? 1 : 0.6 }}>{r.name}</span>
              <span style={{ fontSize: 12, color: "var(--text-dim)" }}>
                {(r.modes ?? []).map((m) => MODE_LABEL[m]).join(", ") || "any mode"} · {ACTIONS.find((a) => a.key === r.action)?.label}
              </span>
            </summary>
            <div style={{ display: "grid", gap: 8, marginTop: 8, paddingLeft: 24 }}>
              <label>Name <input value={r.name} onChange={(e) => editRule(r, { name: e.target.value })} /></label>
              <div>When the mode is <Chips all={MODES} value={r.modes} label={(m) => MODE_LABEL[m]} onChange={(v) => editRule(r, { modes: v })} /></div>
              <div>For <Chips all={KINDS.map((k) => k.key)} value={r.kinds} label={(k) => KINDS.find((x) => x.key === k)?.label ?? k} onChange={(v) => editRule(r, { kinds: v })} /></div>
              <div>Involving <Chips all={["person", "car", "truck", "motorcycle", "bicycle", "dog", "cat"]} value={r.objects} label={(o) => o} onChange={(v) => editRule(r, { objects: v })} /></div>
              <div>
                Who (needs face recognition){" "}
                <Chips
                  all={["known", "unknown", "household", "familiar", "stranger", "watchlist"]}
                  value={r.identities}
                  label={(w) => ({ known: "Anyone trusted (family/familiar)", unknown: "Unknown face or stranger", household: "Family", familiar: "Familiar", staff: "Familiar", visitor: "Familiar", stranger: "Stranger", watchlist: "Suspicious" } as Record<string, string>)[w] ?? w}
                  onChange={(v) => editRule(r, { identities: v })}
                />
              </div>
              <label>
                In named zones (comma separated, empty = anywhere){" "}
                <input value={(r.zones ?? []).join(", ")} onChange={(e) => editRule(r, { zones: e.target.value ? e.target.value.split(",").map((z) => z.trim()).filter(Boolean) : null })} />
              </label>
              <label>
                Then{" "}
                <select value={r.action} onChange={(e) => editRule(r, { action: e.target.value as RuleAction })}>
                  {ACTIONS.map((a) => <option key={a.key} value={a.key}>{a.label}</option>)}
                </select>
              </label>
              <label>
                Also call this webhook (e.g. a Home Assistant siren){" "}
                <input placeholder="https://…" value={r.webhook_url ?? ""} onChange={(e) => editRule(r, { webhook_url: e.target.value || null })} />
              </label>
              <div style={{ display: "flex", gap: 8 }}>
                <button className="btn btn-sm btn-primary" onClick={() => saveRule(r)}>Save rule</button>
                <button className="btn btn-sm" onClick={() => deleteRule(r.id).then(reload)}>Delete</button>
              </div>
            </div>
          </details>
        ))}
        {error && <div className="error-text" style={{ marginTop: 8 }}>{error}</div>}
      </div>

      <CameraGroupsCard />

      {preview && (
        <div className="card">
          <strong>In {MODE_LABEL[preview.mode as SecurityMode] ?? preview.mode} mode, the last {preview.results.length} events would…</strong>
          {preview.results.map((p) => (
            <div key={p.event_id} style={{ display: "flex", gap: 8, fontSize: 13, padding: "2px 0" }}>
              <span style={{ minWidth: 110, color: p.action === "silent" ? "var(--text-dim)" : p.action === "notify_high" ? "var(--danger, #f85149)" : "inherit" }}>
                {ACTIONS.find((a) => a.key === p.action)?.label.split(" (")[0] ?? p.action}
              </span>
              <span style={{ flex: 1 }}>{p.message}</span>
              <span style={{ color: "var(--text-dim)" }}>{p.rule ?? "no rule (default)"}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
