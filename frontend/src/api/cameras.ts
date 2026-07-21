import { apiFetch, buildMediaUrl } from "./client";
import type {
  Camera,
  CameraCreatePayload,
  CameraUpdatePayload,
  DiscoveredDevice,
  LiveSegmentInfo,
  ProbeResponse,
  StreamStat,
  TestConnectionResult,
} from "./types";

export function listCameras(): Promise<Camera[]> {
  return apiFetch<Camera[]>("/cameras");
}

export function getCamera(id: number): Promise<Camera> {
  return apiFetch<Camera>(`/cameras/${id}`);
}

export function createCamera(payload: CameraCreatePayload): Promise<Camera> {
  return apiFetch<Camera>("/cameras", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateCamera(id: number, payload: CameraUpdatePayload): Promise<Camera> {
  return apiFetch<Camera>(`/cameras/${id}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function deleteCamera(id: number): Promise<void> {
  return apiFetch<void>(`/cameras/${id}`, { method: "DELETE" });
}

/** Re-run stream auto-detection (scheme, sub-stream, codec, audio) for an
 *  existing ONVIF camera using its saved credentials. Slow: the backend
 *  probes ONVIF + RTSP with fallbacks, up to ~75s worst case. */
export function redetectCamera(id: number): Promise<Camera> {
  return apiFetch<Camera>(`/cameras/${id}/redetect`, { method: "POST" });
}

/** Find an offline camera at its new IP (e.g. after a DHCP change) by ONVIF
 *  serial and update its address in place. Slow: scans the network. */
export function locateCamera(id: number): Promise<Camera> {
  return apiFetch<Camera>(`/cameras/${id}/locate`, { method: "POST" });
}

export function discoverCameras(): Promise<DiscoveredDevice[]> {
  return apiFetch<DiscoveredDevice[]>("/cameras/discover");
}

export function discoverCameraRange(cidr: string): Promise<DiscoveredDevice[]> {
  return apiFetch<DiscoveredDevice[]>("/cameras/discover-range", {
    method: "POST",
    body: JSON.stringify({ cidr }),
  });
}

export function discoverDefaultRange(): Promise<DiscoveredDevice[]> {
  return apiFetch<DiscoveredDevice[]>("/cameras/discover-default-range", { method: "POST" });
}

export function getDiscoverySettings(): Promise<{ custom_subnets: string[] }> {
  return apiFetch<{ custom_subnets: string[] }>("/cameras/discovery-settings");
}

export function updateDiscoverySettings(customSubnets: string[]): Promise<{ custom_subnets: string[] }> {
  return apiFetch<{ custom_subnets: string[] }>("/cameras/discovery-settings", {
    method: "PUT",
    body: JSON.stringify({ custom_subnets: customSubnets }),
  });
}

export function probeCamera(payload: {
  host: string;
  port?: number | null;  // omit or null = backend auto-detects from common ONVIF ports
  username: string;
  password: string;
}): Promise<ProbeResponse> {
  return apiFetch<ProbeResponse>("/cameras/probe", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function testConnection(rtspUrl: string): Promise<TestConnectionResult> {
  return apiFetch<TestConnectionResult>("/cameras/test-connection", {
    method: "POST",
    body: JSON.stringify({ rtsp_url: rtspUrl }),
  });
}

export function getStreamStats(): Promise<{ stats: StreamStat[] }> {
  return apiFetch<{ stats: StreamStat[] }>("/cameras/stream-stats");
}

/** Metadata for the segment currently being recorded for a camera, if any -
 *  404s (rejects) when nothing is being written right now. */
export function getLiveSegment(cameraId: number): Promise<LiveSegmentInfo> {
  return apiFetch<LiveSegmentInfo>(`/cameras/${cameraId}/live-segment`);
}

export function liveSegmentVideoUrl(cameraId: number): string {
  return buildMediaUrl(`/cameras/${cameraId}/live-segment/video`);
}

/** Static last-frame JPEG for a camera (latest decoded frame from the bus).
 *  Not a stream - each request returns a single image. */
export function snapshotUrl(cameraId: number): string {
  return buildMediaUrl(`/cameras/${cameraId}/snapshot.jpg`);
}
