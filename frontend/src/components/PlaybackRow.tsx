import { useMemo, useState } from "react";
import { clipExportUrl } from "../api/recordings";
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
  clipIn: number | null;
  clipOut: number | null;
  focused: boolean;
  /** Export clips re-encoded to H.264 (browser-friendly) vs the original codec. */
  exportH264: boolean;
  onFocus: () => void;
  onSeek: (ms: number) => void;
  onView: (start: number, end: number) => void;
  onClip: (inMs: number | null, outMs: number | null) => void;
}

/**
 * One camera's timeline row in the playback stack: a colour-coded coverage
 * timeline plus a day summary and a per-camera clip download. It holds no
 * <video> - the single shared PlaybackPlayer shows whichever row is focused -
 * so adding a camera to the review costs only a timeline, never a transcode.
 */
export function PlaybackRow(props: PlaybackRowProps) {
  const {
    camera, recordings, events, dayStart, dayEnd, viewStart, viewEnd,
    cursor, clipIn, clipOut, focused, exportH264, onFocus, onSeek, onView, onClip,
  } = props;

  const { timezone } = useSettings();

  // Per-camera day summary shown under the timeline.
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

  const clipValid = clipIn != null && clipOut != null && clipOut > clipIn;
  const [downloading, setDownloading] = useState(false);

  const downloadClip = () => {
    if (!clipValid || downloading) return;
    const filename = clipFilename(camera.name, clipIn!, clipOut!, timezone);
    const url = clipExportUrl(camera.id, Math.floor(clipIn! / 1000), Math.floor(clipOut! / 1000), filename, exportH264);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${filename}.mp4`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // The request runs while ffmpeg stitches (and maybe re-encodes) the clip;
    // show a brief busy state so a second click doesn't kick off a duplicate.
    setDownloading(true);
    setTimeout(() => setDownloading(false), exportH264 ? 8000 : 4000);
  };

  return (
    <div className={`pb-trow${focused ? " pb-trow-focused" : ""}`}>
      <div className="pb-trow-head">
        <button
          className={`btn btn-sm pb-trow-view${focused ? " btn-primary" : ""}`}
          onClick={onFocus}
          title={focused ? "Now showing in the player above" : "Show this camera in the player"}
        >
          {focused ? "▶ Viewing" : "View"}
        </button>
        <span className="pb-trow-name">{camera.name}</span>
        <span className="pb-trow-info">
          <span title="Recorded segments this day">🎞 {stats.clips}</span>
          <span title="Total recorded footage this day">⏺ {fmtRecorded(stats.recordedMs)}</span>
          <span title="Motion events this day">🟧 {events.length}</span>
          <span className="pb-coverage" title="Share of the day with recordings">{stats.coverage}%</span>
        </span>
        <button
          className="tile-ctrl-btn pb-trow-clip"
          disabled={!clipValid || downloading}
          onClick={downloadClip}
          title={clipValid ? "Download the selected clip for this camera" : "Set In and Out on the timeline first"}
        >
          {downloading ? "Preparing…" : "⤓ Clip"}
        </button>
      </div>
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
    </div>
  );
}
