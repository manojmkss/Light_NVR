import { useCallback, useEffect, useRef, useState } from "react";
import {
  authenticatedEndpoints,
  type CameraTileEndpoints,
  type PlaybackAnswer,
  type TileCamera,
  type TileRecording,
} from "../api/liveEndpoints";
import { recordingNeedsTranscode } from "../api/recordings";
import type { StreamStat } from "../api/types";
import { useSettings } from "../context/SettingsContext";
import { StatusBadge } from "./StatusBadge";

// How far back the live tile's scrub bar reaches. Not a retention/deletion
// limit - older footage is still available via the "Browse older recordings"
// picker and the Recordings page; this is just the quick-scrub window.
const SCRUB_WINDOW_MS = 60 * 60 * 1000;
const LIVE_EDGE_MS = 3000; // within 3s of "now" counts as live
// Playback that catches up to within this of "now" hands over to live (the
// newest few seconds are still inside ffmpeg's current fragment).
const CATCH_UP_MS = 8000;
// A scrub or a finished clip that lands in a gap this short (the second or
// two between continuous segments) just carries on from the next footage.
const SMALL_GAP_MS = 15000;
const RESOLVE_DEBOUNCE_MS = 200;
const TIMELINE_POLL_MS = 15000;

// Backstop interval for re-requesting a live tile's MJPEG stream. Deliberately
// long: every reconnect costs a visible hitch in the picture, and the server
// now holds streams open across camera hiccups (see live_media's keep-alive),
// so this only has to catch rarer silent deaths - a proxy dropping the
// connection, a wedged decoder. A dropped connection raises onerror and is
// already retried within seconds, so nothing depends on this being frequent.
const STREAM_WATCHDOG_MS = 15 * 60 * 1000;

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

/** What the <video> is playing: a file plus the wall time of its first frame. */
interface Source {
  url: string;
  key: string;
  originMs: number;
  endMs: number;
  growing: boolean; // a snapshot of the segment still being recorded
}

/** Why nothing is playing in playback view. */
type Gap = { kind: "loading" } | { kind: "none"; nextAt: number | null } | { kind: "error"; message: string };

