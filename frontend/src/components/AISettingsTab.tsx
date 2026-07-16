import { useEffect, useState } from "react";
import {
  getAIClasses,
  getAISettings,
  listOllamaModels,
  testAIBackend,
  testVlm,
  updateAISettings,
} from "../api/ai";
import type { AISettings, AITestResult } from "../api/types";

const MODELS = ["yolov8n", "yolov8s", "yolov8m", "yolo11n", "yolo11s", "yolo11m"];

/** The one-time model download command, for the OS the user is most likely on.
 *
 *  This is a guess from the *browser's* platform, not the server's - the NVR
 *  itself always runs in a Linux container, so it can't tell us what the host
 *  is. Someone administering a Linux NVR from a Windows laptop gets the wrong
 *  one, which is why both are always shown rather than only the guess. */
function modelCommand(model: string): { primary: string; other: string; otherLabel: string } {
  const ua = typeof navigator !== "undefined" ? navigator.userAgent : "";
  const isWindows = /Windows/i.test(ua);
  const ps = `.\\scripts\\fetch-ai-models.ps1 -Model ${model}`;
  const sh = `./scripts/fetch-ai-models.sh ${model}`;
  return isWindows
    ? { primary: ps, other: sh, otherLabel: "On Linux / macOS instead" }
    : { primary: sh, other: ps, otherLabel: "On Windows (PowerShell) instead" };
}

/** Small inline result line for the various Test buttons. */
function TestLine({ result }: { result: AITestResult | null }) {
  if (!result) return null;
  return (
    <span
      style={{
        fontSize: 13,
        color: result.success ? "var(--ok, #3fb950)" : "var(--danger, #f85149)",
      }}
    >
      {result.success ? "✓" : "✗"} {result.message}
    </span>
  );
}

/** A titled section that only shows its body once its own switch is on -
 *  the AI tab covers four features and showing all of them at once was a wall
 *  of controls that made a simple "stop alerting me about trees" look hard. */
function Section({
  title,
  blurb,
  on,
  onToggle,
  children,
}: {
  title: string;
  blurb: string;
  on: boolean;
  onToggle: (v: boolean) => void;
  children?: React.ReactNode;
}) {
  return (
    <div className="card">
      <label style={{ display: "flex", gap: 10, alignItems: "flex-start", cursor: "pointer" }}>
        <input type="checkbox" checked={on} onChange={(e) => onToggle(e.target.checked)} style={{ marginTop: 4 }} />
        <span>
          <strong>{title}</strong>
          <div style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 2 }}>{blurb}</div>
        </span>
      </label>
      {on && <div style={{ marginTop: 16 }}>{children}</div>}
    </div>
  );
}

