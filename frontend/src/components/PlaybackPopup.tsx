import { useCallback, useEffect, useRef, useState } from "react";
import { authenticatedEndpoints, type PlaybackAnswer } from "../api/liveEndpoints";
import { recordingNeedsTranscode } from "../api/recordings";
import { useSettings } from "../context/SettingsContext";

const LEAD_IN_MS = 3000; // start a little before the moment, to see them arrive

interface Props {
  cameraId: number;
  cameraName?: string;
  /** The moment to show (ms since epoch). */
  at: number;
  title?: string;
  onClose: () => void;
}

interface Playing {
  url: string;
  originMs: number;
  endMs: number;
  growing: boolean;
}

/** A popup that plays one camera from a moment in time - opened by clicking a
 *  face or a visit picture. Uses the same "what plays at this moment?" answer
 *  as the live view, so it works for finished recordings and for the segment
 *  still being recorded, and carries on into the next segment. */
export function PlaybackPopup({ cameraId, cameraName, at, title, onClose }: Props) {
  const { fmtDateTime, fmtTime } = useSettings();
  const videoRef = useRef<HTMLVideoElement>(null);
  const seekTo = useRef(0);
  const [playing, setPlaying] = useState<Playing | null>(null);
  const [position, setPosition] = useState(at - LEAD_IN_MS);
  const [message, setMessage] = useState<string | null>("Loading recording…");
  const seq = useRef(0);

  const load = useCallback(
    async (ts: number, continuing = false) => {
      const mine = ++seq.current;
      let a: PlaybackAnswer;
      try {
        a = await authenticatedEndpoints.resolvePlayback(cameraId, ts / 1000);
      } catch {
        if (mine === seq.current) setMessage("Couldn't reach the NVR.");
        return;
      }
      if (mine !== seq.current) return;
      if (!a.found || a.origin == null || a.end == null) {
        const next = a.next_at != null ? a.next_at * 1000 : null;
        if (next != null && next - ts < 15000) {
          load(next + 250, continuing);
          return;
        }
        setPlaying(null);
        setMessage(
          continuing
            ? "End of the recorded footage."
            : "No recording of this moment - it may have been deleted by retention, or this camera wasn't recording then.",
        );
        return;
      }
      const url =
        a.kind === "recording"
          ? authenticatedEndpoints.recordingVideoUrl(a.recording_id!, recordingNeedsTranscode({ codec: a.codec }))
          : authenticatedEndpoints.snapshotVideoUrl(cameraId, a.snapshot!);
      const origin = a.origin * 1000;
      seekTo.current = Math.max(0, (Math.min(ts, a.end * 1000 - 1500) - origin) / 1000);
      setMessage(null);
      setPlaying({ url, originMs: origin, endMs: a.end * 1000, growing: !!a.growing });
    },
    [cameraId],
  );

  useEffect(() => {
    load(at - LEAD_IN_MS);
  }, [at, load]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const onEnded = () => {
    const v = videoRef.current;
    if (!playing || !v) return;
    const reached = playing.originMs + v.duration * 1000;
    load(playing.growing ? reached : Math.max(reached, playing.endMs) + 250, true);
  };

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.75)", zIndex: 1000,
        display: "flex", alignItems: "center", justifyContent: "center", padding: 16,
      }}
    >
      <div
        className="card"
        onClick={(e) => e.stopPropagation()}
        style={{ width: "min(960px, 100%)", padding: 12, display: "grid", gap: 8 }}
      >
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <strong style={{ flex: 1 }}>
            {title ? `${title} · ` : ""}
            {cameraName ?? `Camera ${cameraId}`} · {fmtDateTime(at)}
          </strong>
          <span style={{ fontSize: 13, color: "var(--text-dim)", fontVariantNumeric: "tabular-nums" }}>
            {playing ? fmtTime(position) : ""}
          </span>
          <button className="btn btn-sm" onClick={onClose}>Close</button>
        </div>
        {playing ? (
          <video
            key={playing.url}
            ref={videoRef}
            src={playing.url}
            autoPlay
            controls
            playsInline
            style={{ width: "100%", maxHeight: "75vh", background: "#000", borderRadius: 6 }}
            onLoadedMetadata={() => {
              const v = videoRef.current;
              if (v) v.currentTime = seekTo.current;
            }}
            onTimeUpdate={() => {
              const v = videoRef.current;
              if (v) setPosition(playing.originMs + v.currentTime * 1000);
            }}
            onEnded={onEnded}
            onError={() => setMessage("This recording couldn't be played.")}
          />
        ) : (
          <div style={{ padding: 40, textAlign: "center", color: "var(--text-dim)", background: "#000", borderRadius: 6 }}>
            {message}
          </div>
        )}
      </div>
    </div>
  );
}
