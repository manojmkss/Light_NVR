import { useEffect, useState } from "react";
import { getAIClasses, getAISettings, testAIBackend, updateAISettings } from "../api/ai";
import type { AISettings, AITestResult } from "../api/types";

const MODELS = ["yolov8n", "yolov8s", "yolov8m", "yolo11n", "yolo11s", "yolo11m"];

export function AISettingsTab() {
  const [settings, setSettings] = useState<AISettings | null>(null);
  const [suggested, setSuggested] = useState<string[]>([]);
  const [remoteKey, setRemoteKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<AITestResult | null>(null);

  useEffect(() => {
    getAISettings().then(setSettings).catch((e) => setError((e as Error).message));
    getAIClasses()
      .then((c) => setSuggested(c.suggested))
      .catch(() => setSuggested([]));
  }, []);

  if (error && !settings) return <div className="error-text">{error}</div>;
  if (!settings) return <p style={{ color: "var(--text-dim)" }}>Loading...</p>;

  const update = <K extends keyof AISettings>(key: K, value: AISettings[K]) => {
    setSettings((s) => (s ? { ...s, [key]: value } : s));
    setSaved(false);
  };

  const toggleClass = (cls: string) => {
    const has = settings.detection_classes.includes(cls);
    update(
      "detection_classes",
      has ? settings.detection_classes.filter((c) => c !== cls) : [...settings.detection_classes, cls],
    );
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const result = await updateAISettings({
        enabled: settings.enabled,
        backend: settings.backend,
        remote_url: settings.remote_url,
        // Only send the key when the user actually typed one, so saving an
        // unrelated field never wipes the stored secret.
        ...(remoteKey ? { remote_api_key: remoteKey } : {}),
        detection_enabled: settings.detection_enabled,
        detection_model: settings.detection_model,
        detection_confidence: settings.detection_confidence,
        detection_classes: settings.detection_classes,
        alert_on_objects_only: settings.alert_on_objects_only,
        detection_retention_days: settings.detection_retention_days,
      });
      setSettings(result);
      setRemoteKey("");
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const runTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      setTestResult(await testAIBackend());
    } catch (e) {
      setTestResult({ success: false, message: (e as Error).message });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div style={{ display: "grid", gap: 20, maxWidth: 640 }}>
      {/* ── Master switch ─────────────────────────────────────────────── */}
      <div className="card">
        <h3 style={{ marginTop: 0 }}>AI detection</h3>
        <p style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 0 }}>
          Motion detection alone can't tell a person from a swaying tree, a shadow, or rain — which is
          why motion-only alerts get ignored. With AI on, each motion event is checked for real
          objects, so an alert means <em>“a person was there”</em>, not <em>“some pixels changed”</em>.
        </p>
        <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input
            type="checkbox"
            checked={settings.enabled}
            onChange={(e) => update("enabled", e.target.checked)}
          />
          <span>Enable AI</span>
        </label>
        <p style={{ color: "var(--text-dim)", fontSize: 12, marginTop: 6, marginBottom: 0 }}>
          Off by default and completely inert when off — no model is loaded and no extra CPU is used.
        </p>
      </div>

      {settings.enabled && (
        <>
          {/* ── Where inference runs ──────────────────────────────────── */}
          <div className="card">
            <h3 style={{ marginTop: 0 }}>Where inference runs</h3>
            <div className="field">
              <label>Backend</label>
              <select
                value={settings.backend}
                onChange={(e) => update("backend", e.target.value as AISettings["backend"])}
              >
                <option value="local">Local — this machine's CPU</option>
                <option value="remote">Remote — another PC (e.g. with a GPU)</option>
              </select>
            </div>

            {settings.backend === "local" ? (
              <p style={{ color: "var(--text-dim)", fontSize: 12 }}>
                Runs in the NVR process on CPU. Only ever runs on a motion event (never every frame),
                which is what keeps it viable on a mini-PC or Pi. Needs the model downloaded once:
                <br />
                <code>./scripts/fetch-ai-models.sh {settings.detection_model}</code>
              </p>
            ) : (
              <>
                <div className="field">
                  <label>AI worker URL</label>
                  <input
                    type="text"
                    placeholder="http://192.168.1.20:811"
                    value={settings.remote_url}
                    onChange={(e) => update("remote_url", e.target.value)}
                  />
                </div>
                <div className="field">
                  <label>API key {settings.has_remote_api_key && <span style={{ color: "var(--text-dim)" }}>(stored — leave blank to keep)</span>}</label>
                  <input
                    type="password"
                    placeholder={settings.has_remote_api_key ? "••••••••" : "optional"}
                    value={remoteKey}
                    onChange={(e) => setRemoteKey(e.target.value)}
                  />
                </div>
                <p style={{ color: "var(--text-dim)", fontSize: 12 }}>
                  Sends the motion frame to an <code>ai-worker</code> on another machine. The NVR box
                  then does almost no AI work — best if you have a GPU elsewhere on the LAN. Frames
                  stay on your own network; nothing goes to the internet.
                </p>
              </>
            )}

            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <button className="btn btn-sm" onClick={runTest} disabled={testing}>
                {testing ? "Testing..." : "Test backend"}
              </button>
              {testResult && (
                <span
                  style={{
                    fontSize: 13,
                    color: testResult.success ? "var(--ok, #3fb950)" : "var(--danger, #f85149)",
                  }}
                >
                  {testResult.success ? "✓" : "✗"} {testResult.message}
                </span>
              )}
            </div>
          </div>

          {/* ── Tier 1: object detection ──────────────────────────────── */}
          <div className="card">
            <h3 style={{ marginTop: 0 }}>Objects</h3>

            <div className="field">
              <label>Model</label>
              <select
                value={settings.detection_model}
                onChange={(e) => update("detection_model", e.target.value)}
              >
                {MODELS.map((m) => (
                  <option key={m} value={m}>
                    {m}
                    {m.endsWith("n") ? " (fastest — recommended for CPU)" : m.endsWith("m") ? " (most accurate — GPU)" : ""}
                  </option>
                ))}
              </select>
            </div>

            <div className="field">
              <label>Confidence threshold: {settings.detection_confidence}%</label>
              <input
                type="range"
                min={1}
                max={99}
                value={settings.detection_confidence}
                onChange={(e) => update("detection_confidence", Number(e.target.value))}
              />
              <p style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 4 }}>
                Lower catches more but invents things; higher is stricter but misses distant/partial
                objects. 50% is a sane default.
              </p>
            </div>

            <div className="field">
              <label>Alert me about these</label>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 4 }}>
                {suggested.map((cls) => {
                  const on = settings.detection_classes.includes(cls);
                  return (
                    <button
                      type="button"
                      key={cls}
                      onClick={() => toggleClass(cls)}
                      className={`btn btn-sm ${on ? "btn-primary" : ""}`}
                      style={{ textTransform: "capitalize" }}
                    >
                      {on ? "✓ " : ""}
                      {cls}
                    </button>
                  );
                })}
              </div>
              {settings.detection_classes.length === 0 && (
                <p style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 6 }}>
                  Nothing selected — every object the model knows will be kept.
                </p>
              )}
            </div>

            <div className="field">
              <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <input
                  type="checkbox"
                  checked={settings.alert_on_objects_only}
                  onChange={(e) => update("alert_on_objects_only", e.target.checked)}
                />
                <span>Only alert when one of these objects is present</span>
              </label>
              <p style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 4 }}>
                <strong>This is the setting that kills false alarms.</strong> Motion with no object of
                interest raises no alert at all. Recording is unaffected — it still starts the instant
                motion is seen, so you never lose footage.
              </p>
            </div>

            <div className="field">
              <label>Keep detection history for (days)</label>
              <input
                type="number"
                min={1}
                max={3650}
                value={settings.detection_retention_days}
                onChange={(e) => update("detection_retention_days", Number(e.target.value))}
                style={{ maxWidth: 120 }}
              />
            </div>
          </div>

          {error && <div className="error-text">{error}</div>}
        </>
      )}

      <div>
        <button className="btn btn-primary" onClick={save} disabled={saving}>
          {saving ? "Saving..." : saved ? "Saved" : "Save"}
        </button>
      </div>
    </div>
  );
}