export function AISettingsTab() {
  const [s, setS] = useState<AISettings | null>(null);
  const [suggested, setSuggested] = useState<string[]>([]);
  const [remoteKey, setRemoteKey] = useState("");
  const [vlmKey, setVlmKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<AITestResult | null>(null);
  const [vlmTesting, setVlmTesting] = useState(false);
  const [vlmResult, setVlmResult] = useState<AITestResult | null>(null);
  const [ollamaModels, setOllamaModels] = useState<string[] | null>(null);
  const [ollamaLoading, setOllamaLoading] = useState(false);
  const [ollamaError, setOllamaError] = useState<string | null>(null);

  useEffect(() => {
    getAISettings().then(setS).catch((e) => setError((e as Error).message));
    getAIClasses().then((c) => setSuggested(c.suggested)).catch(() => setSuggested([]));
  }, []);

  if (error && !s) return <div className="error-text">{error}</div>;
  if (!s) return <p style={{ color: "var(--text-dim)" }}>Loading...</p>;

  const up = <K extends keyof AISettings>(k: K, v: AISettings[K]) => {
    setS((p) => (p ? { ...p, [k]: v } : p));
    setSaved(false);
  };

  const toggleClass = (cls: string) =>
    up(
      "detection_classes",
      s.detection_classes.includes(cls)
        ? s.detection_classes.filter((c) => c !== cls)
        : [...s.detection_classes, cls],
    );

  const loadOllama = async () => {
    setOllamaLoading(true);
    setOllamaError(null);
    setOllamaModels(null);
    try {
      const r = await listOllamaModels(s.vlm_url);
      // Vision models first: picking a text-only model is the most likely way
      // to misconfigure this, and it fails silently at description time.
      setOllamaModels([...r.vision_models, ...r.models.filter((m) => !r.vision_models.includes(m))]);
      if (r.vision_models.length && !s.vlm_model) up("vlm_model", r.vision_models[0]);
    } catch (e) {
      setOllamaError((e as Error).message);
    } finally {
      setOllamaLoading(false);
    }
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      // Every field is sent, not just the Tier 1 ones - an earlier version
      // omitted the description/recognition fields, so editing them and
      // hitting Save silently discarded the change.
      const result = await updateAISettings({
        enabled: s.enabled,
        backend: s.backend,
        remote_url: s.remote_url,
        ...(remoteKey ? { remote_api_key: remoteKey } : {}),
        detection_enabled: s.detection_enabled,
        detection_model: s.detection_model,
        detection_confidence: s.detection_confidence,
        detection_classes: s.detection_classes,
        alert_on_objects_only: s.alert_on_objects_only,
        detection_retention_days: s.detection_retention_days,
        vlm_enabled: s.vlm_enabled,
        vlm_provider: s.vlm_provider,
        vlm_url: s.vlm_url,
        vlm_model: s.vlm_model,
        ...(vlmKey ? { vlm_api_key: vlmKey } : {}),
        vlm_daily_digest: s.vlm_daily_digest,
        privacy_ack: s.privacy_ack,
        face_enabled: s.face_enabled,
        face_threshold: s.face_threshold,
        alpr_enabled: s.alpr_enabled,
      });
      setS(result);
      setRemoteKey("");
      setVlmKey("");
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

  const runVlmTest = async () => {
    setVlmTesting(true);
    setVlmResult(null);
    try {
      setVlmResult(await testVlm());
    } catch (e) {
      setVlmResult({ success: false, message: (e as Error).message });
    } finally {
      setVlmTesting(false);
    }
  };

  return (
    <div style={{ display: "grid", gap: 16, maxWidth: 660 }}>
      {/* ── Master switch ──────────────────────────────────────────────── */}
      <div className="card">
        <label style={{ display: "flex", gap: 10, alignItems: "flex-start", cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={s.enabled}
            onChange={(e) => up("enabled", e.target.checked)}
            style={{ marginTop: 4 }}
          />
          <span>
            <strong>Use AI</strong>
            <div style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 2 }}>
              Motion alone can't tell a person from a swaying tree or passing headlights — which is why
              motion alerts get ignored. AI checks each motion event for real objects, so an alert means
              something was actually there. Off by default; nothing runs while it's off.
            </div>
          </span>
        </label>
      </div>

      {s.enabled && (
        <>
          {/* ── Where it runs ────────────────────────────────────────── */}
          <div className="card">
            <strong>Where it runs</strong>
            <div className="field" style={{ marginTop: 10 }}>
              <select value={s.backend} onChange={(e) => up("backend", e.target.value as AISettings["backend"])}>
                <option value="local">This machine (CPU) — simplest</option>
                <option value="remote">Another PC with a GPU — faster</option>
              </select>
            </div>

            {s.backend === "local" ? (
              <div style={{ color: "var(--text-dim)", fontSize: 12, marginTop: 0 }}>
                <p style={{ marginTop: 0 }}>
                  Runs here on the CPU, only when motion happens (never on every frame) — that's what
                  keeps it light. One-time setup: run this <strong>on the machine hosting LightNVR</strong>{" "}
                  to download the model, then press “Check it works”.
                </p>
                <code style={{ display: "block", padding: "6px 8px", background: "var(--bg-alt, #0d1117)", borderRadius: 4 }}>
                  {modelCommand(s.detection_model).primary}
                </code>
                <details style={{ marginTop: 6 }}>
                  <summary style={{ cursor: "pointer" }}>{modelCommand(s.detection_model).otherLabel}</summary>
                  <code style={{ display: "block", padding: "6px 8px", marginTop: 4, background: "var(--bg-alt, #0d1117)", borderRadius: 4 }}>
                    {modelCommand(s.detection_model).other}
                  </code>
                </details>
              </div>
            ) : (
              <>
                <div className="field">
                  <label>Address of the AI worker</label>
                  <input
                    type="text"
                    placeholder="http://192.168.1.20:8811"
                    value={s.remote_url}
                    onChange={(e) => up("remote_url", e.target.value)}
                  />
                </div>
                <div className="field">
                  <label>
                    Password{" "}
                    {s.has_remote_api_key && (
                      <span style={{ color: "var(--text-dim)" }}>(saved — leave blank to keep)</span>
                    )}
                  </label>
                  <input
                    type="password"
                    placeholder={s.has_remote_api_key ? "••••••••" : "optional"}
                    value={remoteKey}
                    onChange={(e) => setRemoteKey(e.target.value)}
                  />
                </div>
                <p style={{ color: "var(--text-dim)", fontSize: 12 }}>
                  Video frames go to that PC on your own network — nothing leaves your home.
                </p>
              </>
            )}

            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <button className="btn btn-sm" onClick={runTest} disabled={testing}>
                {testing ? "Checking..." : "Check it works"}
              </button>
              <TestLine result={testResult} />
            </div>
          </div>

          {/* ── Tier 1: what to look for ─────────────────────────────── */}
          <div className="card">
            <strong>What to look for</strong>
            <div className="field" style={{ marginTop: 10 }}>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {suggested.map((cls) => {
                  const on = s.detection_classes.includes(cls);
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
            </div>

            <div className="field">
              <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <input
                  type="checkbox"
                  checked={s.alert_on_objects_only}
                  onChange={(e) => up("alert_on_objects_only", e.target.checked)}
                />
                <span>Only alert me when one of these is seen</span>
              </label>
              <p style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 4 }}>
                <strong>This is the setting that stops false alarms.</strong> Recording is never affected —
                it still starts the moment motion is seen, so you never lose footage.
              </p>
            </div>

            <details>
              <summary style={{ cursor: "pointer", fontSize: 13, color: "var(--text-dim)" }}>
                Advanced
              </summary>
              <div style={{ marginTop: 12 }}>
                <div className="field">
                  <label>Model</label>
                  <select value={s.detection_model} onChange={(e) => up("detection_model", e.target.value)}>
                    {MODELS.map((m) => (
                      <option key={m} value={m}>
                        {m}
                        {m.endsWith("n") ? " — fastest (best for CPU)" : m.endsWith("m") ? " — most accurate (GPU)" : ""}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label>How sure it must be: {s.detection_confidence}%</label>
                  <input
                    type="range"
                    min={1}
                    max={99}
                    value={s.detection_confidence}
                    onChange={(e) => up("detection_confidence", Number(e.target.value))}
                  />
                  <p style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 4 }}>
                    Lower catches more but imagines things; higher misses distant or partly hidden objects.
                  </p>
                </div>
                <div className="field">
                  <label>Keep detection history for (days)</label>
                  <input
                    type="number"
                    min={1}
                    max={3650}
                    value={s.detection_retention_days}
                    onChange={(e) => up("detection_retention_days", Number(e.target.value))}
                    style={{ maxWidth: 120 }}
                  />
                </div>
              </div>
            </details>
          </div>

          {/* ── Tier 3: descriptions ─────────────────────────────────── */}
          <Section
            title="Describe what's happening (optional)"
            blurb='Instead of "Motion on Front Door", get "A delivery person left a package at the front door". Needs a vision model — Ollama on your own PC is free and keeps everything at home.'
            on={s.vlm_enabled}
            onToggle={(v) => up("vlm_enabled", v)}
          >
            <div className="field">
              <label>Vision model provider</label>
              <select
                value={s.vlm_provider}
                onChange={(e) => {
                  up("vlm_provider", e.target.value as AISettings["vlm_provider"]);
                  setOllamaModels(null);
                }}
              >
                <option value="ollama">Ollama — free, runs on your own PC</option>
                <option value="openai_compatible">LM Studio / vLLM / OpenAI</option>
                <option value="anthropic">Claude (Anthropic)</option>
              </select>
            </div>

            {s.vlm_provider === "ollama" && (
              <>
                <div className="field">
                  <label>Ollama address</label>
                  <div className="form-row">
                    <input
                      type="text"
                      style={{ flex: 1 }}
                      placeholder="http://192.168.1.20:11434"
                      value={s.vlm_url}
                      onChange={(e) => up("vlm_url", e.target.value)}
                    />
                    <button className="btn btn-sm" onClick={loadOllama} disabled={!s.vlm_url || ollamaLoading}>
                      {ollamaLoading ? "Loading..." : "Find models"}
                    </button>
                  </div>
                  <p style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 4 }}>
                    The PC running Ollama. Port is usually 11434. It must be started with{" "}
                    <code>OLLAMA_HOST=0.0.0.0</code> so this NVR can reach it over the network.
                  </p>
                  {ollamaError && <div className="error-text">{ollamaError}</div>}
                </div>

                <div className="field">
                  <label>Model</label>
                  {ollamaModels && ollamaModels.length > 0 ? (
                    <select value={s.vlm_model} onChange={(e) => up("vlm_model", e.target.value)}>
                      <option value="">Select a model…</option>
                      {ollamaModels.map((m) => (
                        <option key={m} value={m}>
                          {m}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input
                      type="text"
                      placeholder="llama3.2-vision"
                      value={s.vlm_model}
                      onChange={(e) => up("vlm_model", e.target.value)}
                    />
                  )}
                  <p style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 4 }}>
                    Must be a model that can see images — e.g. <code>llama3.2-vision</code>,{" "}
                    <code>llava</code>, <code>minicpm-v</code>, <code>moondream</code>. On the Ollama PC:{" "}
                    <code>ollama pull llama3.2-vision</code>
                  </p>
                </div>
              </>
            )}

            {s.vlm_provider !== "ollama" && (
              <>
                <div className="field">
                  <label>{s.vlm_provider === "anthropic" ? "API URL (leave blank for Anthropic's own)" : "Endpoint URL"}</label>
                  <input
                    type="text"
                    placeholder={
                      s.vlm_provider === "anthropic" ? "https://api.anthropic.com" : "http://192.168.1.20:1234/v1"
                    }
                    value={s.vlm_url}
                    onChange={(e) => up("vlm_url", e.target.value)}
                  />
                </div>
                <div className="field">
                  <label>Model</label>
                  <input
                    type="text"
                    placeholder={s.vlm_provider === "anthropic" ? "claude-haiku-4-5-20251001" : "model name"}
                    value={s.vlm_model}
                    onChange={(e) => up("vlm_model", e.target.value)}
                  />
                </div>
                <div className="field">
                  <label>
                    API key{" "}
                    {s.has_vlm_api_key && <span style={{ color: "var(--text-dim)" }}>(saved — leave blank to keep)</span>}
                  </label>
                  <input
                    type="password"
                    placeholder={s.has_vlm_api_key ? "••••••••" : ""}
                    value={vlmKey}
                    onChange={(e) => setVlmKey(e.target.value)}
                  />
                </div>
                <p style={{ fontSize: 12, color: "var(--danger, #f85149)" }}>
                  Heads up: this sends camera snapshots to an outside company. Only snapshots that already
                  contain a detected object are sent — never your recordings — but if that's not OK, use
                  Ollama instead and nothing leaves your home.
                </p>
              </>
            )}

            <div className="field">
              <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <input
                  type="checkbox"
                  checked={s.vlm_daily_digest}
                  onChange={(e) => up("vlm_daily_digest", e.target.checked)}
                />
                <span>Send me a daily summary at 8pm</span>
              </label>
              <p style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 4 }}>
                One roundup of the day's activity, e.g. “Front Door: 3x person, 1x car”. Nothing is sent on
                a quiet day.
              </p>
            </div>

            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <button className="btn btn-sm" onClick={runVlmTest} disabled={vlmTesting}>
                {vlmTesting ? "Asking the model..." : "Test description"}
              </button>
              <TestLine result={vlmResult} />
            </div>
            <p style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 6, marginBottom: 0 }}>
              Save your changes before testing — the test uses the saved settings.
            </p>
          </Section>

          {/* ── Tier 4: recognition ──────────────────────────────────── */}
          <div className="card">
            <strong>Recognise faces and number plates</strong>
            <div style={{ color: "var(--text-dim)", fontSize: 13, marginTop: 2 }}>
              Tell known people apart from strangers, and read vehicle number plates.
            </div>

            <div
              style={{
                border: "1px solid var(--danger, #f85149)",
                borderRadius: 6,
                padding: 12,
                marginTop: 12,
                fontSize: 12,
              }}
            >
              <strong style={{ color: "var(--danger, #f85149)" }}>Please read before turning this on</strong>
              <p style={{ marginTop: 6, marginBottom: 8, color: "var(--text-dim)" }}>
                This records information that identifies real people — visitors, neighbours, delivery
                staff — not just your own household. In many places (India's DPDP Act, the EU's GDPR, and
                several US states) that carries legal duties, and consent may be required. It is your
                responsibility, as the operator, to use it lawfully. Everything else in LightNVR works
                without this.
              </p>
              <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <input
                  type="checkbox"
                  checked={s.privacy_ack}
                  onChange={(e) => {
                    const ok = e.target.checked;
                    up("privacy_ack", ok);
                    // Withdrawing the acknowledgement must actually switch the
                    // features off, not just grey out the checkboxes - the
                    // server enforces this too.
                    if (!ok) {
                      up("face_enabled", false);
                      up("alpr_enabled", false);
                    }
                  }}
                />
                <span>I understand and I'm responsible for using this lawfully</span>
              </label>
            </div>

            <div style={{ marginTop: 12, opacity: s.privacy_ack ? 1 : 0.5 }}>
              <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <input
                  type="checkbox"
                  disabled={!s.privacy_ack}
                  checked={s.face_enabled}
                  onChange={(e) => up("face_enabled", e.target.checked)}
                />
                <span>Recognise faces</span>
              </label>
              {s.face_enabled && (
                <div className="field" style={{ marginTop: 8 }}>
                  <label>How close a match must be: {s.face_threshold}%</label>
                  <input
                    type="range"
                    min={1}
                    max={99}
                    value={s.face_threshold}
                    onChange={(e) => up("face_threshold", Number(e.target.value))}
                  />
                </div>
              )}
              <label style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
                <input
                  type="checkbox"
                  disabled={!s.privacy_ack}
                  checked={s.alpr_enabled}
                  onChange={(e) => up("alpr_enabled", e.target.checked)}
                />
                <span>Read number plates</span>
              </label>
              <p style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 8, marginBottom: 0 }}>
                Recognition models aren't bundled yet — turning these on records your choice, and they
                start working as soon as the models ship. Everything above works today.
              </p>
            </div>
          </div>

          {error && <div className="error-text">{error}</div>}
        </>
      )}

      <div>
        <button className="btn btn-primary" onClick={save} disabled={saving}>
          {saving ? "Saving..." : saved ? "Saved ✓" : "Save"}
        </button>
      </div>
    </div>
  );
}
