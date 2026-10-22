# Changelog

All notable changes to LightNVR are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.5] - 2026-10-22

Faces that get recognised from your cameras' point of view, and that keep
getting better; visits that count people properly.

### Added
- **Learns by itself.** When someone in Family or Familiar is recognised with
  confidence (two agreeing sightings or one clear one, a sharp frontal face,
  a clear margin over everyone else), that camera sighting is added to their
  photos automatically - at most 5 a day and 40 in all, oldest replaced - so
  recognition adapts to how they look on *your* cameras. The People table
  shows "learned N sightings by itself".
- **Near misses are visible.** Each "Who is this?" card says who it looks most
  like and how close it came ("41% similar; recognised from 45%"), with a
  one-click "Yes, it's ..." that teaches the NVR. "N sightings look like X -
  Add all" files them in one go. Sightings group more readily across visits.
- **AdaFace (optional).** A second face recogniser built for small, blurred,
  badly lit faces. Download it on the People page (92 MB; weights for home
  use), compare it with the current one on your own confirmed faces, and
  switch only if it wins.
- **The same person coming back into view is counted once** ("person #1 came
  back"), from their clothing colours and recognised face, within 5 minutes -
  and doesn't trigger another "another person arrived" alert.
- **Main stream for real events.** Once AI confirms a person, vehicle or
  animal, the rest of the event uses the camera's full-resolution stream for
  detection, snapshots, faces and descriptions (not on motion alone).
- Visits page: one-click "Use my timezone" when the NVR has none set (visit
  texts and alerts were in UTC).

### Changed
- Recognition strength says "weak - only phone photos" until someone has a
  few camera sightings: phone photos alone match CCTV faces poorly.
- Visits count visits per kind ("people in 40 visits") instead of adding up
  every re-entry into view ("170 persons").
- "Who is this?" cards are compact.
- The AI worker returns the aligned face with each match (better pictures,
  and what AdaFace needs).

## [0.4.4] - 2026-10-22

### Added
- **"Faces can't be captured yet" checklist** on the People page whenever
  something needed is off: AI, object detection, AI working, face
  recognition and its privacy acknowledgement, face models, and motion
  detection on each camera (continuous-recording cameras need it turned on
  separately - faces and visits only come from motion events). Each red item
  says where to fix it. `GET /api/faces/readiness`.

## [0.4.3] - 2026-10-22

### Added
- **Click a face to watch that moment.** Faces under "Who is this?" and each
  person's photo open a popup that plays that camera from a few seconds
  before the face was captured, carrying on into the next recording. On the
  Visits page, the visit picture and a ▶ next to every event do the same.

### Changed
- **"Forget all" keeps Family.** The button is now "Forget everyone except
  Family": Family members and their photos stay; remove one by one with
  Delete if you really mean it.
- The "Face capture today" counts on the People page survive a restart or an
  update, so a miss earlier in the day can still be explained.
- Each captured face now records when it was on camera (it used the time the
  event was saved, up to a minute later).

## [0.4.2] - 2026-10-22

Rewinding live view now always finds the footage; faces are looked for in the
full-resolution picture; the People and Visits pages are easier to use.

### Fixed
- **"No recording at this moment" when dragging the live-view slider back.**
  The tile worked out what to play from a list it refreshed every 30s, and
  footage disappeared from it for a while after every 5-minute segment
  closed; the segment being recorded couldn't be seeked at all. The NVR now
  answers "what plays at this moment?" itself (finished recordings, segments
  still being finalized, and the one being recorded, served as a seekable
  copy), the tile keeps playing into the next segment instead of jumping to
  live, retries instead of giving up, and runs on the NVR's clock. The slider
  is brighter where there is footage; a real gap says why (camera offline,
  motion-only recording) and offers "Jump to next footage". Kiosk views too.
- Recording start times come from the footage itself, so seeking lands on the
  right second (they were 1-3s early: the camera connect time).
- Faces are searched in the camera's full-resolution main stream while a
  person is in view (it was the small live picture, where faces across a yard
  are too small), and looked for every 1.5s for as long as they're in view
  instead of 6 tries.

### Added
- **People page as a table**: photo, name, relation, trust (Family / Familiar /
  Stranger / Suspicious) and "Notify on arrival" (Yes / Only when Away / No),
  all edited in place, with recognition strength (weak / good / strong).
  "Who is this?" groups the same person's sightings, so you name them once;
  strangers and suspicious people need no name.
- **Face capture today** on the People page: per camera, how many people were
  checked, how many faces were usable, and why not (not facing the camera,
  too small, blurry) with what to do about it.
- **Visits page summary**: Today / Yesterday / Last 7 days, real activity
  only (motion-only visits hidden, counted), what was seen, who, which
  cameras, the busiest hour; every visit shows its summary and its events.

### Changed
- "Regular" and "Visitor" people became "Familiar" (existing people and alert
  rules are moved over automatically). A recognised stranger is treated like
  an unknown face by alert rules.

## [0.4.1] - 2026-10-21

Stability and security: the NVR now heals itself when something hangs, not
only when something crashes.

### Added
- **Freeze watchdog.** If the server stops responding for 90s (for example a
  hung network-storage call), it logs where it was stuck and restarts itself;
  Docker brings it straight back. It also restarts cleanly before running out
  of memory, instead of being killed at random by the system.
- **Self-healing cameras.** Every 30s each camera is checked: live-stream
  thread alive and making progress, recordings and the motion buffer producing
  while the camera is delivering pictures. A stuck camera is restarted on its
  own (with increasing delays if it keeps failing) and you get an event saying
  why. Per-camera health at `GET /api/system/health` and in diagnostics.
- The motion recording buffer restarts ffmpeg when it stays connected but
  stops sending video.
- The AI worker restarts itself if an inference hangs (GPU/driver lock-up).

### Changed
- Alerts are sent in the background: a slow mail server or Telegram outage no
  longer holds up a camera's AI or recording.
- Copies to and deletes on network storage run off the main thread with time
  limits, so an unresponsive NAS can't freeze the system.
- On stop, open motion clips and summaries get time to save (45s grace;
  live-view connections no longer hold the shutdown back), and an init process
  reaps stray ffmpeg processes.
- One camera failing to start no longer stops the others (or the NVR) from
  starting; camera edits can't race each other into two workers.
- Database maintenance waits 10 minutes after boot and only runs the
  write-blocking VACUUM when there's real space to reclaim; the write-ahead
  log is checkpointed daily.
- AI monitoring survives a failing check instead of stopping for the rest of
  the event; repeated AI failures are logged once a minute, not every check.
- Thumbnail generation has a time limit; visits whose camera never reported an
  end are closed after their 1-hour cap.

### Security
- Login tokens in URLs (`?token=`) and signed links are masked in all logs,
  including Docker's access log.
- Face crops are admin-only; face photo uploads (15 MB) and AI worker images
  (20 MB) are size-limited; the AI worker compares its API key in constant time.

## [0.4.0] - 2026-10-20

The home-security release: one visit is one event (even when someone waits at
the gate for five minutes), arm modes with alert rules, visits followed across
cameras, faces you choose to have recognised, a local AI service that uses your
GPU when you have one, tamper and package alerts, and search.

### Added - arm modes & notifications
- **Home / Away / Night / Disarmed**, switched from the sidebar, Home Assistant
  (new alarm panel entity) or a weekly schedule (Settings → Modes). Modes change
  who is alerted about what, never what is recorded.
- **Alert rules**: per mode, camera, kind of event, object, zone and person -
  notify, **urgent** (repeats until "I've seen it") or quiet; optional webhook
  (e.g. a siren via Home Assistant). Suggested rules are added switched off, and
  "What would happen?" previews the rules on recent events. With no rules on,
  alerts behave exactly as before.
- Push notifications now show the snapshot, open the event page when tapped,
  and urgent ones stay on screen with an "I've seen it" button. Alerts include
  a link to the event when you set the NVR's outside address (Settings → Alerts).

### Added - visits across cameras
- **Visits**: give each camera an area (Gate, Driveway, Porch...) and link areas
  with a walking time. A visit gets one alert (plus one if it reaches a private
  area like the back yard) and one summary of the whole route. New Visits page.

### Added - zones, packages, tamper, search
- **Named zones** with loitering alerts ("Person loitering in 'Doorstep' (1m)")
  and **lines** with crossing alerts, drawn in the zone editor.
- **Package left / taken** at the door (bags/boxes the detector can see).
- **Tamper alerts**: camera covered, blacked out, blurred or turned away -
  urgent in every mode. Gradual nightfall is not tampering.
- **Search** in plain words: "person in red shirt yesterday", "car driveway",
  "Ramesh", "package today" - by object, clothing colour, name, camera, area and
  time. New Search page.

### Added - faces (opt-in, local only)
- **People**: enrol someone from photos or from a captured face ("Who is
  this?"); alerts then say "Ramesh (milkman) arrived at 'Gate'", and unknown
  faces are flagged. Conservative matching (two agreeing sightings, a margin
  over look-alikes); "possibly Ramesh" never counts as recognised. Watch-list
  people are always urgent; recognised household members can make Home-mode
  alerts quieter, never Away or Night ones. Delete a person or "forget all
  faces" at any time; unnamed faces expire after 7 days. With a cloud vision
  model, faces are blurred before any picture leaves the house.

### Added - local AI service (LightNVR Vision)
- The ai-worker is rewritten on ONNX Runtime (no PyTorch) with images for CPU,
  Intel GPU (OpenVINO) and NVIDIA (CUDA): `ghcr.io/manojmkss/light_nvr-ai-worker`.
- **`./scripts/enable-ai.sh`** detects the hardware, starts the right image next
  to the NVR (isolated from recording, with its own CPU/RAM limit) and proves it
  with a real benchmark - a GPU that isn't actually usable (common in VMs) falls
  back to CPU. Then pick "This NVR's AI service" in Settings → AI.
- **Measure this hardware** (Settings → AI) benchmarks the AI and says, per
  feature, what your hardware can sustain for your number of cameras.
- Inference is shared fairly between cameras (priorities, newest frame wins,
  stale frames dropped) instead of one global queue; live AI stats.
- YOLO26 models are supported (`fetch-ai-models.sh yolo26n`); the model
  script takes `--volume` for a GPU PC.

### Added - one visit, one event
- **People keep an event open while they're there, even standing still** - up
  to 15 minutes. If pixel motion goes quiet but the AI can still see the
  person, the event carries on instead of ending and starting a new one. The
  summary says how long they waited ("person #1 stood still ~4m 50s").
- **Parked vehicles.** A vehicle that stops for 20s is marked parked: it no
  longer keeps events open or triggers alerts, and it's remembered between
  events, so the car in the driveway isn't "seen" again whenever someone walks
  past. The timeline notes when it parks, starts moving, or leaves.
- **"Another person arrived" alert** when a second person shows up 20s or more
  into an event that already alerted - the motion cooldown no longer hides a
  second visitor.
- Long waits that cross the 10-minute clip limit say "Clip: ... (2 parts)".

### Changed
- **AI respects motion zones.** People (by where their feet are) and vehicles
  outside the watched area - the street, a neighbour's porch - are ignored:
  they don't alert, count or keep events open.
- Movement is judged relative to the object's size, so fidgeting near the
  camera isn't "walking" and a distant walker isn't "standing still".
- A vehicle the model calls "car" one moment and "truck" the next is one
  vehicle.
- While everyone in view is standing still, the AI checks every 2s instead of
  every 0.5s, to save CPU during long waits.
- The AI hold no longer lapses when inference is briefly slow.
- Tracking follows people through partial occlusion (weak detections extend
  known tracks) and predicts fast walkers' positions.

### Fixed
- A spot where something was standing when the camera started (or a car that
  drove off) could keep "motion" alive for two minutes and merge separate
  visits; with AI working, the AI now decides whether someone is still there.
- Motion clip times could drift from the footage under heavy CPU load.
- The version shown in the sidebar was always "1.0.0".

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

[Unreleased]: https://github.com/manojmkss/Light_NVR/compare/v0.4.5...HEAD
[0.4.5]: https://github.com/manojmkss/Light_NVR/compare/v0.4.4...v0.4.5
[0.4.4]: https://github.com/manojmkss/Light_NVR/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/manojmkss/Light_NVR/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/manojmkss/Light_NVR/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/manojmkss/Light_NVR/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/manojmkss/Light_NVR/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/manojmkss/Light_NVR/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/manojmkss/Light_NVR/compare/v0.2.5...v0.3.0
[0.2.5]: https://github.com/manojmkss/Light_NVR/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/manojmkss/Light_NVR/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/manojmkss/Light_NVR/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/manojmkss/Light_NVR/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/manojmkss/Light_NVR/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/manojmkss/Light_NVR/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/manojmkss/Light_NVR/releases/tag/v0.1.0
