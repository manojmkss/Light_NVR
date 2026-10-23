import { useEffect, useState } from "react";
import { benchmarkAIEngine, getAIEngine, setAIEngine, type EngineDevice, type EngineStatus } from "../api/ai";

const CHOICES: { key: "auto" | EngineDevice; label: string }[] = [
  { key: "auto", label: "Automatic (fastest that works)" },
  { key: "cpu", label: "CPU" },
  { key: "intel_gpu", label: "Intel GPU" },
  { key: "nvidia_gpu", label: "NVIDIA GPU" },
];

/** Which device runs the AI - picked here, applied instantly by the AI
 *  service. Hardware itself is connected by the install/update scripts; when
 *  that's the missing step, this card says so. */
export function AIEngineCard({ onUseThisNvr }: { onUseThisNvr: () => void }) {
  const [st, setSt] = useState<EngineStatus | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = () => getAIEngine().then(setSt).catch((e) => setErr((e as Error).message));
  useEffect(() => {
    load();
  }, []);

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    setErr(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  if (!st) return err ? <div className="card error-text">{err}</div> : null;
  const eng = st.engine;
  const tested = (eng?.devices ?? []).filter((d) => d.result?.ok && d.result.p50_ms);
  const fastest = tested.sort((a, b) => (a.result!.p50_ms ?? 1e9) - (b.result!.p50_ms ?? 1e9))[0];

  return (
    <div className="card">
      <strong>AI engine</strong>
      <div style={{ color: "var(--text-dim)", fontSize: 12, margin: "4px 0 8px" }}>
        Which part of the computer runs the AI. A GPU checks pictures many times faster, so more cameras can be
        watched at once. Switching is instant.
      </div>

      {!st.in_use && (
        <div style={{ fontSize: 13, marginBottom: 8 }}>
          {st.reachable ? (
            <>
              AI currently runs inside the NVR on its CPU. To choose the engine (and use a GPU), run AI on this
              NVR&apos;s AI service:{" "}
              <button className="btn btn-sm btn-primary" onClick={onUseThisNvr}>
                Use this NVR&apos;s AI service
              </button>{" "}
              <span style={{ color: "var(--text-dim)" }}>(then Save)</span>
            </>
          ) : (
            <span style={{ color: "var(--text-dim)" }}>
              AI runs inside the NVR on its CPU. This NVR&apos;s AI service isn&apos;t running - run the update once
              (./scripts/update-linux.sh) to add it; it can then use a GPU.
            </span>
          )}
        </div>
      )}

      {st.in_use && !st.reachable && (
        <div className="error-text" style={{ fontSize: 13, marginBottom: 8 }}>
          The AI service at {st.worker_url} isn&apos;t answering ({st.error}).
        </div>
      )}

      {eng && (st.in_use || st.local) && (
        <>
          <div className="table-wrap" style={{ border: "none" }}>
            <table>
              <tbody>
                {eng.devices.map((d) => {
                  const active = d.id === eng.active;
                  const ms = d.result?.ok ? d.result.p50_ms : undefined;
                  return (
                    <tr key={d.id}>
                      <td style={{ width: 24, color: active ? "var(--success)" : "var(--text-dim)" }}>{active ? "✓" : ""}</td>
                      <td>
                        <span style={{ fontWeight: active ? 600 : 400 }}>{d.name}</span>
                        {active && <span style={{ color: "var(--success)", fontSize: 12 }}> · in use</span>}
                        {!d.available && d.reason && (
                          <div style={{ fontSize: 12, color: "var(--text-dim)" }}>{d.reason}</div>
                        )}
                        {d.result && !d.result.ok && (
                          <div style={{ fontSize: 12, color: "var(--danger)" }}>Test failed: {d.result.error}</div>
                        )}
                      </td>
                      <td style={{ whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums" }}>
                        {ms ? `${ms} ms per check` : d.available ? "not tested" : "not available"}
                        {ms && fastest && fastest.id !== d.id && fastest.result?.p50_ms && (
                          <div style={{ fontSize: 11, color: "var(--text-dim)" }}>
                            {(ms / fastest.result.p50_ms).toFixed(1)}× slower
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {eng.note && <div style={{ fontSize: 13, color: "#d29922", marginTop: 6 }}>{eng.note}</div>}
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginTop: 10 }}>
            <select
              value={eng.choice}
              style={{ width: "auto" }}
              disabled={!!busy}
              onChange={(e) => act("set", () => setAIEngine(e.target.value as "auto" | EngineDevice))}
            >
              {CHOICES.map((c) => {
                const dev = eng.devices.find((d) => d.id === c.key);
                return (
                  <option key={c.key} value={c.key} disabled={dev ? !dev.available : false}>
                    {c.label}
                    {dev && !dev.available ? " - not available" : ""}
                  </option>
                );
              })}
            </select>
            <button className="btn btn-sm" disabled={!!busy} onClick={() => act("bench", benchmarkAIEngine)}>
              {busy === "bench" ? "Testing… (a GPU's first run takes a moment)" : "Test again"}
            </button>
            {busy === "set" && <span style={{ fontSize: 12, color: "var(--text-dim)" }}>Switching…</span>}
          </div>
        </>
      )}

      {st.hints.length > 0 && (
        <div style={{ marginTop: 10, display: "grid", gap: 4 }}>
          {st.hints.map((h) => (
            <div key={h} style={{ fontSize: 13, padding: "6px 8px", borderRadius: 6, background: "var(--bg)" }}>
              💡 {h}
            </div>
          ))}
        </div>
      )}
      {err && <div className="error-text" style={{ marginTop: 6 }}>{err}</div>}
    </div>
  );
}
