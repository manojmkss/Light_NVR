import { useEffect, useRef, useState } from "react";
import {
  benchmarkAIEngine,
  downloadDetectionModel,
  getAIEngine,
  getAISettings,
  getDetectionModels,
  setAIEngine,
  switchDetectionModel,
  testAIBackend,
  testDetectionModel,
  updateAISettings,
  type DetectionModels,
  type EngineDevice,
  type EngineState,
  type EngineStatus,
} from "../api/ai";
import type { AISettings } from "../api/types";
import {
  classesFromGroups,
  DETECT_GROUPS,
  DEVICE_LABEL,
  groupsFromClasses,
  recommend,
  type Recommendation,
} from "./aiSetup";

type Step = "intro" | "detect" | "hardware" | "review" | "apply" | "done";
const STEPS: { key: Step; label: string }[] = [
  { key: "intro", label: "Turn on" },
  { key: "detect", label: "What to detect" },
  { key: "hardware", label: "Check hardware" },
  { key: "review", label: "Choose setup" },
  { key: "apply", label: "Install" },
];

type TaskState = "pending" | "running" | "done" | "failed";
interface Task {
  key: string;
  label: string;
  state: TaskState;
  detail?: string;
}

interface Plan {
  device: "auto" | EngineDevice;
  model: string;
  intervalMs: number;
  confidence: number;
}

const INTERVALS = [250, 500, 1000, 1500, 2000];

function errText(e: unknown): string {
  return (e as Error)?.message || String(e);
}

/** Step-by-step setup for AI detection: turn it on, say what matters, let the
 *  NVR test its own hardware and propose a setup, accept or adjust it, and
 *  have everything downloaded, switched on and checked in one go. Built from
 *  the same endpoints the detailed settings use, so nothing here can get out
 *  of step with them. */
