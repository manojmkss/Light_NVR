import { useCallback, useEffect, useRef, useState } from "react";
import {
  authenticatedEndpoints,
  type CameraTileEndpoints,
  type TileCamera,
  type TileRecording,
} from "../api/liveEndpoints";
import type { StreamStat } from "../api/types";
import { useSettings } from "../context/SettingsContext";
import { MediaPlayer, type MediaMode, type MediaPlayerHandle, mediaEngineSupported } from "./MediaPlayer";
import { StatusBadge } from "./StatusBadge";

// How far back the live tile's scrub bar reaches. Not a retention/deletion
// limit - older footage is still available via the "Browse older recordings"
// picker and the Recordings page; this is just the quick-scrub window.
const SCRUB_WINDOW_MS = 60 * 60 * 1000;
const LIVE_EDGE_MS = 3000; // within 3s of "now" counts as live
// Playback that catches up to within this of "now" hands over to live (the
// newest few seconds are still inside ffmpeg's current fragment).
const CATCH_UP_MS = 8000;
const RESOLVE_DEBOUNCE_MS = 200;
const TIMELINE_POLL_MS = 15000;

// Backstop interval for re-requesting a live tile's MJPEG stream. Deliberately
// long: every reconnect costs a visible hitch in the picture, and the server
// now holds streams open across camera hiccups (see live_media's keep-alive),
// so this only has to catch rarer silent deaths - a proxy dropping the
// connection, a wedged decoder. A dropped connection raises onerror and is
// already retried within seconds, so nothing depends on this being frequent.
const STREAM_WATCHDOG_MS = 15 * 60 * 1000;
// A live stream this device couldn't play (no H.265 decoder, say) is tried
// again after this long; pictures in the meantime.
const VIDEO_RETRY_MS = 2 * 60 * 1000;

function fmtDuration(seconds: number | null): string {
  if (!seconds) return "";
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function fmtBitrate(kbps: number): string {
  if (kbps >= 1000) return `${(kbps / 1000).toFixed(1)} Mbps`;
  return `${Math.round(kbps)} kbps`;
}

// How far back the scrubber has moved from "now", as mm:ss - this is what
// tells the user how much time they're shifting, at a glance, without doing
// clock math against the absolute time.
function fmtOffset(deltaMs: number): string {
  const totalSec = Math.max(0, Math.round(deltaMs / 1000));
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function recStartMs(rec: TileRecording): number {
  return new Date(rec.started_at).getTime();
}

/** Track colour for the scrub bar: brighter where there is footage. */
function coverageBackground(spans: [number, number][], start: number, end: number): string | undefined {
  if (!spans.length) return undefined;
  const gap = "rgba(255, 255, 255, 0.12)";
  const have = "rgba(255, 255, 255, 0.45)";
  const pct = (t: number) => `${(((t - start) / (end - start)) * 100).toFixed(2)}%`;
  const stops: string[] = [];
  let cursor = start;
  for (const [a, b] of spans) {
    const s = Math.max(a, start);
    const e = Math.min(b, end);
    if (e <= s) continue;
    if (s > cursor) stops.push(`${gap} ${pct(cursor)}`, `${gap} ${pct(s)}`);
    stops.push(`${have} ${pct(s)}`, `${have} ${pct(e)}`);
    cursor = e;
  }
  if (cursor < end) stops.push(`${gap} ${pct(cursor)}`, `${gap} 100%`);
  return `linear-gradient(to right, ${stops.join(", ")})`;
}

const FullscreenIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3" />
  </svg>
);
const ReduceIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M4 8h3a2 2 0 0 0 2-2V3M20 8h-3a2 2 0 0 1-2-2V3M4 16h3a2 2 0 0 1 2 2v3M20 16h-3a2 2 0 0 0-2 2v3" />
  </svg>
);
const PlayIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
    <path d="M8 5v14l11-7z" />
  </svg>
);
const PauseIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
    <path d="M6 5h4v14H6zM14 5h4v14h-4z" />
  </svg>
);
const RecIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <rect x="2" y="2" width="20" height="20" rx="2.18" />
    <line x1="7" y1="2" x2="7" y2="22" />
    <line x1="17" y1="2" x2="17" y2="22" />
    <line x1="2" y1="12" x2="22" y2="12" />
  </svg>
);

