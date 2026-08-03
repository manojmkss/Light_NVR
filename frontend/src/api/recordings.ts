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

/**
 * Download an exported clip, surfacing real failures.
 *
 * A plain `<a download>` on the export URL silently saves whatever the server
 * returned - so a 404 ("No footage found in that time range") or a 500 landed
 * on disk as a few-hundred-byte ".mp4" that simply wouldn't play, with no error
 * shown anywhere. Fetching first lets us check the status, surface the server's
 * own message, and only save the file once we know it's really a video. It also
 * means the promise resolves when the export is genuinely finished, so the UI
 * can show an honest progress state instead of a fixed timer.
 */
export async function downloadClip(opts: {
  cameraId: number;
  startTs: number;
  endTs: number;
  name: string;
  h264?: boolean;
}): Promise<void> {
  const url = clipExportUrl(opts.cameraId, opts.startTs, opts.endTs, opts.name, opts.h264);
  const res = await fetch(url);
  if (!res.ok) {
    let detail = `Export failed (HTTP ${res.status})`;
    try {
      const body = await res.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* error body wasn't JSON - keep the status-code message */
    }
    throw new Error(detail);
  }
  const blob = await res.blob();
  if (blob.size === 0) throw new Error("Export produced an empty file");
  const objectUrl = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = objectUrl;
    a.download = `${opts.name}.mp4`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

/** Streaming .zip download URL for a set of recordings. */
export function bulkDownloadZipUrl(ids: number[]): string {
  return buildMediaUrl(`/recordings/download-zip?ids=${ids.join(",")}`);
}

/** Direct-download URL for a stitched clip covering [startTs, endTs] (Unix
 *  seconds) for one camera. `name` is a client-suggested filename. By default
 *  the export is a full-quality stream copy (original codec); pass h264=true to
 *  re-encode to H.264 so the downloaded file plays in any browser. */
export function clipExportUrl(cameraId: number, startTs: number, endTs: number, name?: string, h264 = false): string {
  const params = new URLSearchParams({
    camera_id: String(cameraId),
    start: String(startTs),
    end: String(endTs),
  });
  if (name) params.set("name", name);
  if (h264) params.set("h264", "1");
  return buildMediaUrl(`/recordings/export?${params.toString()}`);
}
