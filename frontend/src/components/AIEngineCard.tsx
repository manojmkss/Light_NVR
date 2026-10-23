import { useEffect, useState } from "react";
import {
  benchmarkAIEngine,
  getAIEngine,
  setAIEngine,
  updateAISettings,
  type EngineDevice,
  type EngineDeviceInfo,
  type EngineState,
  type EngineStatus,
} from "../api/ai";
import type { AISettings } from "../api/types";

// This NVR's own AI service (a separate container set up by the install/update
// scripts), reached over the compose network.
export const SIDECAR_URL = "http://ai-worker:8811";

type Choice = "auto" | EngineDevice | "other_pc";

const LOCAL: { key: "auto" | EngineDevice; label: string; hint: string }[] = [
  { key: "auto", label: "Automatic", hint: "the fastest device on this NVR that works (recommended)" },
  { key: "cpu", label: "CPU", hint: "" },
  { key: "intel_gpu", label: "Intel GPU", hint: "" },
  { key: "nvidia_gpu", label: "NVIDIA GPU", hint: "" },
];

/** The one-time model download, for the OS the viewer is likely on. */
function modelCommand(model: string): { primary: string; other: string; otherLabel: string } {
  const isWindows = typeof navigator !== "undefined" && /Windows/i.test(navigator.userAgent);
  const ps = `.\\scripts\\fetch-ai-models.ps1 -Model ${model}`;
  const sh = `./scripts/fetch-ai-models.sh ${model}`;
  return isWindows
    ? { primary: ps, other: sh, otherLabel: "On Linux / macOS instead" }
    : { primary: sh, other: ps, otherLabel: "On Windows (PowerShell) instead" };
}

function speed(d: EngineDeviceInfo | undefined): string {
  if (!d) return "";
  if (d.result?.ok && d.result.p50_ms) return `${d.result.p50_ms} ms per check`;
  if (d.result && !d.result.ok) return "test failed";
  return d.available ? "not tested yet" : "not available";
}

/** Where AI runs and on what - one choice: this NVR's AI service on its CPU /
 *  Intel GPU / NVIDIA GPU (or Automatic), or another PC running the AI
 *  worker. Applied straight away; AI never runs inside the NVR itself. */
