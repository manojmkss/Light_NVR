import { apiFetch, buildMediaUrl } from "./client";
import type { Recording } from "./types";

export interface RecordingFilters {
  camera_id?: number;
  trigger?: string;
  start?: string;
  end?: string;
  limit?: number;
  offset?: number;
}

export function listRecordings(filters: RecordingFilters = {}): Promise<Recording[]> {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      params.set(key, String(value));
    }
  });
  const query = params.toString();
  return apiFetch<Recording[]>(`/recordings${query ? `?${query}` : ""}`);
}

export function deleteRecording(id: number): Promise<void> {
  return apiFetch<void>(`/recordings/${id}`, { method: "DELETE" });
}

export function bulkDeleteRecordings(
  ids: number[],
): Promise<{ deleted: number; not_found: number[]; events_removed: number }> {
  return apiFetch(`/recordings/bulk-delete`, {
    method: "POST",
    body: JSON.stringify({ ids }),
  });
}

/** Force-download URL for a single recording (Save-As with a friendly filename). */
export function recordingDownloadUrl(id: number): string {
  return buildMediaUrl(`/recordings/${id}/video?download=1`);
}

/** Streaming .zip download URL for a set of recordings. */
export function bulkDownloadZipUrl(ids: number[]): string {
  return buildMediaUrl(`/recordings/download-zip?ids=${ids.join(",")}`);
}

/** Direct-download URL for a stitched, full-quality clip covering [startTs, endTs]
 *  (Unix seconds) for one camera. `name` is a client-suggested filename. */
export function clipExportUrl(cameraId: number, startTs: number, endTs: number, name?: string): string {
  const params = new URLSearchParams({
    camera_id: String(cameraId),
    start: String(startTs),
    end: String(endTs),
  });
  if (name) params.set("name", name);
  return buildMediaUrl(`/recordings/export?${params.toString()}`);
}
