import { apiFetch, getAccessToken } from "./client";
import type { TlsActionResult, TlsSettings } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api";

export function getTlsSettings(): Promise<TlsSettings> {
  return apiFetch<TlsSettings>("/tls/settings");
}

export function useSelfSignedCert(): Promise<TlsActionResult> {
  return apiFetch<TlsActionResult>("/tls/self-signed", { method: "POST" });
}

export function useLetsEncrypt(domain: string, email: string): Promise<TlsActionResult> {
  return apiFetch<TlsActionResult>("/tls/letsencrypt", {
    method: "POST",
    body: JSON.stringify({ domain, email }),
  });
}

// Not routed through apiFetch: multipart upload needs the browser's own
// Content-Type boundary, which apiFetch's JSON-default would override.
export async function uploadCustomCert(certFile: File, keyFile: File): Promise<TlsActionResult> {
  const formData = new FormData();
  formData.append("cert", certFile);
  formData.append("key", keyFile);

  const token = getAccessToken();
  const headers: HeadersInit = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${API_BASE}/tls/custom`, { method: "POST", headers, body: formData });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || res.statusText);
  }
  return res.json();
}
