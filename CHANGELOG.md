# Changelog

All notable changes to LightNVR are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.1] - 2026-10-20

Same application code as 0.3.0, released so `./scripts/update-linux.sh`
pulls prebuilt images (it builds from source when the newest commit isn't a
release tag).

### Fixed
- CI: the backend test job no longer fails when every test passes.

## [0.3.0] - 2026-10-20

Motion is rebuilt end to end: detection that actually compares frames,
recordings that start before the motion, and AI that watches the whole event
instead of glancing at one frame - followed by a summary of what happened.

### Added
- **Continuous AI monitoring during motion.** While motion lasts, the newest
  full-resolution frame is checked every 500ms (Settings → AI → Advanced).
  Checks are skipped, never queued, if the machine can't keep up, so alerts
  never fall behind. Detections are tracked across frames, so one person
  walking across is one person, not six.
- **One alert per event, with boxes.** Sent on the first confirmed person,
  vehicle or animal, with labelled boxes drawn on the full-resolution frame.
- **End-of-event summary.** When things go quiet: start/end time, duration,
  what was seen ("2 people, 1 car"), most at once, an appear/leave timeline,
  the clip's time range and the clearest picture - plus, with a vision model
  configured, a sentence describing it. Works without AI too (duration and how
  much moved). Toggle under Settings → AI.
- **Pre-roll for motion recordings.** A rolling buffer keeps the last few
  seconds on disk, so every clip starts before the motion (default 5s) and
  ends after it (default 10s). Both are per-camera settings.
- AI keeps an event open while someone stands still in view (but not for a
  car parked for the night).

### Changed
- **Motion detection compares frames.** Each frame is checked against the
  frames from ~0.4s and ~0.8s earlier, the change must persist for about half
  a second, and the sensitivity slider now states what share of the scene has
  to change. Whole-scene changes - lights, the IR switch, auto-exposure - are
  ignored instead of triggering.
- Live view keeps each camera's real aspect ratio (4:3 substreams were squashed
  to 16:9) at higher quality.
- Motion recording thumbnails show the moment motion was detected.
- The vision-model description now goes into the end-of-event summary rather
  than delaying the first alert.

### Fixed
- AI and alert pictures were a small, recompressed, sometimes stale live-view
  frame - often one left over from before the camera dropped.
- Motion clips lost their first seconds to the RTSP connect and keyframe wait
  (the first event after boot also waited on a 10s audio probe), had start
  times that didn't match the footage, and could stop recording entirely after
  a 10-minute event or an ffmpeg crash.
- Slow-moving or briefly stationary people faded into the background and ended
  the event; a single noisy frame could start one.
- Camera workers now stop in parallel, so open clips and summaries are saved
  within Docker's stop timeout.

## [0.2.5] - 2026-08-04

Playback and export are rebuilt around a single player, and a run of issues
that made the system feel slow or unresponsive over time are fixed - several of
which also made updates themselves appear to do nothing.

### Changed
- **Playback & Export redesigned as a VMS review screen.** A horizontal camera
  strip across the top - each card showing a still from that camera so you can
  recognise it by its view rather than its name - one large player for the
  selected camera, and a docked timeline plus transport controls that stay in
  view. The whole page fits the window without scrolling. Only the selected
  camera is ever transcoded, so opening a multi-camera review no longer fires
  one transcode per camera.

