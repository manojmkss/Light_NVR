import { apiFetch } from "./client";
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
