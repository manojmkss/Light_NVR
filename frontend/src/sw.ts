/// <reference lib="webworker" />
import { cleanupOutdatedCaches, precacheAndRoute } from "workbox-precaching";

declare let self: ServiceWorkerGlobalScope;

// Drop precaches left by previous builds, so an update doesn't leave the old
// app shell taking up storage forever.
cleanupOutdatedCaches();

// Only the static app shell is precached here - there is deliberately no
// fetch handler beyond what this gives us, so /api/ calls (live camera
// state, recordings, auth) are never intercepted or cached. They always hit
// the network fresh.
precacheAndRoute(self.__WB_MANIFEST);

// Take over as soon as an update is installed.
//
// These are NOT optional here: with `strategies: "injectManifest"` the PWA
// plugin does not inject them into a custom service worker (unlike the default
// generateSW), so without them a new worker installs and then sits in the
// "waiting" state while the PREVIOUS worker keeps serving the old cached app
// shell - until every tab of the site is closed. The practical effect is that
// a server-side update appears to do nothing, no matter how many times the
// user reloads. skipWaiting activates the new worker immediately and
// clients.claim() puts already-open tabs under its control.
self.skipWaiting();
self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

interface PushPayload {
  title?: string;
  body?: string;
}

self.addEventListener("push", (event: PushEvent) => {
  let payload: PushPayload = {};
  try {
    payload = event.data?.json() ?? {};
  } catch {
    payload = { body: event.data?.text() ?? "" };
  }

  const title = payload.title || "LightNVR";
  event.waitUntil(
    self.registration.showNotification(title, {
      body: payload.body || "",
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-192.png",
    })
  );
});

self.addEventListener("notificationclick", (event: NotificationEvent) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window" }).then((clientList) => {
      for (const client of clientList) {
        if ("focus" in client) return (client as WindowClient).focus();
      }
      return self.clients.openWindow("/");
    })
  );
});