### Added
- **Browser-friendly (H.264) clip export.** Clip export has a format toggle:
  **Original** (fast stream-copy in the camera's codec) or **H.264**
  (re-encoded so the downloaded file plays in any browser).
- **Snapshot.** Save the frame currently on screen as a PNG, straight from the
  player - no server round-trip.
- **Container memory ceilings.** `install-linux.sh` sizes the backend's limit
  from the host's RAM (half of it, capped at 4GB, floored at 768MB) and all
  limits are `.env`-overridable. A runaway backend is now contained and
  restarted instead of exhausting the host and dragging everything else down.

### Fixed
- **Live and kiosk tiles no longer go permanently blank.** A camera going quiet
  for 30s ended the MJPEG response *cleanly*, which an `<img>` doesn't report
  as an error - so nothing retried and the tile stayed frozen until the kiosk's
  6-hourly reload. The stream is now held open across camera hiccups and
  resumes on its own.
- **The app no longer stalls when moving between pages.** nginx serves HTTP/2,
  so long-lived video streams and ordinary API calls stop competing for the
  ~6 connections a browser allows per origin. Previously, opening Live View and
  a recording could fill that pool and leave every subsequent request queued for
  minutes.
- **Updates take effect on reload.** The service worker now activates
  immediately rather than waiting for every tab of the site to be closed, which
  had made new versions look like they hadn't installed.
- **Config backup download** returned an API error every time: the link carried
  a query token but the endpoint only reads the `Authorization` header.
- **Clip export failures are visible.** A failed export saved the error response
  as a `.mp4` instead of reporting it.
- **Playback transcodes are bounded and stop when you leave.** Capped so a
  multi-camera view can't peg every core, and ffmpeg is killed as soon as the
  viewer navigates away or hides the tab.
- **Audio is stream-copied when the camera already sends AAC**, instead of
  always re-encoding.

### Infrastructure
- **`update-linux.sh` actually updates.** Four separate faults could each make
  it appear to do nothing: it pulled an unchanged `:latest` and left the old
  containers running; a missing `.env` key made `env_get` abort the whole script
  silently under `set -e`; it assumed the user could reach the Docker socket; and
  it couldn't pick up fixes to its own scripts mid-run. It now builds from
  source when no published image matches the checkout, falls back to `sudo`
  automatically, and re-execs when the update machinery itself changes - so it
  remains a genuine one-command update.

## [0.2.4] - 2026-07-30

### Changed
- **Scrub bar reaches back 1 hour** (was 30 minutes) on the live and kiosk
  tiles. Older footage remains available via Browse older recordings / the
  Recordings page; retention is unchanged.
- **H.264 playback copies are now short-lived.** The sub-stream playback copy
  (see 0.2.3) is deleted ~1 hour after a segment ends - once it's past the
  scrub window - to reclaim disk, since scrubbing to older footage is rare and
  transcodes on demand. The full-quality H.265 main is untouched and kept for
  the entire retention period.

## [0.2.3] - 2026-07-30

### Added
- **Instant H.265 recording playback via an H.264 sub-stream copy.** For cameras
  that record H.265 (which Firefox and most Chrome can't decode), LightNVR now
  records the camera's H.264 sub-stream in parallel as a small, seekable
  playback companion (a plain stream-copy - almost no extra CPU). Browsers play
  that copy instantly with no on-demand transcode, and it also makes the last
  few minutes - the segment still being written - watchable, where a raw H.265
  live segment previously showed black. The H.265 main stays the full-quality
  original for download/export. Falls back to on-demand transcoding when a
  camera has no distinct H.264 sub-stream. Applies to new recordings.

## [0.2.2] - 2026-07-23

### Fixed
- **Seeking/scrubbing recorded footage in the live and kiosk tiles.** Two
  causes: recordings were stored as fragmented MP4 (needed so the in-progress
  segment plays live) which carries no seek index, and the file endpoint didn't
  answer HTTP Range requests, so browsers disabled the scrubber entirely.
  Finalized segments are now remuxed (stream copy, no re-encode) to a seekable
  faststart MP4, and recording playback is served with Range support (206
  Partial Content) on both the authenticated and kiosk video endpoints. Seeking
  back to review footage now works.

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

[Unreleased]: https://github.com/manojmkss/Light_NVR/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/manojmkss/Light_NVR/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/manojmkss/Light_NVR/compare/v0.2.5...v0.3.0
[0.2.5]: https://github.com/manojmkss/Light_NVR/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/manojmkss/Light_NVR/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/manojmkss/Light_NVR/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/manojmkss/Light_NVR/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/manojmkss/Light_NVR/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/manojmkss/Light_NVR/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/manojmkss/Light_NVR/releases/tag/v0.1.0
