import { apiFetch } from "./client";
import type { RemoteAccessSettings, RemoteAccessStatus } from "./types";

export function getRemoteAccessSettings(): Promise<RemoteAccessSettings> {
  return apiFetch<RemoteAccessSettings>("/remote-access/settings");
}

export function getRemoteAccessStatus(): Promise<RemoteAccessStatus> {
  return apiFetch<RemoteAccessStatus>("/remote-access/status");
}

export function updateTailscale(
  enabled: boolean,
  authkey?: string,
  hostname?: string
): Promise<RemoteAccessSettings> {
  return apiFetch<RemoteAccessSettings>("/remote-access/tailscale", {
    method: "PUT",
    body: JSON.stringify({ enabled, authkey: authkey || undefined, hostname: hostname || undefined }),
  });
}

export function updateCloudflare(enabled: boolean, token?: string): Promise<RemoteAccessSettings> {
  return apiFetch<RemoteAccessSettings>("/remote-access/cloudflare", {
    method: "PUT",
    body: JSON.stringify({ enabled, token: token || undefined }),
  });
}
