import { apiFetch } from "./client";
import type { CameraTileEndpoints, TileRecording } from "./liveEndpoints";
import type { KioskPublicView, KioskView, LiveSegmentInfo, StreamStat } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api";

export function listKioskViews(): Promise<KioskView[]> {
  return apiFetch<KioskView[]>("/kiosk/views");
}

export function createKioskView(payload: { name: string; layout: number; camera_ids: number[] }): Promise<KioskView> {
  return apiFetch<KioskView>("/kiosk/views", { method: "POST", body: JSON.stringify(payload) });
}

export function updateKioskView(
  id: number,
  payload: { name?: string; layout?: number; camera_ids?: number[] }
): Promise<KioskView> {
  return apiFetch<KioskView>(`/kiosk/views/${id}`, { method: "PUT", body: JSON.stringify(payload) });
}

export function regenerateKioskToken(id: number): Promise<KioskView> {
  return apiFetch<KioskView>(`/kiosk/views/${id}/regenerate-token`, { method: "POST" });
}

export function deleteKioskView(id: number): Promise<void> {
  return apiFetch<void>(`/kiosk/views/${id}`, { method: "DELETE" });
}

export function kioskShareUrl(token: string): string {
  return `${window.location.origin}/kiosk/${token}`;
}

// The public kiosk page is intentionally never authenticated - these two
// bypass apiFetch entirely so an admin's session token, if present in this
// browser, is never attached to a request the page has no business sending it on.
export async function getPublicKioskView(token: string): Promise<KioskPublicView> {
  const res = await fetch(`${API_BASE}/kiosk/public/${token}`);
  if (!res.ok) {
    throw new Error(res.status === 404 ? "This kiosk link is no longer valid." : `Error ${res.status}`);
  }
  return res.json();
}

async function publicJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Error ${res.status}`);
  return res.json();
}

export function getKioskStreamStats(token: string): Promise<{ stats: StreamStat[] }> {
  return publicJson(`${API_BASE}/kiosk/public/${token}/stream-stats`);
}

// CameraTile is written against CameraTileEndpoints so the same component
// (grid engine, adaptive stream, overlay, scrub bar, recordings picker) works
// unmodified against Kiosk's public, token-scoped routes - giving Kiosk full
// feature parity with the authenticated Live View without a second
// implementation to keep in sync.
export function createKioskEndpoints(token: string): CameraTileEndpoints {
  const base = `${API_BASE}/kiosk/public/${token}`;
  return {
    streamUrl: (cameraId, quality, cacheBust) =>
      `${base}/cameras/${cameraId}/stream.mjpeg?quality=${quality}&k=${cacheBust}`,
    snapshotUrl: (cameraId, cacheBust) => `${base}/cameras/${cameraId}/snapshot.jpg?k=${cacheBust}`,
    recordingVideoUrl: (recordingId, transcode) =>
      `${base}/recordings/${recordingId}/video${transcode ? "?transcode=h264" : ""}`,
    listRecordings: (cameraId, limit) =>
      publicJson<TileRecording[]>(`${base}/cameras/${cameraId}/recordings?limit=${limit}`),
    getLiveSegment: (cameraId) => publicJson<LiveSegmentInfo>(`${base}/cameras/${cameraId}/live-segment`),
    liveSegmentVideoUrl: (cameraId) => `${base}/cameras/${cameraId}/live-segment/video`,
  };
}
