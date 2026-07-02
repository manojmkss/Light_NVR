import { apiFetch, getAccessToken } from "./client";
import type { BackupInfo, BackupSettings, BackupSettingsUpdate, OptimizeResult } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api";

export function getBackupSettings(): Promise<BackupSettings> {
  return apiFetch<BackupSettings>("/backup/settings");
}

export function updateBackupSettings(payload: BackupSettingsUpdate): Promise<BackupSettings> {
  return apiFetch<BackupSettings>("/backup/settings", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function listBackups(): Promise<BackupInfo[]> {
  return apiFetch<BackupInfo[]>("/backup/list");
}

export function createBackupNow(): Promise<BackupInfo> {
  return apiFetch<BackupInfo>("/backup/create", { method: "POST" });
}

export function deleteBackup(filename: string): Promise<void> {
  return apiFetch<void>(`/backup/${encodeURIComponent(filename)}`, { method: "DELETE" });
}

export function restoreBackup(filename: string): Promise<{ detail: string }> {
  return apiFetch<{ detail: string }>(`/backup/restore/${encodeURIComponent(filename)}`, { method: "POST" });
}

export function downloadBackupUrl(filename: string): string {
  const token = getAccessToken();
  const base = `${API_BASE}/backup/download/${encodeURIComponent(filename)}`;
  return token ? `${base}?token=${encodeURIComponent(token)}` : base;
}

export function optimizeDatabase(): Promise<OptimizeResult> {
  return apiFetch<OptimizeResult>("/backup/optimize", { method: "POST" });
}

// Not routed through apiFetch: FormData uploads need the browser to set their
// own multipart Content-Type boundary, which apiFetch's JSON-default would override.
export async function restoreFromUpload(file: File): Promise<{ detail: string }> {
  const formData = new FormData();
  formData.append("file", file);

  const token = getAccessToken();
  const headers: HeadersInit = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${API_BASE}/backup/restore-upload`, {
    method: "POST",
    headers,
    body: formData,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || res.statusText);
  }
  return res.json();
}

// After a restore, the backend process exits and Docker restarts it - poll
// until it answers again rather than guessing a fixed delay (restart time
// varies a lot between a Pi and a desktop).
export async function waitForServerRestart(maxWaitMs = 60000): Promise<void> {
  const start = Date.now();
  await new Promise((r) => setTimeout(r, 2000));
  while (Date.now() - start < maxWaitMs) {
    try {
      const res = await fetch(`${API_BASE}/health`);
      if (res.ok) return;
    } catch {
      // not back up yet
    }
    await new Promise((r) => setTimeout(r, 1500));
  }
  throw new Error("Server did not come back up in time");
}
