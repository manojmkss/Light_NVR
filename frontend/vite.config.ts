import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      // injectManifest (not the default generateSW) because push notifications
      // need a custom `push`/`notificationclick` handler in the service
      // worker - see src/sw.ts. There's no fetch handler in it at all beyond
      // precaching the static app shell, so /api/ calls (live camera/system
      // state) are never intercepted or cached - they always hit the network.
      strategies: "injectManifest",
      srcDir: "src",
      filename: "sw.ts",
      injectManifest: {
        injectionPoint: "self.__WB_MANIFEST",
      },
      registerType: "autoUpdate",
      includeAssets: ["icons/favicon-16.png", "icons/favicon-32.png"],
      manifest: {
        id: "/",
        name: "LightNVR",
        short_name: "LightNVR",
        description: "Self-hosted home NVR",
        theme_color: "#0f1216",
        background_color: "#0f1216",
        display: "standalone",
        orientation: "any",
        start_url: "/",
        scope: "/",
        icons: [
          { src: "icons/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
          { src: "icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
          { src: "icons/icon-192-maskable.png", sizes: "192x192", type: "image/png", purpose: "maskable" },
          { src: "icons/icon-512-maskable.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
        ],
      },
    }),
  ],
  server: {
    host: true,
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_DEV_API_TARGET || "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
