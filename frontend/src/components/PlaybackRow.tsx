import { useEffect, useMemo, useRef, useState } from "react";
import { clipExportUrl, recordingPlaybackUrl } from "../api/recordings";
import type { Camera, NvrEvent, Recording } from "../api/types";
import { useSettings } from "../context/SettingsContext";
import { partsInTz } from "../utils/datetime";
import { TimelineCanvas } from "./TimelineCanvas";

function recStartMs(rec: Recording): number {
  return new Date(rec.started_at).getTime();
}
function recEndMs(rec: Recording): number {
  if (rec.ended_at) return new Date(rec.ended_at).getTime();
  return recStartMs(rec) + (rec.duration_seconds ?? 0) * 1000;
}

// The recording that covers a wall-clock instant, if any (gaps return null).
function findCovering(recs: Recording[], ms: number): Recording | null {
  for (const r of recs) {
    if (recStartMs(r) <= ms && ms <= recEndMs(r)) return r;
  }
  return null;
}

function pad(n: number): string {
  return n.toString().padStart(2, "0");
}

// Filename like "Front Door_20260701_183000-184500" (backend appends .mp4 and
// sanitises); timestamps are in the NVR's display timezone.
function clipFilename(name: string, inMs: number, outMs: number, tz?: string): string {
  const s = partsInTz(inMs, tz);
  const e = partsInTz(outMs, tz);
  const day = `${s.year}${pad(s.month)}${pad(s.day)}`;
  const t1 = `${pad(s.hour)}${pad(s.minute)}${pad(s.second)}`;
  const t2 = `${pad(e.hour)}${pad(e.minute)}${pad(e.second)}`;
  return `${name}_${day}_${t1}-${t2}`;
}

