import { apiFetch, buildMediaUrl } from "./client";
import type { AISettings, AISettingsUpdate, AITestResult, Detection } from "./types";

export function getAISettings(): Promise<AISettings> {
  return apiFetch<AISettings>("/ai/settings");
}

export function updateAISettings(payload: AISettingsUpdate): Promise<AISettings> {
  return apiFetch<AISettings>("/ai/settings", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

/** Verifies the configured backend is actually loadable/reachable. Can take a
 *  few seconds on first run (the local model loads, or the remote box wakes). */
export function testAIBackend(): Promise<AITestResult> {
  return apiFetch<AITestResult>("/ai/test", { method: "POST" });
}

export function getAIClasses(): Promise<{ all: string[]; suggested: string[] }> {
  return apiFetch<{ all: string[]; suggested: string[] }>("/ai/classes");
}

/** Round-trips a real image through the configured VLM — proves the model
 *  exists and accepts images, which merely reaching the host does not. */
export function testVlm(): Promise<AITestResult> {
  return apiFetch<AITestResult>("/ai/test-vlm", { method: "POST" });
}

/** Models actually installed on an Ollama host, so the UI can offer a dropdown
 *  instead of asking the user to type a name from memory. */
export function listOllamaModels(url: string): Promise<{ models: string[]; vision_models: string[] }> {
  return apiFetch<{ models: string[]; vision_models: string[] }>(
    `/ai/ollama/models?url=${encodeURIComponent(url)}`,
  );
}

export interface FeatureLevel {
  level: string;
  reason: string;
}

export interface AIBenchmarkReport {
  cpu: { model: string; arch: string; cores: number; avx2: boolean };
  memory: { total_mb?: number; available_mb?: number; container_limit_mb?: number };
  devices: { intel_or_amd_render_node: boolean; amd_kfd: boolean; nvidia: boolean };
  onnx_providers: string[];
  worker: { reachable: boolean; device?: string; error?: string } | null;
  benchmark: { ok: boolean; p50_ms?: number; p95_ms?: number; error?: string };
  feasibility: {
    detection: FeatureLevel;
    faces?: FeatureLevel;
    search?: FeatureLevel;
    descriptions?: FeatureLevel;
    recommended_interval_ms: number | null;
    capacity_fps?: number;
    demand_fps?: number;
  };
}

export interface AIStats {
  slots: number;
  busy: number;
  queued: number;
  avg_infer_ms: number;
  active_cameras: number;
  cameras: Record<string, {
    submitted: number;
    served: number;
    superseded: number;
    expired: number;
    failed: number;
    wait_p95_ms: number | null;
    infer_p50_ms: number | null;
  }>;
}

export function getAIStats(): Promise<AIStats> {
  return apiFetch<AIStats>("/ai/stats");
}

/** Runs real detections on the configured backend (a few seconds). */
export function runAIBenchmark(): Promise<AIBenchmarkReport> {
  return apiFetch<AIBenchmarkReport>("/ai/benchmark", { method: "POST" });
}

export function detectionSnapshotUrl(detectionId: number): string {
  return buildMediaUrl(`/ai/detections/${detectionId}/snapshot.jpg`);
}

export function listDetections(params: {
  camera_id?: number;
  label?: string;
  limit?: number;
  offset?: number;
} = {}): Promise<Detection[]> {
  const q = new URLSearchParams();
  if (params.camera_id !== undefined) q.set("camera_id", String(params.camera_id));
  if (params.label) q.set("label", params.label);
  if (params.limit !== undefined) q.set("limit", String(params.limit));
  if (params.offset !== undefined) q.set("offset", String(params.offset));
  const qs = q.toString();
  return apiFetch<Detection[]>(`/ai/detections${qs ? `?${qs}` : ""}`);
}

// ── AI engine (CPU / Intel GPU / NVIDIA GPU) ─────────────────────────────────

export type EngineDevice = "cpu" | "intel_gpu" | "nvidia_gpu";
export interface EngineDeviceInfo {
  id: EngineDevice;
  label: string;
  name: string;
  available: boolean;
  reason: string | null;
  result: { ok: boolean; p50_ms?: number; error?: string; at: number } | null;
}
export interface EngineState {
  choice: "auto" | EngineDevice;
  active: EngineDevice;
  note: string;
  variant: string;
  devices: EngineDeviceInfo[];
  model?: string;
  model_ready?: boolean;
}
export interface EngineStatus {
  worker_url: string;
  in_use: boolean;
  local: boolean;
  reachable: boolean;
  error: string | null;
  engine: EngineState | null; // the AI service in use
  local_engine: EngineState | null; // this NVR's own AI service (null when it isn't running)
  hardware: Record<string, unknown> | null;
  hints: string[];
}
export const getAIEngine = () => apiFetch<EngineStatus>("/ai/engine");
export const setAIEngine = (device: "auto" | EngineDevice) =>
  apiFetch<EngineState>("/ai/engine", { method: "PUT", body: JSON.stringify({ device }) });
export const benchmarkAIEngine = () => apiFetch<EngineState>("/ai/engine/benchmark", { method: "POST" });
