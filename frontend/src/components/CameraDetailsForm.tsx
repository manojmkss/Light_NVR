import { useState } from "react";
import type { CameraCreatePayload, Codec, RecordingMode } from "../api/types";
import { testConnection } from "../api/cameras";

// Mirrors backend motion_detector.sensitivity_to_pct: 1 -> 3%, 50 -> ~0.7%, 100 -> 0.15%.
function triggerPercent(sensitivity: number): string {
  const pct = 3.0 * Math.pow(0.05, (sensitivity - 1) / 99);
  return pct >= 1 ? pct.toFixed(1) : pct.toFixed(2);
}

interface Props {
  initial: CameraCreatePayload;
  submitLabel: string;
  submitting: boolean;
  serverError: string | null;
  onSubmit: (payload: CameraCreatePayload) => void;
  onCancel: () => void;
}

export function CameraDetailsForm({ initial, submitLabel, submitting, serverError, onSubmit, onCancel }: Props) {
  const [form, setForm] = useState<CameraCreatePayload>(initial);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);

  const update = <K extends keyof CameraCreatePayload>(key: K, value: CameraCreatePayload[K]) => {
    setForm((f) => ({ ...f, [key]: value }));
  };

  const handleTest = async () => {
    if (!form.rtsp_main_url) return;
    setTesting(true);
    setTestResult(null);
    try {
      const result = await testConnection(form.rtsp_main_url);
      if (result.success) {
        setTestResult(
          `OK — ${result.codec?.toUpperCase()} ${result.width}x${result.height} @ ${result.fps?.toFixed(0)}fps, audio: ${result.has_audio ? "yes" : "no"}`
        );
        if (result.codec === "h264" || result.codec === "h265") {
          update("codec", result.codec as Codec);
        }
        update("has_audio", Boolean(result.has_audio));
      } else {
        setTestResult(`Failed — ${result.error}`);
      }
    } catch (err) {
      setTestResult(`Failed — ${(err as Error).message}`);
    } finally {
      setTesting(false);
    }
  };

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit(form);
      }}
    >
      <div className="field">
        <label>Camera name</label>
        <input
          type="text"
          required
          value={form.name}
          onChange={(e) => update("name", e.target.value)}
        />
      </div>

      <div className="field">
        <label>Main stream RTSP URL</label>
        <input
          type="text"
          required
          value={form.rtsp_main_url}
          onChange={(e) => update("rtsp_main_url", e.target.value)}
          placeholder="rtsp://user:pass@192.168.1.50:554/stream1"
        />
      </div>

      <div className="field">
        <label>Sub stream RTSP URL (optional — used for live view & motion)</label>
        <input
          type="text"
          value={form.rtsp_sub_url ?? ""}
          onChange={(e) => update("rtsp_sub_url", e.target.value || null)}
          placeholder="rtsp://user:pass@192.168.1.50:554/stream2"
        />
      </div>

      <div className="form-row">
        <div className="field">
          <label>Username (RTSP)</label>
          <input type="text" value={form.username ?? ""} onChange={(e) => update("username", e.target.value)} />
        </div>
        <div className="field">
          <label>Password (RTSP)</label>
          <input
            type="password"
            value={form.password ?? ""}
            onChange={(e) => update("password", e.target.value)}
          />
        </div>
      </div>

      <div className="field">
        <button type="button" className="btn btn-sm" onClick={handleTest} disabled={testing || !form.rtsp_main_url}>
          {testing ? "Testing..." : "Test connection"}
        </button>
        {testResult && <div style={{ marginTop: 8, fontSize: 13 }}>{testResult}</div>}
      </div>

      <div className="form-row">
        <div className="field">
          <label>Codec</label>
          <select value={form.codec} onChange={(e) => update("codec", e.target.value as Codec)}>
            <option value="h264">H.264</option>
            <option value="h265">H.265</option>
          </select>
        </div>
        <div className="field checkbox-field" style={{ alignSelf: "center", marginTop: 18 }}>
          <input
            type="checkbox"
            id="has_audio"
            checked={Boolean(form.has_audio)}
            onChange={(e) => update("has_audio", e.target.checked)}
          />
          <label htmlFor="has_audio">Has audio</label>
        </div>
      </div>

      <div className="field">
        <label>Recording mode</label>
        <select value={form.recording_mode} onChange={(e) => update("recording_mode", e.target.value as RecordingMode)}>
          <option value="continuous">Continuous</option>
          <option value="motion">Motion-triggered</option>
          <option value="off">Off (live view only)</option>
        </select>
      </div>

      <div className="field checkbox-field">
        <input
          type="checkbox"
          id="motion_enabled"
          checked={Boolean(form.motion_enabled) || form.recording_mode === "motion"}
          disabled={form.recording_mode === "motion"}
          onChange={(e) => update("motion_enabled", e.target.checked)}
        />
        <label htmlFor="motion_enabled">
          Detect motion (for alerts/tagging)
          {form.recording_mode === "motion" && <span className="muted"> - always on in motion-triggered mode</span>}
        </label>
      </div>

      {(form.motion_enabled || form.recording_mode === "motion") && (
        <div className="field">
          <label>Motion sensitivity ({form.motion_sensitivity ?? 50})</label>
          <input
            type="range"
            min={1}
            max={100}
            value={form.motion_sensitivity ?? 50}
            onChange={(e) => update("motion_sensitivity", Number(e.target.value))}
          />
          <div className="muted">
            Triggers when about {triggerPercent(form.motion_sensitivity ?? 50)}% of the scene (or of your motion
            zones) changes for at least half a second. Higher = smaller movements count.
          </div>
        </div>
      )}

      {form.recording_mode === "motion" && (
        <div className="field">
          <label>Clip padding (seconds)</label>
          <div style={{ display: "flex", gap: 12 }}>
            <label style={{ flex: 1 }}>
              Before motion
              <input
                type="number"
                min={0}
                max={30}
                value={form.pre_roll_seconds ?? 5}
                onChange={(e) => update("pre_roll_seconds", Math.min(30, Math.max(0, Number(e.target.value) || 0)))}
              />
            </label>
            <label style={{ flex: 1 }}>
              After motion ends
              <input
                type="number"
                min={0}
                max={120}
                value={form.post_roll_seconds ?? 10}
                onChange={(e) => update("post_roll_seconds", Math.min(120, Math.max(0, Number(e.target.value) || 0)))}
              />
            </label>
          </div>
          <div className="muted">
            The last few seconds are always kept in a rolling buffer, so each clip starts this long before motion was
            detected. Motion again within the "after" window continues the same clip.
          </div>
        </div>
      )}

      <div className="field">
        <label>Area (optional)</label>
        <input
          type="text"
          maxLength={64}
          placeholder="e.g. Gate, Driveway, Back yard"
          value={form.area ?? ""}
          onChange={(e) => update("area", e.target.value || null)}
        />
        <div className="muted">
          Which part of the property this camera watches. Events on cameras in the same area — or in areas you link
          with a walking time (Settings → Modes → Areas) — are joined into one visit, with one summary. Leave empty and
          all cameras count as one area.
        </div>
      </div>

      <div className="field">
        <label>Retention override (days)</label>
        <input
          type="number"
          min={1}
          placeholder="Use global default"
          value={form.retention_days ?? ""}
          onChange={(e) => update("retention_days", e.target.value === "" ? null : Number(e.target.value))}
        />
      </div>

      {serverError && <div className="error-text">{serverError}</div>}

      <div className="modal-actions">
        <button type="button" className="btn" onClick={onCancel}>
          Cancel
        </button>
        <button type="submit" className="btn btn-primary" disabled={submitting}>
          {submitting ? "Saving..." : submitLabel}
        </button>
      </div>
    </form>
  );
}
