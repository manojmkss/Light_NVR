import { useCallback, useEffect, useRef, useState } from "react";
import { authenticatedEndpoints, type CameraTileEndpoints, type TileCamera, type TileRecording } from "../api/liveEndpoints";
import { recordingNeedsTranscode } from "../api/recordings";
import type { StreamStat } from "../api/types";
import { useSettings } from "../context/SettingsContext";
import { StatusBadge } from "./StatusBadge";

const THIRTY_MIN_MS = 30 * 60 * 1000;
const LIVE_EDGE_MS = 3000; // within 3s of "now" counts as live

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
function recEndMs(rec: TileRecording): number {
  if (rec.ended_at) return new Date(rec.ended_at).getTime();
  return recStartMs(rec) + (rec.duration_seconds ?? 0) * 1000;
}
function findCovering(recs: TileRecording[], ts: number): TileRecording | null {
  return recs.find((r) => recStartMs(r) <= ts && ts <= recEndMs(r)) ?? null;
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
  const [playbackRec, setPlaybackRec] = useState<TileRecording | null>(null);
  const [videoPaused, setVideoPaused] = useState(false);
  const [snapshotSrc, setSnapshotSrc] = useState("");
  const [playbackRes, setPlaybackRes] = useState("");

  const [recentRecs, setRecentRecs] = useState<TileRecording[]>([]);
  // The segment currently being written for this camera, if any - covers the
  // gap between "now" and the last finalized recording, which otherwise has
  // no Recording row yet and would wrongly read as "no recording".
  const [activeSeg, setActiveSeg] = useState<{ startedAt: number } | null>(null);
  const [showPicker, setShowPicker] = useState(false);
  const [pickerRecs, setPickerRecs] = useState<TileRecording[] | null>(null);
  const [loadingRec, setLoadingRec] = useState(false);

  // Ticking clock drives the moving 30-minute scrub window.
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const windowStart = now - THIRTY_MIN_MS;

  // Live tiles use the substream; a maximised / fullscreen tile upgrades to the
  // main stream for full detail.
  const activeQuality: "sub" | "main" = isFullscreen ? "main" : quality;

  // Keep a small rolling list of recent recordings for the scrubber.
  useEffect(() => {
    let cancelled = false;
    const load = () =>
      endpoints
        .listRecordings(camera.id, 15)
        .then((r) => {
          if (!cancelled) setRecentRecs(r);
        })
        .catch(() => {});
    load();
    const t = setInterval(load, 30000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [camera.id, endpoints]);

  // Whenever scrubbing lands somewhere with no finalized Recording (typically
  // the last few minutes, still being written), check whether the camera has
  // a segment actively in progress that covers it - if so we can play that
  // instead of reporting "no recording" for footage that genuinely exists.
  useEffect(() => {
    if (view !== "playback" || playbackRec) {
      setActiveSeg(null);
      return;
    }
    let cancelled = false;
    const load = () => {
      endpoints
        .getLiveSegment(camera.id)
        .then((info) => {
          if (!cancelled) setActiveSeg({ startedAt: new Date(info.started_at).getTime() });
        })
        .catch(() => {
          if (!cancelled) setActiveSeg(null);
        });
    };
    load();
    const t = setInterval(load, 5000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [view, playbackRec, camera.id, endpoints]);

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
    setView("live");
    setPlaybackTs(null);
    setPlaybackRec(null);
    setSnapshotSrc("");
  }, []);

  // Move to a point in the last 30 minutes; snapping to the live edge resumes live.
  const scrubTo = useCallback(
    (ts: number) => {
      if (ts >= now - LIVE_EDGE_MS) {
        goLive();
        return;
      }
      const rec = findCovering(recentRecs, ts);
      seeking.current = true;
      setPlaybackTs(ts);
      setPlaybackRec(rec);
      setView("playback");
      setVideoPaused(false);
    },
    [now, recentRecs, goLive],
  );

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

  // What's actually playable at playbackTs: either a finalized Recording, or
  // (when no Recording covers it) the segment currently being written, if its
  // start time reaches back far enough to cover this instant. The latter is
  // what lets the last few minutes - before that segment closes and becomes a
  // real Recording row - still play instead of reading as "no recording".
  const playSource = playbackRec
    ? {
        originMs: recStartMs(playbackRec),
        // H.265 recordings need the on-demand H.264 transcode on browsers that
        // can't play HEVC (Firefox, many Chrome). Live segments are always H.264
        // via the live path, so only finalized recordings are checked here.
        url: endpoints.recordingVideoUrl(playbackRec.id, recordingNeedsTranscode(playbackRec)),
        key: `rec-${playbackRec.id}`,
      }
    : activeSeg && playbackTs != null && playbackTs >= activeSeg.startedAt
      ? { originMs: activeSeg.startedAt, url: endpoints.liveSegmentVideoUrl(camera.id), key: `seg-${activeSeg.startedAt}` }
      : null;

  // Seek the playback element when the scrub point moves (tolerant so the
  // element's own playback doesn't fight the seek).
  useEffect(() => {
    if (view !== "playback" || !playSource || playbackTs == null) return;
    const v = videoRef.current;
    if (!v) return;
    const want = Math.max(0, (playbackTs - playSource.originMs) / 1000);
    pendingSeek.current = want;
    if (v.readyState >= 1 && Math.abs(v.currentTime - want) > 0.5) {
      seeking.current = true;
      try {
        v.currentTime = want;
      } catch {
        /* not ready */
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playbackTs, playSource?.key, playSource?.originMs, view]);

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

  const liveState =
    offline
      ? { label: "Offline", cls: "state-offline" }
      : camera.recording_mode === "continuous"
        ? { label: "Recording", cls: "state-recording" }
        : { label: "Live", cls: "state-live" };

  // Stream / resolution readout.
  const streamInfo =
    view === "playback"
      ? `recording${playbackRes ? ` · ${playbackRes}` : ""}`
      : stat && stat.width > 0
        ? `${activeQuality === "main" ? "main" : "sub"} · ${stat.width}×${stat.height}`
        : activeQuality === "main"
          ? "main"
          : "sub";

  const sliderValue = playbackTs ?? now;
  const isLive = view === "live";

  return (
    <div ref={tileRef} className="camera-tile" onDoubleClick={enterFullscreen}>
      {/* ── Media ── */}
      {offline ? (
        <div className="tile-empty">{camera.name}: offline</div>
      ) : view === "playback" && playSource ? (
        <video
          key={playSource.key}
          ref={videoRef}
          autoPlay
          className="tile-video"
          src={playSource.url}
          onLoadedMetadata={() => {
            const v = videoRef.current;
            if (!v) return;
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
              setPlaybackTs(playSource.originMs + v.currentTime * 1000);
            }
          }}
          onSeeked={() => {
            const v = videoRef.current;
            if (v && Math.abs(v.currentTime - pendingSeek.current) < 0.4) seeking.current = false;
          }}
          onEnded={goLive}
        />
      ) : view === "playback" ? (
        <div className="tile-empty">No recording at this moment</div>
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
            title="Scrub the last 30 minutes"
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
                    seeking.current = true;
                    setPlaybackRec(rec);
                    setPlaybackTs(recStartMs(rec));
                    setView("playback");
                    setVideoPaused(false);
                    setShowPicker(false);
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