interface CameraTileProps {
  camera: TileCamera;
  quality?: "sub" | "main";
  stat?: StreamStat | null;
  /** Which API surface this tile talks to - defaults to the authenticated
   *  Live View endpoints; Kiosk supplies its own token-scoped set so the same
   *  component works unmodified against the public kiosk API. */
  endpoints?: CameraTileEndpoints;
}

type View = "live" | "paused" | "playback";

/** Why nothing is playing in playback view. */
type Gap = { kind: "none"; nextAt: number | null } | { kind: "error"; message: string };

export function CameraTile({ camera, quality = "sub", stat, endpoints = authenticatedEndpoints }: CameraTileProps) {
  const { fmtTime } = useSettings();
  const tileRef = useRef<HTMLDivElement>(null);
  const playerRef = useRef<MediaPlayerHandle>(null);

  const [errorCount, setErrorCount] = useState(0);
  const [streamKey, setStreamKey] = useState(0); // picture-stream reconnects
  const [isFullscreen, setIsFullscreen] = useState(false);

  const [view, setView] = useState<View>("live");
  const [playbackTs, setPlaybackTs] = useState<number | null>(null);
  const [playFrom, setPlayFrom] = useState<{ at: number; nonce: number } | null>(null);
  const [gap, setGap] = useState<Gap | null>(null);
  const [videoPaused, setVideoPaused] = useState(false);
  const [snapshotSrc, setSnapshotSrc] = useState("");
  const [videoRes, setVideoRes] = useState("");
  const [spans, setSpans] = useState<[number, number][]>([]);
  const [soundOn, setSoundOn] = useState(true);

  const [showPicker, setShowPicker] = useState(false);
  const [pickerRecs, setPickerRecs] = useState<TileRecording[] | null>(null);
  const [loadingRec, setLoadingRec] = useState(false);

  // The scrub bar runs on the NVR's clock, not this device's: recordings are
  // timestamped by the NVR, and a drifting VM or PC clock otherwise turns the
  // newest minutes into "no recording".
  const clockOffset = useRef(0);
  const serverNow = () => Date.now() + clockOffset.current;
  const noteServerClock = (serverSeconds: number) => {
    clockOffset.current = serverSeconds * 1000 - Date.now();
  };

  // Ticking clock drives the moving scrub window.
  const [now, setNow] = useState(serverNow());
  useEffect(() => {
    const t = setInterval(() => setNow(serverNow()), 1000);
    return () => clearInterval(t);
  }, []);
  const windowStart = now - SCRUB_WINDOW_MS;

  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastScrub = useRef(0);
  const playbackTsRef = useRef<number | null>(null);
  playbackTsRef.current = playbackTs;

  // Live tiles use the substream; a full-screen tile the main stream, with
  // its sound.
  const activeQuality: "sub" | "main" = isFullscreen ? "main" : quality;

  // The media engine plays everything: live and recordings, the cameras' own
  // video. Where a device can't play a stream (no H.265 decoder, say), live
  // falls back to the picture stream for a while; main falls back to sub.
  const engineOk = !!endpoints.mediaUrl && mediaEngineSupported();
  const [liveFailed, setLiveFailed] = useState<{ sub: boolean; main: boolean }>({ sub: false, main: false });
  const retryTimers = useRef<ReturnType<typeof setTimeout>[]>([]);
  useEffect(() => () => retryTimers.current.forEach(clearTimeout), []);
  const liveQuality: "sub" | "main" | null = !engineOk
    ? null
    : activeQuality === "main" && !liveFailed.main
      ? "main"
      : !liveFailed.sub
        ? "sub"
        : null;
  const liveFailedFor = (q: "sub" | "main") => {
    setLiveFailed((f) => ({ ...f, [q]: true }));
    retryTimers.current.push(setTimeout(() => setLiveFailed((f) => ({ ...f, [q]: false })), VIDEO_RETRY_MS));
  };

  // Nothing plays while the page is hidden (phone locked, app in the
  // background): nobody sees it, and on mobile data it would keep
  // downloading. Playback picks up where it was when the page is shown again.
  const [pageHidden, setPageHidden] = useState(document.visibilityState === "hidden");
  useEffect(() => {
    const onChange = () => {
      const hidden = document.visibilityState === "hidden";
      setPageHidden(hidden);
      if (!hidden && playbackTsRef.current != null) {
        setPlayFrom({ at: playbackTsRef.current, nonce: Date.now() });
      }
    };
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);

  const mode: MediaMode | null =
    !engineOk
      ? null
      : view === "playback" && playFrom
        ? // Sound comes along (it's small); the player is muted unless full screen.
          { kind: "play", camera: camera.id, at: playFrom.at / 1000, audio: true, nonce: playFrom.nonce }
        : view === "live" && liveQuality
          ? { kind: "live", camera: camera.id, quality: liveQuality, audio: liveQuality === "main" }
          : null;
  const muted = !(isFullscreen && soundOn);

  const mediaUrl = useCallback(() => endpoints.mediaUrl!(), [endpoints]);

  // Where footage exists in the window (shaded on the scrub bar).
  useEffect(() => {
    let cancelled = false;
    const load = () =>
      endpoints
        .timeline(camera.id, (serverNow() - SCRUB_WINDOW_MS) / 1000)
        .then((t) => {
          if (cancelled) return;
          noteServerClock(t.server_now);
          setSpans(t.spans.map(([a, b]) => [a * 1000, b * 1000] as [number, number]));
        })
        .catch(() => {});
    load();
    const t = setInterval(load, TIMELINE_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [camera.id, endpoints]);

  useEffect(() => {
    const onFsChange = () => {
      const fs = document.fullscreenElement === tileRef.current;
      setIsFullscreen(fs);
      if (fs) setSoundOn(true); // the tap that went full screen allows sound
    };
    document.addEventListener("fullscreenchange", onFsChange);
    return () => document.removeEventListener("fullscreenchange", onFsChange);
  }, []);

  const enterFullscreen = useCallback(() => {
    if (!document.fullscreenElement) tileRef.current?.requestFullscreen?.();
    else document.exitFullscreen?.();
  }, []);

  const goLive = useCallback(() => {
    if (debounce.current) clearTimeout(debounce.current);
    setView("live");
    setPlaybackTs(null);
    setPlayFrom(null);
    setGap(null);
    setSnapshotSrc("");
    setVideoPaused(false);
  }, []);

  /** Play the recordings from wall time `ts` (ms). */
  const playAt = useCallback((ts: number) => {
    setGap(null);
    setView("playback");
    setPlaybackTs(ts);
    setVideoPaused(false);
    lastScrub.current = Date.now();
    setPlayFrom({ at: ts, nonce: Date.now() });
  }, []);

  // Move to a point in the window; snapping to the live edge resumes live.
  const scrubTo = useCallback(
    (ts: number) => {
      if (ts >= serverNow() - LIVE_EDGE_MS) {
        goLive();
        return;
      }
      lastScrub.current = Date.now();
      setPlaybackTs(ts);
      if (debounce.current) clearTimeout(debounce.current);
      // Inside what's already loaded: just jump, no round trip.
      if (view === "playback" && !gap && playerRef.current?.seekLocal(ts)) return;
      if (view !== "playback") setView("playback");
      debounce.current = setTimeout(() => playAt(ts), RESOLVE_DEBOUNCE_MS);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [goLive, playAt, view, gap],
  );

  useEffect(() => () => {
    if (debounce.current) clearTimeout(debounce.current);
  }, []);

  const togglePlay = useCallback(() => {
    if (view === "live") {
      // Freeze on the current frame via a one-off snapshot.
      setSnapshotSrc(endpoints.snapshotUrl(camera.id, Date.now()));
      setView("paused");
    } else if (view === "paused") {
      goLive();
    } else {
      setVideoPaused((p) => !p);
    }
  }, [view, camera.id, endpoints, goLive]);

  const onTime = useCallback(
    (wallMs: number) => {
      if (Date.now() - lastScrub.current < 1500) return; // the slider leads while it's being moved
      setPlaybackTs(wallMs);
      if (wallMs >= serverNow() - LIVE_EDGE_MS) goLive();
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [goLive],
  );

  const onGap = useCallback(
    (at: number, nextAt: number | null) => {
      // Played up to the present: carry on live.
      if (nextAt == null && at * 1000 >= serverNow() - CATCH_UP_MS * 2) {
        goLive();
        return;
      }
      setGap({ kind: "none", nextAt: nextAt != null ? nextAt * 1000 : null });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [goLive],
  );

  const onFail = useCallback(
    (why: string) => {
      if (view === "playback") setGap({ kind: "error", message: why });
      else if (liveQuality) liveFailedFor(liveQuality);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [view, liveQuality],
  );

  const openPicker = useCallback(
    async (e: React.MouseEvent) => {
      e.stopPropagation();
      if (showPicker) {
        setShowPicker(false);
        return;
      }
      setShowPicker(true);
      if (pickerRecs === null) {
        setLoadingRec(true);
        try {
          setPickerRecs(await endpoints.listRecordings(camera.id, 20));
        } finally {
          setLoadingRec(false);
        }
      }
    },
    [camera.id, endpoints, pickerRecs, showPicker],
  );

  const handleError = () => {
    setErrorCount((c) => c + 1);
    setTimeout(() => setStreamKey((k) => k + 1), 3000);
  };

  const streamUrl = endpoints.streamUrl(camera.id, activeQuality, streamKey);
  const offline = camera.status === "offline" || errorCount > 2;
  const showsPictures = view === "live" && !mode;

  // Picture-stream watchdog (only when live view falls back to pictures; the
  // media engine's player watches its own stream).
  //
  // An <img> gives no per-frame signal, and a stream that simply stops - a
  // proxy dropping an idle connection, a wedged decoder, a tab that was
  // backgrounded - ends without firing onerror. Re-request it on a timer, and
  // when the page is shown again.
  useEffect(() => {
    if (!showsPictures || offline) return;
    const reconnect = () => setStreamKey((k) => k + 1);
    // Spread tiles out so a full wall doesn't reconnect in lockstep.
    const jitter = (camera.id % 7) * 1500;
    const timer = setInterval(reconnect, STREAM_WATCHDOG_MS + jitter);
    const onVisible = () => {
      if (document.visibilityState === "visible") reconnect();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [showsPictures, offline, camera.id]);

  const liveState =
    offline
      ? { label: "Offline", cls: "state-offline" }
      : camera.recording_mode === "continuous"
        ? { label: "Recording", cls: "state-recording" }
        : { label: "Live", cls: "state-live" };

  // Stream / resolution readout.
  const streamInfo =
    view === "playback"
      ? `recording${videoRes ? ` · ${videoRes}` : ""}`
      : mode?.kind === "live"
        ? `${mode.quality} · video${videoRes ? ` · ${videoRes}` : ""}`
        : stat && stat.width > 0
          ? `${activeQuality === "main" ? "main" : "sub"} · ${stat.width}×${stat.height}`
          : activeQuality === "main"
            ? "main"
            : "sub";

  const sliderValue = playbackTs ?? now;
  const isLive = view === "live";

  const gapMessage = () => {
    if (!engineOk) return <div className="tile-empty">Playing recordings needs a current browser.</div>;
    if (!gap) return null;
    if (gap.kind === "error") return <div className="tile-empty tile-over">{gap.message}</div>;
    const why =
      camera.recording_mode === "motion"
        ? "This camera only records when there's motion."
        : camera.recording_mode === "off"
          ? "Recording is turned off for this camera."
          : "The camera wasn't recording then (offline or restarting).";
    return (
      <div className="tile-empty tile-over" style={{ flexDirection: "column", gap: 8, textAlign: "center" }}>
        <div>No footage at this moment.</div>
        <div style={{ fontSize: 12, opacity: 0.8 }}>{why}</div>
        {gap.nextAt != null && gap.nextAt < now - LIVE_EDGE_MS && (
          <button
            className="tile-ctrl-btn"
            onClick={(e) => {
              e.stopPropagation();
              playAt(gap.nextAt! + 250);
            }}
          >
            Jump to next footage ({fmtTime(gap.nextAt)})
          </button>
        )}
      </div>
    );
  };

  return (
    <div ref={tileRef} className="camera-tile" onDoubleClick={enterFullscreen}>
      {/* ── Media ── */}
      {offline && view !== "playback" ? (
        <div className="tile-empty">{camera.name}: offline</div>
      ) : view === "paused" ? (
        <img src={snapshotSrc} alt={camera.name} className="tile-frozen" />
      ) : pageHidden && engineOk ? (
        <div className="tile-video" />
      ) : mode ? (
        <>
          <MediaPlayer
            ref={playerRef}
            url={mediaUrl}
            mode={mode}
            muted={muted}
            paused={view === "playback" && videoPaused}
            className="tile-video"
            onPlaying={(w, h) => {
              setErrorCount(0);
              if (w && h) setVideoRes(`${w}×${h}`);
            }}
            onFail={onFail}
            onGap={onGap}
            onTime={onTime}
            onSoundBlocked={() => setSoundOn(false)}
          />
          {view === "playback" && gapMessage()}
        </>
      ) : view === "playback" ? (
        gapMessage()
      ) : (
        <img src={streamUrl} alt={camera.name} onError={handleError} onLoad={() => setErrorCount(0)} />
      )}

      {/* ── Top bar: name + state (left), stream/bitrate + browse (right) ── */}
      <div className="tile-topbar">
        <span className="tile-name">
          <span className={`tile-state ${liveState.cls}`}>
            <i className="tile-state-dot" />
            {liveState.label}
          </span>
          {camera.name}
        </span>
        <span className="tile-topbar-right">
          {!offline && stat && stat.kbps > 0 && view !== "playback" && showsPictures && (
            <span className="tile-bitrate">{fmtBitrate(stat.kbps)}</span>
          )}
          <button className="tile-ctrl-btn" title="Browse older recordings" onClick={openPicker}>
            <RecIcon />
          </button>
          <StatusBadge status={camera.status} />
        </span>
      </div>

      {/* ── Permanent control bar (below the video) ── */}
      {(!offline || view === "playback") && (
        <div className="tile-controls" onDoubleClick={(e) => e.stopPropagation()}>
          <button
            className="tile-ctrl-btn tile-play"
            title={isLive ? "Pause (freeze)" : view === "paused" ? "Resume live" : videoPaused ? "Play" : "Pause"}
            onClick={(e) => {
              e.stopPropagation();
              togglePlay();
            }}
          >
            {isLive || (view === "playback" && !videoPaused) ? <PauseIcon /> : <PlayIcon />}
          </button>

          <input
            type="range"
            className="tile-scrub"
            min={windowStart}
            max={now}
            step={1000}
            value={sliderValue}
            style={{ background: coverageBackground(spans, windowStart, now) }}
            title="Scrub the last 60 minutes (brighter = recorded)"
            onChange={(e) => scrubTo(Number(e.target.value))}
            onClick={(e) => e.stopPropagation()}
          />

          <span
            className="tile-time"
            onClick={(e) => e.stopPropagation()}
            title={isLive ? undefined : `Recorded at ${fmtTime(playbackTs ?? now)}`}
          >
            {isLive ? (
              <span className="tile-live-tag">
                <i className="tile-live-dot" /> LIVE
              </span>
            ) : (
              `-${fmtOffset(now - (playbackTs ?? now))}`
            )}
          </span>

          <span className="tile-streaminfo" title="Stream in use and resolution">
            {streamInfo}
          </span>

          <button
            className="tile-ctrl-btn tile-golive"
            title="Jump back to live"
            disabled={isLive}
            onClick={(e) => {
              e.stopPropagation();
              goLive();
            }}
          >
            ⏭ Live
          </button>

          {isFullscreen && mode && (
            <button
              className="tile-ctrl-btn"
              title={soundOn ? "Mute" : "Sound on"}
              onClick={(e) => {
                e.stopPropagation();
                setSoundOn((s) => !s);
              }}
            >
              {soundOn ? "🔊" : "🔇"}
            </button>
          )}

          <button
            className="tile-ctrl-btn"
            title={isFullscreen ? "Exit fullscreen" : "Fullscreen"}
            onClick={(e) => {
              e.stopPropagation();
              enterFullscreen();
            }}
          >
            {isFullscreen ? <ReduceIcon /> : <FullscreenIcon />}
          </button>
        </div>
      )}

      {/* ── Recording picker (browse all) ── */}
      {showPicker && (
        <div className="tile-rec-picker" onClick={(e) => e.stopPropagation()}>
          <div className="tile-rec-picker-header">
            <span>Recordings</span>
            <button onClick={() => setShowPicker(false)}>×</button>
          </div>
          <div className="tile-rec-picker-list">
            {loadingRec ? (
              <div className="tile-rec-empty">Loading…</div>
            ) : !pickerRecs || pickerRecs.length === 0 ? (
              <div className="tile-rec-empty">No recordings yet</div>
            ) : (
              pickerRecs.map((rec) => (
                <div
                  key={rec.id}
                  className="tile-rec-item"
                  onClick={() => {
                    setShowPicker(false);
                    playAt(recStartMs(rec) + 250);
                  }}
                >
                  <span>{fmtTime(rec.started_at)}</span>
                  <span className="tile-rec-dur">{fmtDuration(rec.duration_seconds)}</span>
                </div>
              ))
            )}
          </div>
        </div>
      )}

      {isFullscreen && <div className="tile-fs-hint">Press Esc or double-click to exit fullscreen</div>}
    </div>
  );
}
