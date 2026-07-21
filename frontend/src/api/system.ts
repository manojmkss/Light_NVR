import { apiFetch } from "./client";
import type {
  AlertSettings,
  AlertSettingsUpdate,
  DashboardData,
  NvrEvent,
  SystemStatus,
  TestEmailRequest,
  TestMessageResult,
  TestTelegramRequest,
  TestWhatsAppRequest,
} from "./types";

export function getSystemStatus(): Promise<SystemStatus> {
  return apiFetch<SystemStatus>("/system/status");
}

export function getDashboard(): Promise<DashboardData> {
  return apiFetch<DashboardData>("/system/dashboard");
}

export function listEvents(cameraId?: number, limit = 50): Promise<NvrEvent[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (cameraId !== undefined) params.set("camera_id", String(cameraId));
  return apiFetch<NvrEvent[]>(`/system/events?${params.toString()}`);
}

export function getAlertSettings(): Promise<AlertSettings> {
  return apiFetch<AlertSettings>("/system/alert-settings");
}

export function updateAlertSettings(payload: AlertSettingsUpdate): Promise<AlertSettings> {
  return apiFetch<AlertSettings>("/system/alert-settings", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function testEmail(payload: TestEmailRequest): Promise<TestMessageResult> {
  return apiFetch<TestMessageResult>("/system/alert-settings/test-email", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function testTelegram(payload: TestTelegramRequest): Promise<TestMessageResult> {
  return apiFetch<TestMessageResult>("/system/alert-settings/test-telegram", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function testWhatsApp(payload: TestWhatsAppRequest): Promise<TestMessageResult> {
  return apiFetch<TestMessageResult>("/system/alert-settings/test-whatsapp", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getSystemTime(): Promise<{ server_utc: string; server_timestamp: number }> {
  return apiFetch<{ server_utc: string; server_timestamp: number }>("/system/time");
}

export function getSystemSettings(): Promise<{ timezone: string; ntp_server: string }> {
  return apiFetch<{ timezone: string; ntp_server: string }>("/system/settings");
}

export function updateSystemSettings(payload: {
  timezone?: string;
  ntp_server?: string;
}): Promise<{ timezone: string; ntp_server: string }> {
  return apiFetch<{ timezone: string; ntp_server: string }>("/system/settings", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export interface NtpPushResult {
  camera_id: number;
  name: string;
  success: boolean;
  detail: string;
}

/** Push the saved NTP server to every enabled ONVIF camera. Slow-ish: talks
 *  to each camera in turn (up to ~15s per unreachable one). */
export function pushNtpToCameras(): Promise<NtpPushResult[]> {
  return apiFetch<NtpPushResult[]>("/system/push-ntp", { method: "POST" });
}

/** Recent backend log lines (credential-scrubbed) - the GUI's `docker logs`. */
export function getSystemLogs(limit = 200): Promise<{ lines: string[] }> {
  return apiFetch<{ lines: string[] }>(`/system/logs?limit=${limit}`);
}

/** Diagnostics bundle (versions, health, camera states, events, logs - no
 *  secrets), for attaching to a bug report. */
export function getDiagnostics(): Promise<Record<string, unknown>> {
  return apiFetch<Record<string, unknown>>("/system/diagnostics");
}