export function AIEngineCard({ settings, onSettings }: { settings: AISettings; onSettings: (s: AISettings) => void }) {
  const [st, setSt] = useState<EngineStatus | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const onOtherPc = settings.remote_url !== SIDECAR_URL;
  const [url, setUrl] = useState(onOtherPc ? settings.remote_url : "");
  const [key, setKey] = useState("");
  const [showOther, setShowOther] = useState(onOtherPc);

  const load = () => getAIEngine().then(setSt).catch((e) => setErr((e as Error).message));
  useEffect(() => {
    load();
  }, [settings.remote_url]);

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

  /** Point AI at this NVR's own service (if it isn't already), then pick the device. */
  const useLocal = (device: "auto" | EngineDevice) =>
    act("set", async () => {
      if (onOtherPc) onSettings(await updateAISettings({ backend: "remote", remote_url: SIDECAR_URL, remote_api_key: "" }));
      await setAIEngine(device);
    });

  const connectOther = () =>
    act("connect", async () => {
      const target = url.trim().replace(/\/+$/, "");
      if (!/^https?:\/\/.+/.test(target)) throw new Error("Enter the AI worker's address, e.g. http://192.168.1.50:8811");
      onSettings(await updateAISettings({ backend: "remote", remote_url: target, ...(key ? { remote_api_key: key } : {}) }));
      setKey("");
    });

  const local: EngineState | null = st?.local_engine ?? null;
  const remote: EngineState | null = onOtherPc ? (st?.engine ?? null) : null;
  const selected: Choice = onOtherPc || showOther ? "other_pc" : (local?.choice ?? "auto");
  const device = (id: string, eng: EngineState | null) => eng?.devices.find((d) => d.id === id);

  return (
    <div className="card">
      <strong>AI engine</strong>
      <div style={{ color: "var(--text-dim)", fontSize: 12, margin: "4px 0 10px" }}>
        What runs the AI. It always runs in its own service, never inside the NVR, so it can't slow down or stop
        recording. A GPU checks pictures many times faster, so more cameras can be watched at once.
      </div>

      {!st && !err && <div style={{ fontSize: 13, color: "var(--text-dim)" }}>Checking…</div>}

      <div style={{ display: "grid", gap: 6 }}>
        {LOCAL.map((o) => {
          const d = o.key === "auto" ? undefined : device(o.key, local);
          const usable = o.key === "auto" ? !!local : !!d?.available;
          const inUse = !onOtherPc && local && (o.key === "auto" ? local.choice === "auto" : local.active === o.key);
          return (
            <label
              key={o.key}
              style={{ display: "flex", gap: 10, alignItems: "flex-start", opacity: usable ? 1 : 0.6, cursor: usable ? "pointer" : "default" }}
            >
              <input
                type="radio"
                name="ai-engine"
                checked={selected === o.key}
                disabled={!usable || !!busy}
                onChange={() => {
                  setShowOther(false);
                  useLocal(o.key);
                }}
                style={{ marginTop: 3 }}
              />
              <span style={{ flex: 1 }}>
                <span style={{ fontWeight: selected === o.key ? 600 : 400 }}>
                  {o.key === "auto" ? o.label : d?.name ?? o.label}
                </span>
                {o.hint && <span style={{ color: "var(--text-dim)", fontSize: 12 }}> — {o.hint}</span>}
                {o.key === "auto" && local && local.choice === "auto" && !onOtherPc && (
                  <span style={{ color: "var(--success)", fontSize: 12 }}> · using {device(local.active, local)?.name}</span>
                )}
                {d && !d.available && d.reason && (
                  <div style={{ fontSize: 12, color: "var(--text-dim)" }}>{d.reason}</div>
                )}
                {d?.result && !d.result.ok && (
                  <div style={{ fontSize: 12, color: "var(--danger)" }}>Test failed: {d.result.error}</div>
                )}
              </span>
              <span style={{ fontSize: 13, whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums", color: "var(--text-dim)" }}>
                {o.key === "auto" ? "" : speed(d)}
                {inUse && o.key !== "auto" && <span style={{ color: "var(--success)" }}> · in use</span>}
              </span>
            </label>
          );
        })}

        <label style={{ display: "flex", gap: 10, alignItems: "flex-start", cursor: "pointer" }}>
          <input
            type="radio"
            name="ai-engine"
            checked={selected === "other_pc"}
            disabled={!!busy}
            onChange={() => setShowOther(true)}
            style={{ marginTop: 3 }}
          />
          <span style={{ flex: 1 }}>
            <span style={{ fontWeight: selected === "other_pc" ? 600 : 400 }}>Another PC on my network</span>
            <span style={{ color: "var(--text-dim)", fontSize: 12 }}> — a PC (e.g. with a gaming GPU) running the LightNVR AI worker</span>
          </span>
        </label>
        {selected === "other_pc" && (
          <div style={{ marginLeft: 26, display: "grid", gap: 6 }}>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              <input
                type="text"
                placeholder="http://192.168.1.50:8811"
                value={url}
                style={{ flex: "1 1 220px" }}
                onChange={(e) => setUrl(e.target.value)}
              />
              <input
                type="password"
                placeholder={settings.has_remote_api_key && onOtherPc ? "password saved" : "password (optional)"}
                value={key}
                style={{ flex: "0 1 170px" }}
                onChange={(e) => setKey(e.target.value)}
              />
              <button className="btn btn-sm btn-primary" disabled={!!busy || !url.trim()} onClick={connectOther}>
                {busy === "connect" ? "Connecting…" : onOtherPc ? "Save" : "Connect"}
              </button>
            </div>
            <div style={{ fontSize: 12, color: "var(--text-dim)" }}>
              The LightNVR <code>ai-worker</code> (the <code>ai-worker/</code> folder of the project) - not LM Studio or
              Ollama, which are for descriptions. Pictures go to that PC on your own network only.
            </div>
            {onOtherPc && st && !st.reachable && (
              <div className="error-text" style={{ fontSize: 13 }}>That PC isn&apos;t answering ({st.error}).</div>
            )}
            {remote && (
              <div style={{ fontSize: 13 }}>
                That PC runs on <strong>{device(remote.active, remote)?.name}</strong>
                {device(remote.active, remote)?.result?.ok ? ` (${speed(device(remote.active, remote))})` : ""}.{" "}
                <select
                  value={remote.choice}
                  style={{ width: "auto" }}
                  disabled={!!busy}
                  onChange={(e) => act("set", () => setAIEngine(e.target.value as "auto" | EngineDevice))}
                >
                  {LOCAL.map((o) => {
                    const d = o.key === "auto" ? undefined : device(o.key, remote);
                    return (
                      <option key={o.key} value={o.key} disabled={!!d && !d.available}>
                        {o.label}
                        {d && !d.available ? " - not available" : ""}
                      </option>
                    );
                  })}
                </select>
              </div>
            )}
          </div>
        )}
      </div>

      {(onOtherPc ? remote : local)?.model_ready === false && (
        <div style={{ fontSize: 13, marginTop: 10, padding: "6px 8px", borderRadius: 6, background: "var(--bg)" }}>
          💡 The detection model ({(onOtherPc ? remote : local)!.model}) isn&apos;t downloaded yet, so nothing can be
          detected. Download one under <strong>Detection model</strong> below (one click), or without internet access
          on the AI service, run this on {onOtherPc ? "that PC" : "the machine running LightNVR"}:
          <code style={{ display: "block", padding: "6px 8px", marginTop: 4, background: "var(--bg-alt, #0d1117)", borderRadius: 4 }}>
            {modelCommand((onOtherPc ? remote : local)!.model ?? "yolov8n").primary}
            {onOtherPc ? " --volume lightnvr-ai-worker-models" : ""}
          </code>
          <details style={{ marginTop: 4 }}>
            <summary style={{ cursor: "pointer", fontSize: 12 }}>
              {modelCommand((onOtherPc ? remote : local)!.model ?? "yolov8n").otherLabel}
            </summary>
            <code style={{ display: "block", padding: "6px 8px", marginTop: 4, background: "var(--bg-alt, #0d1117)", borderRadius: 4 }}>
              {modelCommand((onOtherPc ? remote : local)!.model ?? "yolov8n").other}
            </code>
          </details>
        </div>
      )}

      {(local?.note || remote?.note) && (
        <div style={{ fontSize: 13, color: "#d29922", marginTop: 8 }}>{onOtherPc ? remote?.note : local?.note}</div>
      )}

      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginTop: 10 }}>
        <button className="btn btn-sm" disabled={!!busy || !st?.reachable} onClick={() => act("bench", benchmarkAIEngine)}>
          {busy === "bench" ? "Testing… (a GPU's first run takes a moment)" : "Test again"}
        </button>
        {busy === "set" && <span style={{ fontSize: 12, color: "var(--text-dim)" }}>Switching…</span>}
      </div>

      {st && !st.local_engine && !onOtherPc && (
        <div style={{ fontSize: 13, marginTop: 10, padding: "6px 8px", borderRadius: 6, background: "var(--bg)" }}>
          💡 This NVR&apos;s AI service isn&apos;t running. Run the update once (<code>./scripts/update-linux.sh</code>,
          or <code>update-windows.ps1</code> on Windows) - it sets it up for this machine&apos;s hardware. Until then
          motion alerts still arrive, marked &quot;AI unavailable&quot;.
        </div>
      )}
      {st && st.hints.length > 0 && (
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