export function AISetupWizard({
  settings,
  onDone,
  onCancel,
}: {
  settings: AISettings;
  onDone: (s: AISettings) => void;
  onCancel: () => void;
}) {
  const [step, setStep] = useState<Step>("intro");
  const [groups, setGroups] = useState<Record<string, boolean>>(() => {
    const saved = groupsFromClasses(settings.detection_classes);
    const any = Object.values(saved).some(Boolean);
    return any ? saved : Object.fromEntries(DETECT_GROUPS.map((g) => [g.key, g.defaultOn]));
  });
  const [alertOnly, setAlertOnly] = useState(settings.alert_on_objects_only);

  // Hardware check
  const [status, setStatus] = useState<EngineStatus | null>(null);
  const [engine, setEngine] = useState<EngineState | null>(null);
  const [models, setModels] = useState<DetectionModels | null>(null);
  const [checking, setChecking] = useState<string | null>(null);
  const [checkErr, setCheckErr] = useState<string | null>(null);
  const [rec, setRec] = useState<Recommendation | null>(null);

  // Review
  const [mode, setMode] = useState<"recommended" | "custom">("recommended");
  const [custom, setCustom] = useState<Plan | null>(null);

  // Install
  const [tasks, setTasks] = useState<Task[]>([]);
  const [applying, setApplying] = useState(false);
  const [result, setResult] = useState<{ settings: AISettings; p50?: number } | null>(null);
  const started = useRef(false);

  const chosenGroups = DETECT_GROUPS.filter((g) => groups[g.key]);
  const plan: Plan | null = rec
    ? mode === "custom" && custom
      ? custom
      : { device: "auto", model: rec.model, intervalMs: rec.intervalMs, confidence: rec.confidence }
    : null;

  // ── Step 3: check the hardware ─────────────────────────────────────────────
  const checkHardware = async () => {
    setCheckErr(null);
    setRec(null);
    try {
      setChecking("Looking for the AI service and graphics cards…");
      const st = await getAIEngine();
      setStatus(st);
      if (!st.reachable || !st.engine) {
        setCheckErr(st.error || "The AI service isn't answering.");
        return;
      }
      setChecking("Testing each device with a real detection (a graphics card's first run can take a minute)…");
      let eng: EngineState = st.engine;
      try {
        eng = await benchmarkAIEngine();
      } catch {
        // A failed speed test still leaves availability to go on.
      }
      setEngine(eng);
      setChecking("Reading the available detection models…");
      const ms = await getDetectionModels().catch(() => null);
      setModels(ms);
      const r = recommend(eng, ms);
      setRec(r);
      // Re-running setup on an NVR that's already set up: "Fine-tune" starts
      // from what it runs today, so keeping a setup is one click, not a re-type.
      const current = settings.enabled && ms?.models.some((m) => m.name === settings.detection_model && m.present);
      setCustom(
        current
          ? {
              device: eng.choice,
              model: settings.detection_model,
              intervalMs: settings.monitor_interval_ms,
              confidence: settings.detection_confidence,
            }
          : { device: r.device, model: r.model, intervalMs: r.intervalMs, confidence: r.confidence },
      );
      if (current) setMode("custom");
    } catch (e) {
      setCheckErr(errText(e));
    } finally {
      setChecking(null);
    }
  };

  useEffect(() => {
    if (step === "hardware" && !rec && !checking && !checkErr) checkHardware();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step]);

  // ── Step 5: install ────────────────────────────────────────────────────────
  const buildTasks = (p: Plan): Task[] => {
    const m = models?.models.find((x) => x.name === p.model);
    const needDownload = !m?.present;
    return [
      { key: "save", label: "Saving what to detect", state: "pending" },
      {
        key: "device",
        label: p.device === "auto" ? `Choosing the fastest device (${rec?.deviceName ?? "automatic"})` : `Switching to the ${DEVICE_LABEL[p.device]}`,
        state: "pending",
      },
      ...(needDownload
        ? [{ key: "download", label: `Downloading the detection model "${m?.label ?? p.model}"${m?.size_mb ? ` (${m.size_mb} MB)` : ""}`, state: "pending" as TaskState }]
        : []),
      { key: "use", label: `Loading "${m?.label ?? p.model}"`, state: "pending" },
      { key: "speed", label: "Measuring its speed on this hardware", state: "pending" },
      { key: "check", label: "Checking detection works end to end", state: "pending" },
    ];
  };

  const runTasks = async (p: Plan, from: Task[]) => {
    setApplying(true);
    let list = from.map((t) => (t.state === "failed" ? { ...t, state: "pending" as TaskState, detail: undefined } : t));
    setTasks(list);
    const set = (key: string, patch: Partial<Task>) => {
      list = list.map((t) => (t.key === key ? { ...t, ...patch } : t));
      setTasks(list);
    };
    let p50: number | undefined;
    for (const t of list) {
      if (t.state === "done") continue;
      set(t.key, { state: "running" });
      try {
        if (t.key === "save") {
          await updateAISettings({
            enabled: true,
            detection_enabled: true,
            detection_classes: classesFromGroups(groups, settings.detection_classes),
            alert_on_objects_only: alertOnly,
            monitor_interval_ms: p.intervalMs,
            detection_confidence: p.confidence,
          });
        } else if (t.key === "device") {
          const st = await setAIEngine(p.device);
          if (st.note) set(t.key, { detail: st.note });
        } else if (t.key === "download") {
          await downloadDetectionModel(p.model);
        } else if (t.key === "use") {
          await switchDetectionModel(p.model);
        } else if (t.key === "speed") {
          const ms = await testDetectionModel(p.model);
          const r = ms.models.find((m) => m.name === p.model)?.result;
          if (r && !r.ok) throw new Error(r.error || "the speed test failed");
          p50 = r?.p50_ms;
          set(t.key, { detail: p50 ? `${p50} ms per check on the ${ms.device_name}` : undefined });
        } else if (t.key === "check") {
          const r = await testAIBackend();
          if (!r.success) throw new Error(r.message);
          set(t.key, { detail: r.message });
        }
        set(t.key, { state: "done" });
      } catch (e) {
        set(t.key, { state: "failed", detail: errText(e) });
        setApplying(false);
        return;
      }
    }
    const fresh = await getAISettings().catch(() => null);
    setResult({ settings: fresh ?? { ...settings, enabled: true }, p50 });
    setApplying(false);
    setStep("done");
  };

  useEffect(() => {
    if (step === "apply" && plan && !started.current) {
      started.current = true;
      runTasks(plan, buildTasks(plan));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step]);

  const failed = tasks.find((t) => t.state === "failed");
  const stepIndex = STEPS.findIndex((s) => s.key === step);

  // ── Rendering ──────────────────────────────────────────────────────────────
  return (
    <div className="card" style={{ display: "grid", gap: 18 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
        <strong style={{ fontSize: 18 }}>Set up smart detection</strong>
        {step !== "done" && !applying && (
          <button className="btn btn-sm" onClick={onCancel}>
            Cancel
          </button>
        )}
      </div>

      {step !== "done" && (
        <ol aria-label="Setup steps" style={{ display: "flex", flexWrap: "wrap", gap: 6, listStyle: "none", padding: 0, margin: 0 }}>
          {STEPS.map((s, i) => {
            const state = i < stepIndex ? "done" : i === stepIndex ? "current" : "todo";
            return (
              <li
                key={s.key}
                aria-current={state === "current" ? "step" : undefined}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  padding: "4px 10px",
                  borderRadius: 999,
                  fontSize: 13,
                  border: `1px solid ${state === "current" ? "var(--accent)" : "var(--border)"}`,
                  background: state === "current" ? "rgba(59,130,246,0.12)" : "transparent",
                  color: state === "todo" ? "var(--text-dim)" : "var(--text)",
                }}
              >
                <span aria-hidden="true" style={{ fontVariantNumeric: "tabular-nums", color: state === "done" ? "var(--success)" : undefined }}>
                  {state === "done" ? "✓" : i + 1}
                </span>
                {s.label}
              </li>
            );
          })}
        </ol>
      )}

      {/* 1 ── Turn on ─────────────────────────────────────────────────────── */}
      {step === "intro" && (
        <div style={{ display: "grid", gap: 12 }}>
          <p style={{ margin: 0 }}>
            Motion alone can't tell a person from a swaying tree, rain or passing headlights. Smart detection checks the
            picture while motion lasts, so an alert means something real was there.
          </p>
          <ul style={{ margin: 0, paddingLeft: 18, color: "var(--text-dim)", fontSize: 14, display: "grid", gap: 4 }}>
            <li>Recording is never affected - it still starts the moment motion is seen.</li>
            <li>Everything runs on this NVR (or a PC you choose); no pictures leave your network.</li>
            <li>This takes about a minute. The NVR tests its own hardware and suggests the best setup.</li>
          </ul>
          <div>
            <button className="btn btn-primary" onClick={() => setStep("detect")}>
              Turn on and continue
            </button>
          </div>
        </div>
      )}

      {/* 2 ── What to detect ──────────────────────────────────────────────── */}
      {step === "detect" && (
        <div style={{ display: "grid", gap: 14 }}>
          <div>
            <strong>What should it look for?</strong>
            <div style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 2 }}>Tick everything you'd want to know about.</div>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 10 }}>
            {DETECT_GROUPS.map((g) => (
              <label
                key={g.key}
                style={{
                  display: "flex",
                  gap: 10,
                  alignItems: "flex-start",
                  padding: 12,
                  borderRadius: 8,
                  cursor: "pointer",
                  border: `1px solid ${groups[g.key] ? "var(--accent)" : "var(--border)"}`,
                  background: groups[g.key] ? "rgba(59,130,246,0.08)" : "transparent",
                }}
              >
                <input
                  type="checkbox"
                  checked={!!groups[g.key]}
                  onChange={(e) => setGroups((p) => ({ ...p, [g.key]: e.target.checked }))}
                  style={{ marginTop: 3 }}
                />
                <span>
                  <strong>{g.label}</strong>
                  <div style={{ color: "var(--text-dim)", fontSize: 12, marginTop: 2 }}>{g.hint}</div>
                </span>
              </label>
            ))}
          </div>
          <label style={{ display: "flex", gap: 10, alignItems: "flex-start", cursor: "pointer" }}>
            <input type="checkbox" checked={alertOnly} onChange={(e) => setAlertOnly(e.target.checked)} style={{ marginTop: 3 }} />
            <span>
              Only alert me when one of these is seen <span style={{ color: "var(--success)", fontSize: 12 }}>recommended</span>
              <div style={{ color: "var(--text-dim)", fontSize: 12, marginTop: 2 }}>
                This is what stops false alarms from trees, rain and shadows. Motion is still recorded either way.
              </div>
            </span>
          </label>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button className="btn" onClick={() => setStep("intro")}>
              Back
            </button>
            <button className="btn btn-primary" disabled={chosenGroups.length === 0} onClick={() => setStep("hardware")}>
              Next: check hardware
            </button>
            {chosenGroups.length === 0 && (
              <span style={{ fontSize: 13, color: "var(--text-dim)", alignSelf: "center" }}>Pick at least one.</span>
            )}
          </div>
        </div>
      )}

      {/* 3 ── Hardware ────────────────────────────────────────────────────── */}
      {step === "hardware" && (
        <div style={{ display: "grid", gap: 14 }}>
          <div>
            <strong>Checking this NVR's hardware</strong>
            <div style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 2 }}>
              Finds what can run AI here and times a real detection on each.
            </div>
          </div>

          {checking && (
            <div role="status" aria-live="polite" style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <span className="spinner" aria-hidden="true" style={{ width: 16, height: 16, border: "2px solid var(--border)", borderTopColor: "var(--accent)", borderRadius: "50%", display: "inline-block", animation: "spin 0.9s linear infinite" }} />
              <span>{checking}</span>
            </div>
          )}

          {checkErr && (
            <div style={{ display: "grid", gap: 8, padding: 12, borderRadius: 8, border: "1px solid var(--danger)" }}>
              <span>
                <strong style={{ color: "var(--danger)" }}>The AI service can't be reached.</strong> {checkErr}
              </span>
              {status?.hints.map((h) => (
                <div key={h} style={{ fontSize: 13, color: "var(--text-dim)" }}>
                  {h}
                </div>
              ))}
              <div>
                <button className="btn btn-sm" onClick={checkHardware}>
                  Try again
                </button>
              </div>
            </div>
          )}

          {engine && (
            <div style={{ display: "grid", gap: 6 }}>
              {engine.devices.map((d) => (
                <div key={d.id} style={{ display: "flex", gap: 10, alignItems: "baseline", flexWrap: "wrap" }}>
                  <span aria-hidden="true" style={{ color: d.available && (!d.result || d.result.ok) ? "var(--success)" : "var(--text-dim)" }}>
                    {d.available && (!d.result || d.result.ok) ? "✓" : "–"}
                  </span>
                  <span style={{ flex: "1 1 200px" }}>{d.available ? d.name : DEVICE_LABEL[d.id]}</span>
                  <span style={{ fontSize: 13, color: "var(--text-dim)", fontVariantNumeric: "tabular-nums" }}>
                    {!d.available
                      ? d.reason || "not available"
                      : d.result?.ok && d.result.p50_ms
                        ? `${d.result.p50_ms} ms per check`
                        : d.result && !d.result.ok
                          ? `test failed: ${d.result.error}`
                          : "available"}
                  </span>
                </div>
              ))}
              {status?.hints.map((h) => (
                <div key={h} style={{ fontSize: 13, color: "var(--text-dim)", padding: "6px 8px", borderRadius: 6, background: "var(--bg)" }}>
                  {h}
                </div>
              ))}
            </div>
          )}

          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button className="btn" disabled={!!checking} onClick={() => setStep("detect")}>
              Back
            </button>
            <button className="btn btn-primary" disabled={!rec || !!checking} onClick={() => setStep("review")}>
              Next: see the recommended setup
            </button>
          </div>
        </div>
      )}

      {/* 4 ── Review ──────────────────────────────────────────────────────── */}
      {step === "review" && rec && plan && custom && (
        <div style={{ display: "grid", gap: 14 }}>
          <strong>Choose your setup</strong>

          <label
            style={{
              display: "grid",
              gap: 8,
              padding: 14,
              borderRadius: 8,
              cursor: "pointer",
              border: `2px solid ${mode === "recommended" ? "var(--accent)" : "var(--border)"}`,
            }}
          >
            <span style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <input type="radio" name="ai-setup-mode" checked={mode === "recommended"} onChange={() => setMode("recommended")} />
              <strong>Use the recommended setup</strong>
              <span style={{ color: "var(--success)", fontSize: 12 }}>best for this hardware</span>
            </span>
            <dl style={{ margin: "0 0 0 26px", display: "grid", gridTemplateColumns: "max-content 1fr", gap: "4px 14px", fontSize: 14 }}>
              <dt style={{ color: "var(--text-dim)" }}>Runs on</dt>
              <dd style={{ margin: 0 }}>{rec.deviceName} <span style={{ color: "var(--text-dim)", fontSize: 12 }}>(switches to the CPU by itself if it ever fails)</span></dd>
              <dt style={{ color: "var(--text-dim)" }}>Detection model</dt>
              <dd style={{ margin: 0 }}>{rec.modelLabel} <span style={{ color: "var(--text-dim)", fontSize: 12 }}>({rec.model})</span></dd>
              <dt style={{ color: "var(--text-dim)" }}>Looks for</dt>
              <dd style={{ margin: 0 }}>{chosenGroups.map((g) => g.label).join(", ")}</dd>
              <dt style={{ color: "var(--text-dim)" }}>Checks during motion</dt>
              <dd style={{ margin: 0 }}>every {rec.intervalMs / 1000} s per camera</dd>
              <dt style={{ color: "var(--text-dim)" }}>How sure it must be</dt>
              <dd style={{ margin: 0 }}>{rec.confidence}%</dd>
            </dl>
            <ul style={{ margin: "0 0 0 26px", paddingLeft: 18, fontSize: 13, color: "var(--text-dim)", display: "grid", gap: 2 }}>
              {rec.why.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          </label>

          <label
            style={{
              display: "grid",
              gap: 8,
              padding: 14,
              borderRadius: 8,
              cursor: "pointer",
              border: `2px solid ${mode === "custom" ? "var(--accent)" : "var(--border)"}`,
            }}
          >
            <span style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <input type="radio" name="ai-setup-mode" checked={mode === "custom"} onChange={() => setMode("custom")} />
              <strong>{settings.enabled ? "Keep or fine-tune my current setup" : "Fine-tune it myself"}</strong>
            </span>
            <span style={{ marginLeft: 26, fontSize: 13, color: "var(--text-dim)" }}>
              {settings.enabled
                ? "Starts from what this NVR runs today; change any part."
                : "Start from the recommendation and change any part."}
            </span>
          </label>

          {mode === "custom" && engine && (
            <div style={{ display: "grid", gap: 12, marginLeft: 8 }}>
              <div className="field" style={{ margin: 0 }}>
                <label htmlFor="ai-setup-device">Runs on</label>
                <select
                  id="ai-setup-device"
                  value={custom.device}
                  onChange={(e) => setCustom({ ...custom, device: e.target.value as Plan["device"] })}
                >
                  <option value="auto">Automatic - the fastest that works ({rec.deviceName})</option>
                  {engine.devices.map((d) => (
                    <option key={d.id} value={d.id} disabled={!d.available}>
                      {d.available ? d.name : `${DEVICE_LABEL[d.id]} - not available`}
                      {d.result?.ok && d.result.p50_ms ? ` · ${d.result.p50_ms} ms per check` : ""}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field" style={{ margin: 0 }}>
                <label htmlFor="ai-setup-model">Detection model</label>
                <select id="ai-setup-model" value={custom.model} onChange={(e) => setCustom({ ...custom, model: e.target.value })}>
                  {(models?.models ?? []).map((m) => (
                    <option key={m.name} value={m.name} disabled={!m.present && !m.downloadable}>
                      {m.label} ({m.name}) · accuracy {m.accuracy.toFixed(0)}
                      {m.present ? " · downloaded" : m.size_mb ? ` · ${m.size_mb} MB download` : ""}
                      {m.name === rec.model ? " · recommended" : ""}
                    </option>
                  ))}
                </select>
                <p style={{ fontSize: 12, color: "var(--text-dim)", margin: "4px 0 0" }}>
                  {models?.models.find((m) => m.name === custom.model)?.about}
                </p>
              </div>
              <div className="field" style={{ margin: 0 }}>
                <label htmlFor="ai-setup-interval">Check each camera during motion every</label>
                <select
                  id="ai-setup-interval"
                  value={custom.intervalMs}
                  onChange={(e) => setCustom({ ...custom, intervalMs: Number(e.target.value) })}
                >
                  {[...new Set([...INTERVALS, custom.intervalMs])].sort((a, b) => a - b).map((ms) => (
                    <option key={ms} value={ms}>
                      {ms / 1000} s{ms === rec.intervalMs ? " (recommended)" : ""}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field" style={{ margin: 0 }}>
                <label htmlFor="ai-setup-conf">How sure it must be: {custom.confidence}%</label>
                <input
                  id="ai-setup-conf"
                  type="range"
                  min={1}
                  max={99}
                  value={custom.confidence}
                  onChange={(e) => setCustom({ ...custom, confidence: Number(e.target.value) })}
                />
                <p style={{ fontSize: 12, color: "var(--text-dim)", margin: "4px 0 0" }}>
                  Lower catches more but imagines things; higher misses far-away or partly hidden objects.
                </p>
              </div>
            </div>
          )}

          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button className="btn" onClick={() => setStep("hardware")}>
              Back
            </button>
            <button
              className="btn btn-primary"
              onClick={() => {
                started.current = false;
                setStep("apply");
              }}
            >
              {models?.models.find((m) => m.name === plan.model)?.present ? "Turn it on" : "Download and turn it on"}
            </button>
          </div>
        </div>
      )}

      {/* 5 ── Install ─────────────────────────────────────────────────────── */}
      {step === "apply" && plan && (
        <div style={{ display: "grid", gap: 12 }}>
          <strong>{failed ? "Setup stopped" : "Setting it up…"}</strong>
          <div role="status" aria-live="polite" style={{ display: "grid", gap: 8 }}>
            {tasks.map((t) => (
              <div key={t.key} style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
                <span aria-hidden="true" style={{ width: 18, textAlign: "center", color: t.state === "done" ? "var(--success)" : t.state === "failed" ? "var(--danger)" : t.state === "running" ? "var(--accent)" : "var(--text-dim)" }}>
                  {t.state === "done" ? "✓" : t.state === "failed" ? "✗" : t.state === "running" ? "…" : "○"}
                </span>
                <span>
                  <span style={{ color: t.state === "pending" ? "var(--text-dim)" : "var(--text)" }}>{t.label}</span>
                  {t.detail && (
                    <div style={{ fontSize: 12, color: t.state === "failed" ? "var(--danger)" : "var(--text-dim)" }}>{t.detail}</div>
                  )}
                </span>
              </div>
            ))}
          </div>
          {failed && (
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button className="btn" disabled={applying} onClick={() => setStep("review")}>
                Back to the setup
              </button>
              <button className="btn btn-primary" disabled={applying} onClick={() => runTasks(plan, tasks)}>
                Try again
              </button>
            </div>
          )}
        </div>
      )}

      {/* 6 ── Done ────────────────────────────────────────────────────────── */}
      {step === "done" && result && plan && (
        <div style={{ display: "grid", gap: 12 }}>
          <div style={{ display: "flex", gap: 10, alignItems: "center", padding: "12px 14px", borderRadius: 8, background: "rgba(34,197,94,0.1)", border: "1px solid rgba(34,197,94,0.5)" }}>
            <span aria-hidden="true" style={{ color: "var(--success)", fontSize: 18 }}>✓</span>
            <span>
              <strong>Smart detection is on.</strong> {rec ? `Running "${models?.models.find((m) => m.name === plan.model)?.label ?? plan.model}" on the ${plan.device === "auto" ? rec.deviceName : DEVICE_LABEL[plan.device]}` : ""}
              {result.p50 ? `, about ${result.p50} ms per check` : ""}.
            </span>
          </div>
          <p style={{ margin: 0, color: "var(--text-dim)", fontSize: 14 }}>
            Looking for {chosenGroups.map((g) => g.label.toLowerCase()).join(", ")}.
            {alertOnly ? " You'll only be alerted when one of them is seen." : " Every motion event still alerts; AI labels what it saw."} Everything can
            be changed on the AI settings page.
          </p>
          <div>
            <button className="btn btn-primary" onClick={() => onDone(result.settings)}>
              Open AI settings
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
