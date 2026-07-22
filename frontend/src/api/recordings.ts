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

/** Whether this browser can play H.265/HEVC in a <video> element. Safari can;
 *  Firefox can't; Chrome only with OS/hardware support. Computed once. */
export const browserPlaysHevc: boolean = (() => {
  try {
    const v = document.createElement("video");
    return (
      v.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"') !== "" ||
      v.canPlayType('video/mp4; codecs="hev1.1.6.L93.B0"') !== ""
    );
  } catch {
    return false;
  }
})();

/** True when this recording won't play natively here and needs server-side
 *  transcoding (it's H.265 and the browser can't do HEVC). Accepts any object
 *  carrying a codec (full Recording or the narrower TileRecording). */
export function recordingNeedsTranscode(rec: { codec?: string | null }): boolean {
  return rec.codec === "h265" && !browserPlaysHevc;
}

/** Playback URL for a recording: the raw file when the browser can play it, or
 *  the on-demand H.264 transcode when it can't (H.265 on Firefox/Chrome). */
export function recordingPlaybackUrl(rec: { id: number; codec?: string | null }): string {
  const suffix = recordingNeedsTranscode(rec) ? "?transcode=h264" : "";
  return buildMediaUrl(`/recordings/${rec.id}/video${suffix}`);
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
