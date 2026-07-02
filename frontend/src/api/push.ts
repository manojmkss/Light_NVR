import { apiFetch } from "./client";

export interface PushTestResult {
  success: boolean;
  message: string;
}

export function isPushSupported(): boolean {
  return window.isSecureContext && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

export function isRunningAsInstalledApp(): boolean {
  return window.matchMedia("(display-mode: standalone)").matches || (navigator as unknown as { standalone?: boolean }).standalone === true;
}

function base64UrlToUint8Array(base64Url: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64Url.length % 4)) % 4);
  const base64 = (base64Url + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const array = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) {
    array[i] = raw.charCodeAt(i);
  }
  return array;
}

export async function getCurrentSubscription(): Promise<PushSubscription | null> {
  if (!isPushSupported()) return null;
  const registration = await navigator.serviceWorker.ready;
  return registration.pushManager.getSubscription();
}

export async function subscribeToPush(): Promise<void> {
  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error("Notification permission was not granted");
  }

  const { public_key } = await apiFetch<{ public_key: string | null }>("/push/vapid-public-key");
  if (!public_key) {
    throw new Error("Server push keys aren't ready yet - try again in a moment");
  }

  const registration = await navigator.serviceWorker.ready;
  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: base64UrlToUint8Array(public_key),
  });

  const json = subscription.toJSON();
  await apiFetch<void>("/push/subscribe", {
    method: "POST",
    body: JSON.stringify({
      endpoint: json.endpoint,
      keys: { p256dh: json.keys?.p256dh, auth: json.keys?.auth },
    }),
  });
}

export async function unsubscribeFromPush(): Promise<void> {
  const subscription = await getCurrentSubscription();
  if (!subscription) return;

  const endpoint = subscription.endpoint;
  await subscription.unsubscribe();
  await apiFetch<void>("/push/unsubscribe", {
    method: "POST",
    body: JSON.stringify({ endpoint }),
  });
}

export function testPush(): Promise<PushTestResult> {
  return apiFetch<PushTestResult>("/push/test", { method: "POST" });
}
