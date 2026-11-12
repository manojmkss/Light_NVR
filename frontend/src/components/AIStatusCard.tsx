import { useEffect, useState } from "react";
import { getAIEngine, getDetectionModels, type DetectionModels, type EngineStatus } from "../api/ai";
import type { AISettings } from "../api/types";
import { DEVICE_LABEL, engineInUse } from "./aiSetup";

interface Health {
  ok: boolean;
  headline: string;
  detail: string;
}

/** One line on whether AI detection actually works right now - and, when it
 *  doesn't, the single cause and one button that fixes it - instead of
 *  leaving people to piece it together from several cards. */
function assess(s: AISettings, st: EngineStatus | null, models: DetectionModels | null, err: string | null): Health {
  if (err || (st && !st.reachable)) {
    return {
      ok: false,
      headline: "Not working: the AI service isn't answering.",
      detail: st?.hints[0] ?? st?.error ?? err ?? "Motion is still recorded and alerted, just not checked for objects.",
    };
  }
  const eng = engineInUse(st);
  if (eng && eng.model_ready === false) {
    return {
      ok: false,
      headline: "Not working: the detection model isn't downloaded.",
      detail: "No pictures are being checked. Recording and motion alerts are fine.",
    };
  }
  if (!s.detection_classes.length) {
    return { ok: false, headline: "Nothing to look for is selected.", detail: "Pick at least one thing under \"Alert me when\"." };
  }
  const device = eng ? eng.devices.find((d) => d.id === eng.active) : undefined;
  const model = models?.models.find((m) => m.name === (models?.in_use || s.detection_model));
  const speed = model?.result?.ok && model.result.p50_ms ? ` · about ${model.result.p50_ms} ms per check` : "";
  return {
    ok: true,
    headline: "Smart detection is working",
    detail: `${model?.label ?? s.detection_model} model on the ${device?.name ?? (eng ? DEVICE_LABEL[eng.active] : "AI service")}${speed}`,
  };
}

export function AIStatusCard({
  settings,
  refreshKey,
  onSetup,
  onTurnOff,
  busy,
}: {
  settings: AISettings;
  refreshKey: number;
  onSetup: () => void;
  onTurnOff: () => void;
  busy: boolean;
}) {
  const [st, setSt] = useState<EngineStatus | null>(null);
  const [models, setModels] = useState<DetectionModels | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let live = true;
    setLoading(true);
    Promise.all([getAIEngine(), getDetectionModels().catch(() => null)])
      .then(([e, m]) => {
        if (!live) return;
        setSt(e);
        setModels(m);
        setErr(null);
      })
      .catch((e) => live && setErr((e as Error).message))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [refreshKey, settings.remote_url, settings.detection_model]);

  const h = loading ? null : assess(settings, st, models, err);

  return (
    <div className="card" style={{ display: "grid", gap: 14 }}>
      <div style={{ display: "flex", gap: 12, alignItems: "flex-start", justifyContent: "space-between", flexWrap: "wrap" }}>
        <div style={{ flex: "1 1 320px" }}>
          <strong style={{ fontSize: 17 }}>Smart detection</strong>
          <div style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 2 }}>
            Tells a real person or car apart from trees, rain and headlights, so an alert means something was really there.
            Recording is never affected.
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button className="btn btn-sm" onClick={onSetup} disabled={busy}>
            Run setup again
          </button>
          <button className="btn btn-sm" onClick={onTurnOff} disabled={busy}>
            Turn off
          </button>
        </div>
      </div>

      {loading && <div style={{ fontSize: 13, color: "var(--text-dim)" }}>Checking…</div>}

      {h && (
        <div
          role={h.ok ? "status" : "alert"}
          style={{
            display: "flex",
            gap: 12,
            alignItems: "center",
            justifyContent: "space-between",
            flexWrap: "wrap",
            padding: "12px 14px",
            borderRadius: 8,
            background: h.ok ? "rgba(34,197,94,0.08)" : "rgba(239,68,68,0.08)",
            border: `1px solid ${h.ok ? "rgba(34,197,94,0.5)" : "rgba(239,68,68,0.6)"}`,
          }}
        >
          <span style={{ display: "flex", gap: 10, alignItems: "flex-start", flex: "1 1 300px" }}>
            <span
              aria-hidden="true"
              style={{ width: 10, height: 10, borderRadius: 5, marginTop: 6, flex: "none", background: h.ok ? "var(--success)" : "var(--danger)" }}
            />
            <span>
              <strong style={{ color: h.ok ? "#4ade80" : "#fca5a5" }}>{h.headline}</strong>
              <div style={{ fontSize: 13, color: "var(--text-dim)", marginTop: 2 }}>{h.detail}</div>
            </span>
          </span>
          {!h.ok && (
            <button className="btn btn-sm btn-primary" onClick={onSetup} disabled={busy}>
              Fix it
            </button>
          )}
        </div>
      )}
    </div>
  );
}
