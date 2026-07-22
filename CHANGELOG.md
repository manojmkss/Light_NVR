# Changelog

All notable changes to LightNVR are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.1] - 2026-07-22

### Fixed
- **Release pipeline.** Build each architecture on its own native GitHub runner
  (amd64 on `ubuntu-latest`, arm64 on `ubuntu-24.04-arm`) instead of emulating
  arm64 under QEMU. The emulated frontend build hung for hours and left v0.2.0's
  images incompletely published. No application code changed from v0.2.0 - this
  is the first fully-published build of the v0.2.0 features below.

## [0.2.0] - 2026-07-22

### Added
- **Motion alert screenshots.** Every motion alert now carries the frame it
  fired on: attached to the alert email, sent as a photo on Telegram, sent as
  an image message on WhatsApp, and shown as a thumbnail on the dashboard's
  Recent Events feed. Web push notifications stay text-only, since showing an
  image there would need an unauthenticated image URL, which isn't a tradeoff
  worth making for a notification-bubble thumbnail. Screenshots are pruned
  along with their event after the existing 90-day event retention window.
- **Motion zones.** Draw regions on a camera's snapshot to *ignore* (a waving
  tree, a busy road) or *watch only* (just the driveway). Motion detection masks
  the ignored areas before counting motion, which also means the AI layer and
  alerts stop firing on them - the biggest cut to false alerts. Edit them from
  Cameras → Zones; stored per camera, empty = the whole frame as before.
- **H.265 recordings now play in every browser.** HEVC won't decode in Firefox
  and many Chrome installs. When you open an H.265 clip in a browser that can't
  play it, LightNVR transcodes that clip to H.264 on demand and plays it
  (cached, so re-watching is instant) - recordings stay stored as efficient
  H.265, and the transcode only runs when actually needed. Recordings also show
  a codec badge. Works on the authenticated playback pages and kiosk views.
- **Self-healing for camera IP changes.** When an ONVIF camera goes unreachable
  (typically a DHCP lease change after a router reboot), a background task finds
  it again at its new address by matching the device's ONVIF serial number,
  updates the stored URLs automatically, and reconnects - no manual re-add. A
  **Locate** button on offline cameras triggers the same search on demand. Docs
  now also recommend DHCP reservations as the belt-and-braces fix.

## [0.1.0] - 2026-07-08

First tagged release, with prebuilt multi-arch (amd64 + arm64) images published
to GHCR so a fresh install is `docker compose pull && docker compose up -d` -
no build step.

### Added
- **Prebuilt images + releases.** `ghcr.io/manojmkss/light_nvr-backend` and
  `-frontend`, built for amd64 and arm64 by the release workflow. `docker
  compose pull` fetches them; building from source stays available as a
  fallback.
- **NVR multi-channel import.** Adding a multi-channel recorder now detects each
  channel (grouped by ONVIF video source) and offers to import them all at once,
  each as its own camera with main/sub streams pre-detected.
- **NTP push to cameras.** One button (Settings → System) points every ONVIF
  camera's clock at the configured NTP server, keeping recording timestamps
  aligned across cameras.
- **Offline reasons.** When a camera goes offline the specific cause (auth
  failure, connection refused, timeout, …) is shown on the Cameras page instead
  of a bare "offline".
- **In-app logs & diagnostics.** Recent backend logs (credential-scrubbed) are
  viewable in Settings → System, plus a one-click diagnostics bundle for bug
  reports - no `docker logs` needed.
- AI object detection (YOLO) with a local-CPU or remote-GPU backend, optional
  VLM scene descriptions, and graceful fallback to plain motion when disabled.
- One-command install scripts for Linux and Windows, and matching updaters.
- Kiosk view: shareable, login-free live grids scoped to chosen cameras.
- Native Tailscale and Cloudflare Tunnel remote access, configured from the GUI.
- Web push, Telegram, and WhatsApp notification channels.
- Interactive canvas timeline with clip export (stream-copy, no re-encode).

### Security
- Backend API bound to loopback only; modern TLS + HSTS/security headers at
  nginx; camera credentials redacted from API responses for non-admin users;
  login and token-refresh rate limiting; RTSP-scheme validation on camera URLs.

### Infrastructure
- SQLite on a named Docker volume (WAL-safe across platforms) with startup
  integrity checks and crash recovery.
- `.gitattributes` pins shell scripts to LF so a fresh Windows clone runs
  cleanly (no nginx CRLF crash-loop).

[Unreleased]: https://github.com/manojmkss/Light_NVR/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/manojmkss/Light_NVR/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/manojmkss/Light_NVR/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/manojmkss/Light_NVR/releases/tag/v0.1.0
