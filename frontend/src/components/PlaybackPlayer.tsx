import { useEffect, useMemo, useRef, useState } from "react";
import { recordingPlaybackUrl } from "../api/recordings";
import type { Recording } from "../api/types";
import { useSettings } from "../context/SettingsContext";
import { usePageVisible } from "../hooks/usePageVisible";

function recStartMs(rec: Recording): number {
  return new Date(rec.started_at).getTime();
}
function recEndMs(rec: Recording): number {
  if (rec.ended_at) return new Date(rec.ended_at).getTime();
  return recStartMs(rec) + (rec.duration_seconds ?? 0) * 1000;
}
function findCovering(recs: Recording[], ms: number): Recording | null {
  for (const r of recs) {
    if (recStartMs(r) <= ms && ms <= recEndMs(r)) return r;
  }
  return null;
}

interface Props {
  cameraName: string;
  recordings: Recording[];
  cursor: number;
  playing: boolean;
  speed: number;
  direction: number;
  onPlayheadTime: (ms: number) => void;
  /** Lets the page reach the element (e.g. to grab a snapshot frame). */
  videoRef?: React.RefObject<HTMLVideoElement | null>;
}

/**
 * The single, large playback video for the currently-focused camera. It's the
 * only <video> the page mounts, so only one camera is ever transcoded at a time
 * - the other cameras contribute just their (cheap) coverage timelines. Its
 * real position drives the shared cursor (it's always the transport clock), so
 * every timeline tracks what this player is actually showing.
 */
export function PlaybackPlayer({
  cameraName, recordings, cursor, playing, speed, direction, onPlayheadTime, videoRef: externalVideoRef,
}: Props) {
  const { fmtTime } = useSettings();
  const pageVisible = usePageVisible();
  const internalVideoRef = useRef<HTMLVideoElement>(null);
  // Both are "possibly null" refs; the cast is only to satisfy the `ref` prop's
  // non-null RefObject signature.
  const videoRef = (externalVideoRef ?? internalVideoRef) as React.RefObject<HTMLVideoElement>;
  const wantRef = useRef(0);
  const [activeRec, setActiveRec] = useState<Recording | null>(null);

  const covering = useMemo(() => findCovering(recordings, cursor), [recordings, cursor]);

  // Keep the element aligned to the shared cursor every tick.
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

    if (activeRec?.id !== covering.id) {
      setActiveRec(covering); // src swaps; seek happens on loadedmetadata
      return;
    }
    if (v.readyState < 1) return;

    const nativeMode = playing && direction > 0 && speed > 0 && speed <= 4;
    if (nativeMode) {
      v.playbackRate = speed;
      // As the clock, the cursor tracks the element itself, so this practically
      // never fires and the video plays uninterrupted.
      if (Math.abs(v.currentTime - want) > 1.2) {
        try {
          v.currentTime = want;
        } catch {
          /* not ready */
        }
      }
      if (v.paused) v.play().catch(() => {});
    } else {
      // Paused / reverse / >4x: slave the element - pause + seek precisely.
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
  }, [cursor, playing, speed, direction, covering, activeRec]);

  // Report the playing element's real position so the shared cursor follows
  // actual playback (smooth, drift-free) instead of a free timer.
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    const nativeMode = playing && direction > 0 && speed > 0 && speed <= 4;
    if (!nativeMode || !covering) return;

    const recStart = recStartMs(covering);
    const report = () => {
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
  }, [playing, speed, direction, covering, onPlayheadTime]);

  const playIfNeeded = () => {
    const v = videoRef.current;
    if (!v) return;
    const nativeMode = playing && direction > 0 && speed > 0 && speed <= 4;
    if (nativeMode && v.paused) v.play().catch(() => {});
  };

  const label = activeRec ? `${fmtTime(activeRec.started_at)} · ${activeRec.trigger}` : "No recording at this moment";

  return (
    <div className="pb-player">
      <video
        ref={videoRef}
        muted
        playsInline
        className="pb-player-video"
        // Drop the source while the tab is hidden so the transcode request is
        // aborted (server kills its ffmpeg) instead of churning on a hidden tab.
        src={activeRec && pageVisible ? recordingPlaybackUrl(activeRec) : undefined}
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
          // Nudge the cursor past this clip so the page's gap-skip jumps to the
          // next recording instead of stalling at the end.
          if (covering) onPlayheadTime(recEndMs(covering) + 200);
        }}
      />
      {!activeRec && <div className="pb-player-nosignal">No recording at this moment</div>}
      <div className="pb-player-label">
        <b>{cameraName}</b>
        <span className="pb-player-sub"> {label}</span>
      </div>
    </div>
  );
}
