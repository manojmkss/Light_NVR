import { apiFetch, buildMediaUrl } from "./client";
import { getLiveSegment, liveSegmentVideoUrl } from "./cameras";
import { listRecordings } from "./recordings";
import type { CameraStatus, LiveSegmentInfo, RecordingMode } from "./types";

// CameraTile is shared between the authenticated Live View and the public,
// unauthenticated Kiosk view - this is the minimal surface both need to
// supply. Live View points it at the authenticated API; Kiosk points it at
// its own token-scoped public routes (see createKioskEndpoints below).
export interface CameraTileEndpoints {
  streamUrl(cameraId: number, quality: "sub" | "main", cacheBust: number): string;
  snapshotUrl(cameraId: number, cacheBust: number): string;
  recordingVideoUrl(recordingId: number, transcode?: boolean): string;
  listRecordings(cameraId: number, limit: number): Promise<TileRecording[]>;
  getLiveSegment(cameraId: number): Promise<LiveSegmentInfo>;
  liveSegmentVideoUrl(cameraId: number): string;
  /** "What plays at this moment?" - answered by the NVR (times in unix seconds). */
  resolvePlayback(cameraId: number, atSeconds: number): Promise<PlaybackAnswer>;
  /** Which parts of the window have footage, plus the NVR's clock. */
  timeline(cameraId: number, startSeconds: number): Promise<TimelineAnswer>;
  snapshotVideoUrl(cameraId: number, snapshotId: string): string;
}

export interface PlaybackAnswer {
  server_now: number;
  at: number;
  found: boolean;
  kind?: "recording" | "segment";
  recording_id?: number;
  codec?: string | null;
  snapshot?: string;
  origin?: number; // wall time of the file's first frame
  end?: number; // wall time of its last frame
  growing?: boolean; // the segment is still being recorded
  next_at?: number | null; // when footage resumes, if not found
}

export interface TimelineAnswer {
  server_now: number;
  spans: [number, number][];
}

// Narrower than the full Camera/Recording shapes - exactly what CameraTile
// itself reads - so a minimal public (kiosk) payload satisfies it too without
// needing to fabricate fields it was never given (e.g. rtsp urls, file paths).
export interface TileCamera {
  id: number;
  name: string;
  status: CameraStatus;
  recording_mode: RecordingMode;
}

export interface TileRecording {
  id: number;
  camera_id: number;
  trigger: string;
  codec?: string | null;
  started_at: string;
  ended_at: string | null;
  duration_seconds: number | null;
}

export const authenticatedEndpoints: CameraTileEndpoints = {
  streamUrl: (cameraId, quality, cacheBust) =>
    buildMediaUrl(`/cameras/${cameraId}/stream.mjpeg?quality=${quality}&k=${cacheBust}`),
  snapshotUrl: (cameraId, cacheBust) => buildMediaUrl(`/cameras/${cameraId}/snapshot.jpg?k=${cacheBust}`),
  recordingVideoUrl: (recordingId, transcode) =>
    buildMediaUrl(`/recordings/${recordingId}/video${transcode ? "?transcode=h264" : ""}`),
  listRecordings: (cameraId, limit) => listRecordings({ camera_id: cameraId, limit }),
  getLiveSegment: (cameraId) => getLiveSegment(cameraId),
  liveSegmentVideoUrl: (cameraId) => liveSegmentVideoUrl(cameraId),
  resolvePlayback: (cameraId, at) => apiFetch<PlaybackAnswer>(`/cameras/${cameraId}/playback?at=${at}`),
  timeline: (cameraId, start) => apiFetch<TimelineAnswer>(`/cameras/${cameraId}/timeline?start=${start}`),
  snapshotVideoUrl: (cameraId, snap) => buildMediaUrl(`/cameras/${cameraId}/playback/snap/${snap}.mp4`),
};