export function CameraTile({ camera, quality = "sub", stat, endpoints = authenticatedEndpoints }: CameraTileProps) {
  const { fmtTime } = useSettings();
  const tileRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const pendingSeek = useRef(0);
  // True from the moment a new scrub target is set until the element's
  // currentTime actually reaches it - while true we ignore timeupdate so the
  // pre-seek position (the recording's start) can't drag the slider backward.
  const seeking = useRef(false);

  const [errorCount, setErrorCount] = useState(0);
  const [streamKey, setStreamKey] = useState(0);
  const [isFullscreen, setIsFullscreen] = useState(false);

  const [view, setView] = useState<View>("live");
  const [playbackTs, setPlaybackTs] = useState<number | null>(null);
  const [source, setSource] = useState<Source | null>(null);
  const [gap, setGap] = useState<Gap | null>(null);
  const [videoPaused, setVideoPaused] = useState(false);
  const [snapshotSrc, setSnapshotSrc] = useState("");
  const [playbackRes, setPlaybackRes] = useState("");
  const [spans, setSpans] = useState<[number, number][]>([]);

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

  const sourceRef = useRef<Source | null>(null);
  sourceRef.current = source;
  const resolveSeq = useRef(0);
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);
  const videoErrors = useRef(0);

  // Live tiles use the substream; a maximised / fullscreen tile upgrades to the
  // main stream for full detail.
  const activeQuality: "sub" | "main" = isFullscreen ? "main" : quality;

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
    const onFsChange = () => setIsFullscreen(document.fullscreenElement === tileRef.current);
    document.addEventListener("fullscreenchange", onFsChange);
    return () => document.removeEventListener("fullscreenchange", onFsChange);
  }, []);

  const enterFullscreen = useCallback(() => {
    if (!document.fullscreenElement) tileRef.current?.requestFullscreen?.();
    else document.exitFullscreen?.();
  }, []);

  const goLive = useCallback(() => {
    resolveSeq.current += 1; // drop any answer still on its way
    if (debounce.current) clearTimeout(debounce.current);
    setView("live");
    setPlaybackTs(null);
    setSource(null);
    setGap(null);
    setSnapshotSrc("");
  }, []);

  /** Seek the element already showing `src` (no reload) to wall time `ts`. */
  const seekWithin = (src: Source, ts: number) => {
    const want = Math.max(0, (ts - src.originMs) / 1000);
    pendingSeek.current = want;
    const v = videoRef.current;
    if (v && v.readyState >= 1) {
      seeking.current = true;
      try {
        v.currentTime = want;
      } catch {
        /* not ready - onLoadedMetadata applies pendingSeek */
      }
      if (v.paused && !videoPaused) v.play().catch(() => {});
    }
  };

  /** Ask the NVR what plays at `ts` and start it. `continuing`: called because
   *  the previous file ended, so small gaps are skipped and the live edge
   *  hands over to live instead of showing "no footage". */
  const resolveAt = useCallback(
    async (ts: number, continuing = false) => {
      const seq = ++resolveSeq.current;
      if (!sourceRef.current) setGap({ kind: "loading" });
      let answer: PlaybackAnswer;
      try {
        answer = await endpoints.resolvePlayback(camera.id, ts / 1000);
      } catch (e) {
        if (seq !== resolveSeq.current) return;
        setSource(null);
        setGap({ kind: "error", message: "Couldn't reach the NVR - retrying…" });
        setTimeout(() => seq === resolveSeq.current && resolveAt(ts, continuing), 3000);
        return;
      }
      if (seq !== resolveSeq.current) return;
      noteServerClock(answer.server_now);
      const nowMs = answer.server_now * 1000;

      if (!answer.found || answer.origin == null || answer.end == null) {
        const nextAt = answer.next_at != null ? answer.next_at * 1000 : null;
        if (nextAt != null && nextAt - ts <= SMALL_GAP_MS && nextAt < nowMs - LIVE_EDGE_MS) {
          setPlaybackTs(nextAt);
          resolveAt(nextAt + 250, continuing);
          return;
        }
        if (continuing && ts >= nowMs - CATCH_UP_MS * 2) {
          goLive();
          return;
        }
        setSource(null);
        setGap({ kind: "none", nextAt });
        return;
      }

      const next: Source =
        answer.kind === "recording"
          ? {
              // H.265 recordings need the on-demand H.264 copy on browsers that
              // can't play HEVC; segment snapshots prefer the H.264 sub already.
              url: endpoints.recordingVideoUrl(answer.recording_id!, recordingNeedsTranscode({ codec: answer.codec })),
              key: `rec-${answer.recording_id}`,
              originMs: answer.origin * 1000,
              endMs: answer.end * 1000,
              growing: false,
            }
          : {
              url: endpoints.snapshotVideoUrl(camera.id, answer.snapshot!),
              key: `snap-${answer.snapshot}`,
              originMs: answer.origin * 1000,
              endMs: answer.end * 1000,
              growing: !!answer.growing,
            };
      // Past the end of what's been written so far: start a little before it.
      const target = Math.min(ts, next.endMs - 1500);
      if (continuing && next.growing && target >= nowMs - CATCH_UP_MS) {
        goLive();
        return;
      }
      setGap(null);
      setPlaybackTs(target);
      if (sourceRef.current?.key === next.key) {
        seekWithin(sourceRef.current, target);
      } else {
        pendingSeek.current = Math.max(0, (target - next.originMs) / 1000);
        seeking.current = true;
        setPlaybackRes("");
        setSource(next);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [camera.id, endpoints, goLive],
  );

  // Move to a point in the window; snapping to the live edge resumes live.
  const scrubTo = useCallback(
    (ts: number) => {
      if (ts >= serverNow() - LIVE_EDGE_MS) {
        goLive();
        return;
      }
      setView("playback");
      setPlaybackTs(ts);
      setVideoPaused(false);
      videoErrors.current = 0;
      // Inside what's already loaded: just seek, no round trip.
      const cur = sourceRef.current;
      if (cur && ts >= cur.originMs && ts <= cur.endMs - 500) {
        if (debounce.current) clearTimeout(debounce.current);
        resolveSeq.current += 1;
        seekWithin(cur, ts);
        return;
      }
      if (debounce.current) clearTimeout(debounce.current);
      debounce.current = setTimeout(() => resolveAt(ts), RESOLVE_DEBOUNCE_MS);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [goLive, resolveAt],
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
      const v = videoRef.current;
      if (!v) return;
      if (v.paused) {
        v.play().catch(() => {});
        setVideoPaused(false);
      } else {
        v.pause();
        setVideoPaused(true);
      }
    }
  }, [view, camera.id, endpoints, goLive]);

  /** The file finished: carry on with whatever comes next. */
  const onEnded = () => {
    const s = sourceRef.current;
    const v = videoRef.current;
    if (!s) return;
    const reached = s.originMs + (v && isFinite(v.duration) ? v.duration * 1000 : s.endMs - s.originMs);
    // A growing segment's snapshot ends where the recording had got to: ask
    // again for a fresher one from here. A finished file: the next one.
    resolveAt(s.growing ? reached : Math.max(reached, s.endMs) + 250, true);
  };

  /** The file failed to load (moved by the storage mover, expired, ...):
   *  ask again rather than leave a dead tile; give up after a couple of tries. */
  const onVideoError = () => {
    const s = sourceRef.current;
    if (!s) return;
    videoErrors.current += 1;
    const at = playbackTs ?? s.originMs;
    if (videoErrors.current <= 2) {
      setSource(null);
      setTimeout(() => resolveAt(at), 1000);
    } else {
      setSource(null);
      setGap({ kind: "error", message: "This recording couldn't be played." });
    }
  };

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

  // Stream watchdog.
  //
  // An <img> gives no per-frame signal, and a stream that simply stops - a
  // proxy dropping an idle connection, a wedged decoder, a tab that was
  // backgrounded - ends without firing onerror, so nothing above ever learns
  // the picture is dead. Rather than leave a tile frozen until someone walks
  // past and notices, re-request the stream on a timer: it's one cheap HTTP
  // request and it guarantees any stall self-heals within the interval.
  // Reconnecting on tab-visible covers the common case of a display waking up.
  useEffect(() => {
    if (view !== "live" || offline) return;
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
  }, [view, offline, camera.id]);

  const liveState =
    offline
      ? { label: "Offline", cls: "state-offline" }
      : camera.recording_mode === "continuous"
        ? { label: "Recording", cls: "state-recording" }
        : { label: "Live", cls: "state-live" };

  // Stream / resolution readout.
  const streamInfo =
    view === "playback"
      ? `recording${source && playbackRes ? ` · ${playbackRes}` : ""}`
      : stat && stat.width > 0
        ? `${activeQuality === "main" ? "main" : "sub"} · ${stat.width}×${stat.height}`
        : activeQuality === "main"
          ? "main"
          : "sub";

  const sliderValue = playbackTs ?? now;
  const isLive = view === "live";

  const gapMessage = () => {
    if (!gap || gap.kind === "loading") return <div className="tile-empty">Loading recording…</div>;
    if (gap.kind === "error") return <div className="tile-empty">{gap.message}</div>;
    const why =
      camera.recording_mode === "motion"
        ? "This camera only records when there's motion."
        : camera.recording_mode === "off"
          ? "Recording is turned off for this camera."
          : "The camera wasn't recording then (offline or restarting).";
    return (
      <div className="tile-empty" style={{ flexDirection: "column", gap: 8, textAlign: "center" }}>
        <div>No footage at this moment.</div>
        <div style={{ fontSize: 12, opacity: 0.8 }}>{why}</div>
        {gap.nextAt != null && gap.nextAt < now - LIVE_EDGE_MS && (
          <button
            className="tile-ctrl-btn"
            onClick={(e) => {
              e.stopPropagation();
              setPlaybackTs(gap.nextAt!);
              resolveAt(gap.nextAt! + 250);
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
      {offline ? (
        <div className="tile-empty">{camera.name}: offline</div>
      ) : view === "playback" && source ? (
        <video
          key={source.key}
          ref={videoRef}
          autoPlay
          className="tile-video"
          src={source.url}
          onLoadedMetadata={() => {
            const v = videoRef.current;
            if (!v) return;
            videoErrors.current = 0;
            setPlaybackRes(`${v.videoWidth}×${v.videoHeight}`);
            try {
              v.currentTime = pendingSeek.current;
            } catch {
              /* ignore */
            }
          }}
          onTimeUpdate={() => {
            const v = videoRef.current;
            if (!v) return;
            // Until the requested seek actually lands, don't let the element's
            // (stale) position write back to the slider - that's what made a
            // click jump backward to the clip's start.
            if (seeking.current) {
              if (Math.abs(v.currentTime - pendingSeek.current) < 0.4) seeking.current = false;
              return;
            }
            if (!v.paused) {
              const ts = source.originMs + v.currentTime * 1000;
              setPlaybackTs(ts);
              if (ts >= serverNow() - LIVE_EDGE_MS) goLive();
            }
          }}
          onSeeked={() => {
            const v = videoRef.current;
            if (v && Math.abs(v.currentTime - pendingSeek.current) < 0.4) seeking.current = false;
          }}
          onEnded={onEnded}
          onError={onVideoError}
        />
      ) : view === "playback" ? (
        gapMessage()
      ) : view === "paused" ? (
        <img src={snapshotSrc} alt={camera.name} className="tile-frozen" />
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
          {!offline && stat && stat.kbps > 0 && view !== "playback" && (
            <span className="tile-bitrate">{fmtBitrate(stat.kbps)}</span>
          )}
          <button className="tile-ctrl-btn" title="Browse older recordings" onClick={openPicker}>
            <RecIcon />
          </button>
          <StatusBadge status={camera.status} />
        </span>
      </div>

      {/* ── Permanent control bar (below the video) ── */}
      {!offline && (
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
                    const ts = recStartMs(rec) + 250;
                    setView("playback");
                    setPlaybackTs(ts);
                    setVideoPaused(false);
                    setShowPicker(false);
                    resolveAt(ts);
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