function fmtRecorded(ms: number): string {
  const totalMin = Math.round(ms / 60000);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

interface PlaybackRowProps {
  camera: Camera;
  recordings: Recording[];
  events: NvrEvent[];
  dayStart: number;
  dayEnd: number;
  viewStart: number;
  viewEnd: number;
  cursor: number;
  playing: boolean;
  speed: number;
  direction: number; // +1 forward, -1 reverse
  clipIn: number | null;
  clipOut: number | null;
  // When true this row's playing video is the shared timebase: its real
  // position drives the cursor (via onPlayheadTime) instead of a free-running
  // timer seeking the element, which is what stalled playback.
  isClock: boolean;
  onSeek: (ms: number) => void;
  onView: (start: number, end: number) => void;
  onClip: (inMs: number | null, outMs: number | null) => void;
  onPlayheadTime: (ms: number) => void;
}

/**
 * One camera in the multi-cam playback stack: a slaved <video> plus its own
 * colour-coded timeline. The video is driven entirely by the `cursor` prop -
 * the parent's transport clock is the single source of truth, so every row
 * stays locked to the same instant. Normal forward speeds (<=4x) let the
 * element play natively for smoothness; reverse and 16x/64x fall back to
 * seek-scrubbing, which is how browsers can "play" backwards or very fast at
 * all.
 */
export function PlaybackRow(props: PlaybackRowProps) {
  const {
    camera, recordings, events, dayStart, dayEnd, viewStart, viewEnd,
    cursor, playing, speed, direction, clipIn, clipOut, isClock, onSeek, onView, onClip, onPlayheadTime,
  } = props;

  const { fmtTime, timezone } = useSettings();
  const videoRef = useRef<HTMLVideoElement>(null);
  const wantRef = useRef(0);
  const [activeRec, setActiveRec] = useState<Recording | null>(null);

  const covering = useMemo(() => findCovering(recordings, cursor), [recordings, cursor]);

  // Keep the <video> element aligned to the shared cursor on every tick.
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;

    if (!covering) {
      if (!v.paused) v.pause();
      if (activeRec !== null) setActiveRec(null);
      return;
    }

    const want = Math.max(0, (cursor - recStartMs(covering)) / 1000);
    wantRef.current = want;

    // Switching source: let React swap src, seek happens on loadedmetadata.
    if (activeRec?.id !== covering.id) {
      setActiveRec(covering);
      return;
    }
    if (v.readyState < 1) return; // metadata not ready yet

    const nativeMode = playing && direction > 0 && speed > 0 && speed <= 4;
    if (nativeMode) {
      v.playbackRate = speed;
      // Seek only on a real desync (manual jump, clip change, or a follower
      // drifting), NOT every tick. For the clock camera the cursor tracks the
      // element itself, so this practically never fires and the video plays
      // uninterrupted; continuous seeking is exactly what stopped it playing.
      if (Math.abs(v.currentTime - want) > (isClock ? 1.2 : 0.8)) {
        try {
          v.currentTime = want;
        } catch {
          /* not ready */
        }
      }
      if (v.paused) v.play().catch(() => {});
    } else {
      // Paused / reverse / >4x: the element is a slave - pause it and seek
      // precisely so the shown frame matches the cursor.
      if (!v.paused) v.pause();
      v.playbackRate = 1;
      if (Math.abs(v.currentTime - want) > 0.05) {
        try {
          v.currentTime = want;
        } catch {
          /* not ready */
        }
      }
    }
  }, [cursor, playing, speed, direction, covering, activeRec, isClock]);

  // Clock camera: report the playing element's real position so the shared
  // cursor follows actual playback (smooth, drift-free) instead of a timer.
  useEffect(() => {
    const v = videoRef.current;
    if (!v || !isClock) return;
    const nativeMode = playing && direction > 0 && speed > 0 && speed <= 4;
    if (!nativeMode || !covering) return;

    const recStart = recStartMs(covering);
    const report = () => {
      // Don't report while a seek is in flight - the element's currentTime is
      // still the pre-seek value and would fight a manual timeline jump.
      if (!v.paused && !v.seeking) onPlayheadTime(recStart + v.currentTime * 1000);
    };

    const vfc = v as HTMLVideoElement & {
      requestVideoFrameCallback?: (cb: () => void) => number;
      cancelVideoFrameCallback?: (h: number) => void;
    };
    if (vfc.requestVideoFrameCallback) {
      let handle = 0;
      const cb = () => {
        report();
        handle = vfc.requestVideoFrameCallback!(cb);
      };
      handle = vfc.requestVideoFrameCallback(cb);
      return () => vfc.cancelVideoFrameCallback?.(handle);
    }
    let raf = 0;
    const loop = () => {
      report();
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [isClock, playing, speed, direction, covering, onPlayheadTime]);

  const label = activeRec
    ? `${fmtTime(activeRec.started_at)} · ${activeRec.trigger}`
    : "No recording at this time";

  // Per-camera day summary shown under the timeline (fills the space next to
  // the video and gives an at-a-glance sense of coverage).
  const stats = useMemo(() => {
    let recordedMs = 0;
    for (const r of recordings) {
      const s = Math.max(dayStart, recStartMs(r));
      const e = Math.min(dayEnd, recEndMs(r));
      if (e > s) recordedMs += e - s;
    }
    const coverage = Math.round((recordedMs / (dayEnd - dayStart)) * 100);
    return { clips: recordings.length, recordedMs, coverage };
  }, [recordings, dayStart, dayEnd]);

  // Start playback straight from the element's own readiness events so it never
  // depends on the cursor-driven effect happening to re-run at the right moment.
  const playIfNeeded = () => {
    const v = videoRef.current;
    if (!v) return;
    const nativeMode = playing && direction > 0 && speed > 0 && speed <= 4;
    if (nativeMode && v.paused) v.play().catch(() => {});
  };

  const clipValid = clipIn != null && clipOut != null && clipOut > clipIn;
  const [downloading, setDownloading] = useState(false);

  const downloadClip = () => {
    if (!clipValid || downloading) return;
    const filename = clipFilename(camera.name, clipIn!, clipOut!, timezone);
    const url = clipExportUrl(camera.id, Math.floor(clipIn! / 1000), Math.floor(clipOut! / 1000), filename);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${filename}.mp4`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // The request runs while ffmpeg stitches the clip; show a brief busy state
    // so a second click doesn't kick off a duplicate export.
    setDownloading(true);
    setTimeout(() => setDownloading(false), 4000);
  };

  return (
    <div className="pb-row">
      <div className="pb-row-video">
        <video
          ref={videoRef}
          muted
          playsInline
          className="tile-video"
          src={activeRec ? recordingPlaybackUrl(activeRec) : undefined}
          onLoadedMetadata={() => {
            const v = videoRef.current;
            if (v) {
              try {
                v.currentTime = wantRef.current;
              } catch {
                /* ignore */
              }
            }
            playIfNeeded();
          }}
          onCanPlay={playIfNeeded}
          onEnded={() => {
            // Push the cursor just past this clip so the page's gap-skip jumps
            // to the next recording instead of the clock stalling at the end.
            if (isClock && covering) onPlayheadTime(recEndMs(covering) + 200);
          }}
        />
        {!activeRec && <div className="pb-row-nosignal">No recording</div>}
        <div className="pb-row-name">
          <span>
            {camera.name}
            <span className="pb-row-sub"> {label}</span>
          </span>
          <button
            className="tile-ctrl-btn"
            disabled={!clipValid || downloading}
            onClick={downloadClip}
            title={clipValid ? "Download the selected clip for this camera" : "Set In and Out on the timeline first"}
          >
            {downloading ? "Preparing…" : "⤓ Clip"}
          </button>
        </div>
      </div>
      <div className="pb-row-right">
      <TimelineCanvas
        recordings={recordings}
        events={events}
        timezone={timezone}
        dayStart={dayStart}
        dayEnd={dayEnd}
        viewStart={viewStart}
        viewEnd={viewEnd}
        cursor={cursor}
        clipIn={clipIn}
        clipOut={clipOut}
        onSeek={onSeek}
        onView={onView}
        onClip={onClip}
      />
        <div className="pb-row-info">
          <span title="Recorded segments this day">🎞 {stats.clips} clips</span>
          <span title="Total recorded footage this day">⏺ {fmtRecorded(stats.recordedMs)}</span>
          <span title="Motion events this day">🟧 {events.length} motion</span>
          <span className="pb-coverage" title="Share of the day with recordings">
            {stats.coverage}% coverage
          </span>
        </div>
      </div>
    </div>
  );
}
