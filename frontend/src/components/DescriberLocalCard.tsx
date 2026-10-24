import { useEffect, useRef, useState } from "react";
import {
  deleteDescriberModel,
  downloadDescriberModel,
  getDescriber,
  switchToLocalDescriber,
  testDescriberModel,
  type DescriberModel,
  type DescriberStatus,
} from "../api/ai";
import type { AISettings } from "../api/types";

const FIT_COLOUR = { good: "var(--success)", slow: "#d29922", no: "var(--text-dim)" } as const;

/** Descriptions from this NVR's own describer (Ollama with a small vision
 *  model, added by the update when the machine can run one): what the
 *  machine is, which models fit and how long they take, then download → test
 *  on one of your own pictures → use. */
export function DescriberLocalCard({ onSettings }: { onSettings: (s: AISettings) => void }) {
  const [st, setSt] = useState<DescriberStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const timer = useRef<number | null>(null);

  const load = () =>
    getDescriber()
      .then((r) => {
        setSt(r);
        setErr(null);
      })
      .catch((e) => setErr((e as Error).message));

  useEffect(() => {
    load();
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, []);

  // Follow a download/test while one runs.
  useEffect(() => {
    if (!st?.models.some((m) => m.job?.status === "running")) return;
    timer.current = window.setTimeout(load, 2000);
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [st]);

  const act = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key);
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

  if (!st) return <div style={{ fontSize: 13, color: "var(--text-dim)" }}>{err ?? "Checking this machine…"}</div>;

  const running = st.models.some((m) => m.job?.status === "running");

  return (
    <div style={{ display: "grid", gap: 8, marginBottom: 16 }}>
      {st.machine && (
        <div style={{ fontSize: 13 }}>
          This machine: <strong>{st.machine}</strong>.
        </div>
      )}
      {st.state !== "ready" && (
        <div style={{ fontSize: 13, padding: "6px 8px", borderRadius: 6, background: "var(--bg)" }}>💡 {st.why}</div>
      )}
      {st.state === "ready" && (
        <div style={{ fontSize: 12, color: "var(--text-dim)" }}>
          Descriptions here are written for <strong>visit summaries</strong> (people and vehicles), one at a time, so
          a summary may arrive up to a couple of minutes later. The describer is capped to part of the CPU and
          gives way to recording.
        </div>
      )}

      {st.models.map((m) => (
        <ModelRow
          key={m.name}
          m={m}
          ready={st.state === "ready"}
          recommended={st.recommended === m.name}
          inUse={st.in_use === m.name}
          disabled={!!busy || running}
          busy={busy}
          onDownload={() => act(`dl:${m.name}`, () => downloadDescriberModel(m.name))}
          onTest={() => act(`test:${m.name}`, () => testDescriberModel(m.name))}
          onDelete={() => act(`rm:${m.name}`, () => deleteDescriberModel(m.name))}
          onUse={() => act(`use:${m.name}`, async () => onSettings(await switchToLocalDescriber(m.name)))}
        />
      ))}

      <div style={{ fontSize: 12, color: "var(--text-dim)" }}>
        Models come from the Ollama library and stay on this machine; pictures never leave it. Licence: {st.license}.
      </div>
      {err && <div className="error-text">{err}</div>}
    </div>
  );
}

function ModelRow({
  m,
  ready,
  recommended,
  inUse,
  disabled,
  busy,
  onDownload,
  onTest,
  onDelete,
  onUse,
}: {
  m: DescriberModel;
  ready: boolean;
  recommended: boolean;
  inUse: boolean;
  disabled: boolean;
  busy: string | null;
  onDownload: () => void;
  onTest: () => void;
  onDelete: () => void;
  onUse: () => void;
}) {
  const job = m.job;
  const usable = ready && m.fit !== "no";
  return (
    <div
      style={{
        padding: "8px 10px",
        borderRadius: 6,
        border: `1px solid ${inUse ? "var(--success)" : "var(--border)"}`,
        opacity: usable || m.installed ? 1 : 0.6,
      }}
    >
      <div style={{ display: "flex", gap: 10, alignItems: "flex-start", flexWrap: "wrap" }}>
        <div style={{ flex: "1 1 240px" }}>
          <span style={{ fontWeight: 600 }}>{m.label}</span>
          <span style={{ color: "var(--text-dim)", fontSize: 12 }}> · {m.name} · {m.size_gb} GB</span>
          {recommended && !inUse && <span style={{ color: "var(--accent, #58a6ff)", fontSize: 12 }}> · recommended here</span>}
          {inUse && <span style={{ color: "var(--success)", fontSize: 12 }}> · in use</span>}
          <div style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 2 }}>{m.about}</div>
          <div style={{ fontSize: 12, marginTop: 2, color: FIT_COLOUR[m.fit] }}>
            {m.fit === "no" ? "Not for this machine: " : ""}
            {m.fit_note}
          </div>
        </div>
        {ready && (
          <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
            {!m.installed && m.fit !== "no" && (
              <button className="btn btn-sm" disabled={disabled} onClick={onDownload}>
                Download ({m.size_gb} GB)
              </button>
            )}
            {m.installed && (
              <button className="btn btn-sm" disabled={disabled} onClick={onTest}>
                Test on my camera
              </button>
            )}
            {m.installed && !inUse && m.result?.ok && (
              <button className="btn btn-sm btn-primary" disabled={disabled} onClick={onUse}>
                {busy === `use:${m.name}` ? "Switching…" : "Use"}
              </button>
            )}
            {m.installed && !inUse && (
              <button className="btn btn-sm" disabled={disabled} onClick={onDelete} title="Free the disk space (can be downloaded again)">
                Remove
              </button>
            )}
          </div>
        )}
      </div>

      {job?.status === "running" && (
        <div style={{ marginTop: 6, fontSize: 12 }}>
          {job.kind === "download" ? `Downloading… ${Math.round(job.progress * 100)}%` : job.message || "Testing…"}
          <div style={{ height: 4, borderRadius: 2, background: "var(--bg)", marginTop: 4, overflow: "hidden" }}>
            <div style={{ height: "100%", width: `${Math.max(3, job.progress * 100)}%`, background: "var(--accent, #58a6ff)" }} />
          </div>
        </div>
      )}
      {job?.status === "failed" && (
        <div style={{ marginTop: 6, fontSize: 12, color: "var(--danger)" }}>
          {job.kind === "download" ? "Download" : "Test"} failed: {job.message}
        </div>
      )}
      {m.result && job?.status !== "running" && (
        <div style={{ marginTop: 6, fontSize: 12 }}>
          {m.result.ok ? (
            <>
              <strong>{m.result.seconds} s</strong> per description
              <span style={{ color: "var(--text-dim)" }}>
                {" "}
                (first one after a pause: {m.result.first_seconds} s, while the model loads) · {m.result.picture}:
              </span>
              <div style={{ fontStyle: "italic", marginTop: 2 }}>“{m.result.text}”</div>
            </>
          ) : (
            <span style={{ color: "var(--danger)" }}>Test failed: {m.result.error}</span>
          )}
        </div>
      )}
      {m.installed && !m.result && !job && (
        <div style={{ marginTop: 6, fontSize: 12, color: "var(--text-dim)" }}>
          Downloaded - press “Test on my camera” to see how long it takes here, then “Use”.
        </div>
      )}
    </div>
  );
}
