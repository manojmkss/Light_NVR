import { useEffect, useState } from "react";
import {
  downloadDetectionModel,
  getDetectionModels,
  testDetectionModel,
  switchDetectionModel,
  type DetectionModel,
  type DetectionModels,
} from "../api/ai";
import type { AISettings } from "../api/types";

/** Which model finds people, vehicles and animals: ready-made models
 *  published by LightNVR, downloaded on request (checksum-verified), timed on
 *  the device in use, and switched to without a restart. */
export function DetectionModelCard({ settings, onSettings }: { settings: AISettings; onSettings: (s: AISettings) => void }) {
  const [st, setSt] = useState<DetectionModels | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    getDetectionModels()
      .then(setSt)
      .catch((e) => setErr((e as Error).message));
  }, [settings.remote_url]);

  const act = async (key: string, fn: () => Promise<DetectionModels>, after?: () => void) => {
    setBusy(key);
    setErr(null);
    try {
      setSt(await fn());
      after?.();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  // A GPU runs the mid-size model faster than a CPU runs the smallest one.
  const recommended = st && st.device !== "cpu" ? "yolo26s" : "yolo26n";
  // During motion each camera is checked every `monitor_interval_ms`.
  const budget = settings.monitor_interval_ms / 2;

  const speed = (m: DetectionModel) => {
    if (m.result?.ok && m.result.p50_ms) return `${m.result.p50_ms} ms per check`;
    if (m.result && !m.result.ok) return "test failed";
    return m.present ? "not tested" : "not downloaded";
  };

  return (
    <div className="card">
      <strong>Detection model</strong>
      <div style={{ color: "var(--text-dim)", fontSize: 12, margin: "4px 0 10px" }}>
        What finds people, vehicles and animals in the picture. A bigger model finds more - especially people far
        away, in the dark or partly hidden - but takes longer per check.
        {st && <> Speeds are measured on the <strong>{st.device_name}</strong> (change it above).</>}
      </div>

      {!st && !err && <div style={{ fontSize: 13, color: "var(--text-dim)" }}>Checking…</div>}

      {st && (
        <div style={{ display: "grid", gap: 8 }}>
          {st.models.map((m) => {
            const inUse = st.in_use === m.name;
            const slow = !!m.result?.ok && (m.result.p50_ms ?? 0) > budget;
            return (
              <div
                key={m.name}
                style={{
                  display: "flex",
                  gap: 10,
                  alignItems: "flex-start",
                  flexWrap: "wrap",
                  padding: "8px 10px",
                  borderRadius: 6,
                  border: `1px solid ${inUse ? "var(--success)" : "var(--border)"}`,
                }}
              >
                <div style={{ flex: "1 1 240px" }}>
                  <span style={{ fontWeight: 600 }}>{m.label}</span>
                  <span style={{ color: "var(--text-dim)", fontSize: 12 }}> · {m.name}</span>
                  {m.name === recommended && !inUse && (
                    <span style={{ color: "var(--accent, #58a6ff)", fontSize: 12 }}> · recommended here</span>
                  )}
                  {inUse && <span style={{ color: "var(--success)", fontSize: 12 }}> · in use</span>}
                  <div style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 2 }}>{m.about}</div>
                  <div style={{ fontSize: 12, marginTop: 4, display: "flex", gap: 12, flexWrap: "wrap" }}>
                    <span title="COCO mAP50-95, as published by Ultralytics - higher finds more">
                      Accuracy <strong>{m.accuracy.toFixed(0)}</strong>
                      <span
                        style={{
                          display: "inline-block",
                          width: 60,
                          height: 6,
                          marginLeft: 6,
                          borderRadius: 3,
                          background: "var(--bg)",
                          verticalAlign: "middle",
                          overflow: "hidden",
                        }}
                      >
                        <span style={{ display: "block", height: "100%", width: `${Math.max(0, Math.min(100, ((m.accuracy - 30) / 25) * 100))}%`, background: "var(--success)" }} />
                      </span>
                    </span>
                    <span style={{ fontVariantNumeric: "tabular-nums", color: slow ? "#d29922" : undefined }}>{speed(m)}</span>
                  </div>
                  {slow && (
                    <div style={{ fontSize: 12, color: "#d29922", marginTop: 2 }}>
                      Slower than a check every {settings.monitor_interval_ms} ms needs - fine for one or two cameras in
                      motion at once; more will be checked less often.
                    </div>
                  )}
                  {m.result && !m.result.ok && (
                    <div style={{ fontSize: 12, color: "var(--danger)", marginTop: 2 }}>{m.result.error}</div>
                  )}
                </div>
                <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                  {!m.present && m.downloadable && (
                    <button
                      className="btn btn-sm"
                      disabled={!!busy}
                      onClick={() => act(`dl:${m.name}`, () => downloadDetectionModel(m.name))}
                    >
                      {busy === `dl:${m.name}` ? "Downloading…" : `Download${m.size_mb ? ` (${m.size_mb} MB)` : ""}`}
                    </button>
                  )}
                  {!m.present && !m.downloadable && (
                    <span style={{ fontSize: 12, color: "var(--text-dim)" }}>not published yet</span>
                  )}
                  {m.present && (
                    <button className="btn btn-sm" disabled={!!busy} onClick={() => act(`test:${m.name}`, () => testDetectionModel(m.name))}>
                      {busy === `test:${m.name}` ? "Testing…" : "Test speed"}
                    </button>
                  )}
                  {m.present && !inUse && (
                    <button
                      className="btn btn-sm btn-primary"
                      disabled={!!busy}
                      onClick={() =>
                        act(`use:${m.name}`, () => switchDetectionModel(m.name), () => onSettings({ ...settings, detection_model: m.name }))
                      }
                    >
                      {busy === `use:${m.name}` ? "Switching…" : "Use"}
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {busy?.startsWith("test:") || busy?.startsWith("use:") ? (
        <div style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 6 }}>A GPU&apos;s first run of a model takes a moment.</div>
      ) : null}
      {st && (
        <div style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 8 }}>
          Downloaded from LightNVR&apos;s GitHub releases only when you press Download, and checked against the checksum
          this version ships with. Licence: {st.license}.
        </div>
      )}
      {err && <div className="error-text" style={{ marginTop: 6 }}>{err}</div>}
    </div>
  );
}
