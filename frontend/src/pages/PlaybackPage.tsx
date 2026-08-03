import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { listCameras, snapshotUrl } from "../api/cameras";
import { downloadClip, listRecordings, recordingThumbnailUrl } from "../api/recordings";
import { listEvents } from "../api/system";
import type { Camera, NvrEvent, Recording } from "../api/types";
import { PlaybackPlayer } from "../components/PlaybackPlayer";
import { TimelineCanvas } from "../components/TimelineCanvas";
import { useSettings } from "../context/SettingsContext";
import { clock24, partsInTz, startOfDayInTz, todayInTz } from "../utils/datetime";

const DAY_MS = 24 * 3600 * 1000;
const SPEEDS = [0.5, 1, 2, 4, 16, 64];
const FRAME_STEP_MS = 100;
// The transport clock ticks at ~20fps: smooth enough for the playhead while
// keeping seek-scrub modes and per-camera re-renders cheap.
const TICK_MS = 50;

interface Interval {
  start: number;
  end: number;
}

function recStartMs(r: Recording): number {
  return new Date(r.started_at).getTime();
}
function recEndMs(r: Recording): number {
  if (r.ended_at) return new Date(r.ended_at).getTime();
  return recStartMs(r) + (r.duration_seconds ?? 0) * 1000;
}

function coveredAt(iv: Interval[], ms: number): boolean {
  return iv.some((i) => i.start <= ms && ms <= i.end);
}
// iv is sorted by start; the first interval starting after ms is the next clip.
function nextStartAfter(iv: Interval[], ms: number): number | null {
  for (const i of iv) if (i.start > ms) return i.start;
  return null;
}
function prevEndBefore(iv: Interval[], ms: number): number | null {
  let best: number | null = null;
  for (const i of iv) if (i.end < ms) best = best == null ? i.end : Math.max(best, i.end);
  return best;
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

function fmtDur(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

/**
 * Identifying still for a camera in the strip. People recognise a camera by
 * what it looks at, not by its name, so this prefers a poster frame from the
 * day being reviewed (that's the footage in question), falls back to the live
 * snapshot when the day has no thumbnails yet, and finally to the camera's
 * initial - so a card is never just an empty box.
 */
function CameraThumb({ camera, recordings }: { camera: Camera; recordings: Recording[] }) {
  const [stage, setStage] = useState(0); // 0 = recording poster, 1 = live, 2 = initial

  // Stable per day: picked from the day's recordings, so it doesn't flicker
  // while scrubbing. Resets when the day's recordings change.
  const posterId = useMemo(() => recordings.find((r) => r.thumbnail_path)?.id ?? null, [recordings]);
  useEffect(() => setStage(posterId ? 0 : 1), [posterId, camera.id]);

  const src = stage === 0 && posterId ? recordingThumbnailUrl(posterId) : stage === 1 ? snapshotUrl(camera.id) : null;

  if (!src) {
    return (
      <div className="pb-thumb pb-thumb-none" aria-hidden>
        {camera.name.trim().charAt(0).toUpperCase() || "?"}
      </div>
    );
  }
  return (
    <div className="pb-thumb">
      <img src={src} alt="" loading="lazy" onError={() => setStage((s) => s + 1)} />
    </div>
  );
}

/** Compact coverage bar for a camera strip card: where footage exists today. */
function CoverageBar({ recordings, dayStart, dayEnd }: { recordings: Recording[]; dayStart: number; dayEnd: number }) {
  const spans = useMemo(() => {
    const span = dayEnd - dayStart;
    return recordings.slice(0, 400).map((r) => {
      const s = Math.max(dayStart, recStartMs(r));
      const e = Math.min(dayEnd, recEndMs(r));
      return {
        left: ((s - dayStart) / span) * 100,
        width: Math.max(0.4, ((e - s) / span) * 100),
        motion: r.trigger === "motion",
      };
    });
  }, [recordings, dayStart, dayEnd]);

  return (
    <div className="pb-cov">
      {spans.map((s, i) => (
        <i
          key={i}
          className={s.motion ? "pb-cov-seg pb-cov-motion" : "pb-cov-seg"}
          style={{ left: `${s.left}%`, width: `${s.width}%` }}
        />
      ))}
    </div>
  );
}

export function PlaybackPage() {
  const { timezone } = useSettings();
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [date, setDate] = useState(() => todayInTz(timezone));

  const [recsByCam, setRecsByCam] = useState<Record<number, Recording[]>>({});
  const [eventsByCam, setEventsByCam] = useState<Record<number, NvrEvent[]>>({});

  // Day boundaries are the selected calendar date's midnight-to-midnight window
  // in the NVR's timezone, so the 24h timeline lines up with local wall time.
  const dayStart = useMemo(() => startOfDayInTz(date, timezone), [date, timezone]);
  const dayEnd = dayStart + DAY_MS;

  const [viewStart, setViewStart] = useState(dayStart);
  const [viewEnd, setViewEnd] = useState(dayEnd);

  // Transport state.
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [direction, setDirection] = useState(1);
  const [cursor, setCursor] = useState(dayStart);

  // Which camera is showing in the main player.
  const [focusedId, setFocusedId] = useState<number | null>(null);

  // Clip export markers (epoch ms) + export state.
  const [clipIn, setClipIn] = useState<number | null>(null);
  const [clipOut, setClipOut] = useState<number | null>(null);
  const [exportH264, setExportH264] = useState(false);
  const [exportingId, setExportingId] = useState<number | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const videoRef = useRef<HTMLVideoElement | null>(null);

  // Refs mirror state for the rAF loop (avoids stale closures / re-subscribes).
  const stateRef = useRef({ playing, speed, direction, dayStart, dayEnd });
  stateRef.current = { playing, speed, direction, dayStart, dayEnd };
  const cursorRef = useRef(cursor);
  cursorRef.current = cursor;
  const viewRef = useRef({ start: viewStart, end: viewEnd });
  viewRef.current = { start: viewStart, end: viewEnd };
  const intervalsRef = useRef<Interval[]>([]);
  // Timestamp of the last position report from the player; a fresh value means
  // the video is actively playing and driving the cursor itself.
  const lastReportRef = useRef(0);
  const lastSetRef = useRef(0);

  // The player reports its real position here; it steers the cursor so playback
  // follows the element instead of a free timer.
  const reportPlayhead = useCallback((ms: number) => {
    const now = performance.now();
    lastReportRef.current = now;
    cursorRef.current = ms;
    // Throttle the React state update to ~30fps so a high-fps recording can't
    // flood re-renders; cursorRef stays exact for the transport logic.
    if (now - lastSetRef.current >= 33) {
      lastSetRef.current = now;
      setCursor(ms);
    }
  }, []);

  useEffect(() => {
    listCameras().then((cams) => {
      setCameras(cams);
      setFocusedId((cur) => cur ?? cams.find((c) => c.enabled)?.id ?? null);
    });
  }, []);

  // Reset the view window and clock whenever the day changes.
  useEffect(() => {
    setViewStart(dayStart);
    setViewEnd(dayEnd);
    setCursor(dayStart);
    setPlaying(false);
  }, [dayStart, dayEnd]);

  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 6000);
    return () => clearTimeout(t);
  }, [notice]);

  const enabledCameras = useMemo(() => cameras.filter((c) => c.enabled), [cameras]);
  const focusedCam = useMemo(
    () => enabledCameras.find((c) => c.id === focusedId) ?? enabledCameras[0] ?? null,
    [enabledCameras, focusedId],
  );

  // Load recordings + motion events for every enabled camera for the day. This
  // is metadata only (timelines + coverage bars) - it costs no video decoding,
  // so showing every camera's coverage stays cheap.
  useEffect(() => {
    if (enabledCameras.length === 0) {
      setRecsByCam({});
      setEventsByCam({});
      return;
    }
    const startIso = new Date(dayStart).toISOString();
    const endIso = new Date(dayEnd).toISOString();
    let cancelled = false;

    Promise.all(
      enabledCameras.map(async (cam) => {
        const [recs, evs] = await Promise.all([
          listRecordings({ camera_id: cam.id, start: startIso, end: endIso, limit: 500 }),
          listEvents(cam.id, 500),
        ]);
        return { id: cam.id, recs, evs };
      }),
    )
      .then((results) => {
        if (cancelled) return;
        const rMap: Record<number, Recording[]> = {};
        const eMap: Record<number, NvrEvent[]> = {};
        for (const { id, recs, evs } of results) {
          rMap[id] = recs;
          eMap[id] = evs.filter((e) => {
            const t = new Date(e.created_at).getTime();
            return e.type === "motion" && t >= dayStart && t <= dayEnd;
          });
        }
        setRecsByCam(rMap);
        setEventsByCam(eMap);
      })
      .catch(() => {
        if (!cancelled) setNotice({ kind: "err", text: "Could not load recordings for this day." });
      });

    return () => {
      cancelled = true;
    };
  }, [enabledCameras, dayStart, dayEnd]);

  const focusedRecs = focusedCam ? recsByCam[focusedCam.id] ?? [] : [];

  // Gap-skipping intervals follow the focused camera (that's what's playing).
  useEffect(() => {
    const iv = focusedRecs
      .map((r) => ({ start: recStartMs(r), end: recEndMs(r) }))
      .sort((a, b) => a.start - b.start);
    intervalsRef.current = iv;
    if (iv.length && cursorRef.current <= dayStart) {
      cursorRef.current = iv[0].start;
      setCursor(iv[0].start);
    }
  }, [focusedRecs, dayStart]);

  // The transport clock: advances the playhead while playing, skips over gaps
  // with no footage, and pans the view to follow when zoomed in.
  useEffect(() => {
    let last = performance.now();
    let acc = 0;
    let raf = 0;
    const applyFollow = (c: number, dayStartV: number, dayEndV: number) => {
      const vs0 = viewRef.current.start;
      const ve0 = viewRef.current.end;
      const span = ve0 - vs0;
      if (span >= dayEndV - dayStartV) return; // full day: nothing to follow
      const margin = span * 0.12;
      let vs = vs0;
      if (c > ve0 - margin) vs = c - span * 0.3;
      else if (c < vs0 + margin) vs = c - span * 0.7;
      if (vs === vs0) return;
      let ve = vs + span;
      if (vs < dayStartV) {
        vs = dayStartV;
        ve = vs + span;
      }
      if (ve > dayEndV) {
        ve = dayEndV;
        vs = ve - span;
      }
      viewRef.current = { start: vs, end: ve };
      setViewStart(vs);
      setViewEnd(ve);
    };

    const loop = (now: number) => {
      const dt = now - last;
      last = now;
      acc += dt;
      if (acc >= TICK_MS) {
        const st = stateRef.current;
        if (st.playing) {
          const deltaMs = (acc / 1000) * st.speed * st.direction * 1000;
          const iv = intervalsRef.current;
          const isNative = st.direction > 0 && st.speed > 0 && st.speed <= 4;
          const c0 = cursorRef.current;
          const clockAlive = performance.now() - lastReportRef.current < 1000;
          if (isNative && clockAlive && coveredAt(iv, c0)) {
            // The player's video is the timebase; don't double-advance.
            applyFollow(c0, st.dayStart, st.dayEnd);
          } else {
            let c = c0 + deltaMs;
            if (iv.length && !coveredAt(iv, c)) {
              // Rolled into a gap - jump to the next/prev clip instead of
              // waiting through empty time in real seconds.
              if (st.direction > 0) {
                const ns = nextStartAfter(iv, c);
                if (ns == null) {
                  c = st.dayEnd;
                  setPlaying(false);
                } else {
                  c = ns;
                }
              } else {
                const pe = prevEndBefore(iv, c);
                if (pe == null) {
                  c = st.dayStart;
                  setPlaying(false);
                } else {
                  c = pe - 100;
                }
              }
            } else if (c <= st.dayStart || c >= st.dayEnd) {
              c = Math.min(st.dayEnd, Math.max(st.dayStart, c));
              setPlaying(false);
            }
            cursorRef.current = c;
            setCursor(c);
            applyFollow(c, st.dayStart, st.dayEnd);
          }
        }
        acc = 0;
      }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, []);

  // --- Transport actions ---
  // Give the player a grace window so the timer doesn't advance the cursor (and
  // make the sync effect re-seek, interrupting play()) before the element has
  // had a chance to actually start playing.
  const graceClock = () => {
    lastReportRef.current = performance.now();
  };

  const beginPlay = (dir: number) => {
    graceClock();
    setDirection(dir);
    setPlaying(true);
  };

  const togglePlay = () => {
    if (playing) {
      setPlaying(false);
    } else {
      graceClock();
      setPlaying(true);
    }
  };

  const seekTo = useCallback((ms: number) => {
    lastReportRef.current = performance.now();
    cursorRef.current = ms;
    setCursor(ms);
  }, []);

  const stepFrame = (deltaMs: number) => {
    setPlaying(false);
    seekTo(Math.min(dayEnd, Math.max(dayStart, cursor + deltaMs)));
  };

  const onView = useCallback((s: number, e: number) => {
    setViewStart(s);
    setViewEnd(e);
  }, []);

  const zoomOutFull = () => {
    setViewStart(dayStart);
    setViewEnd(dayEnd);
  };

  const jumpToLatest = () => {
    const iv = intervalsRef.current;
    if (!iv.length) return;
    setPlaying(false);
    seekTo(iv[iv.length - 1].end - 1000);
  };

  // --- Clip export ---
  const clipValid = clipIn != null && clipOut != null && clipOut > clipIn;
  const clipSeconds = clipValid ? Math.floor((clipOut! - clipIn!) / 1000) : 0;

  const runExport = async (cam: Camera) => {
    if (!clipValid || exportingId != null) return;
    setNotice(null);
    setExportingId(cam.id);
    const filename = clipFilename(cam.name, clipIn!, clipOut!, timezone);
    try {
      await downloadClip({
        cameraId: cam.id,
        startTs: Math.floor(clipIn! / 1000),
        endTs: Math.floor(clipOut! / 1000),
        name: filename,
        h264: exportH264,
      });
      setNotice({ kind: "ok", text: `Exported ${cam.name} · ${fmtDur(clipSeconds)}` });
    } catch (err) {
      setNotice({ kind: "err", text: `${cam.name}: ${(err as Error).message}` });
    } finally {
      setExportingId(null);
    }
  };

  // Grab the frame currently on screen straight from the <video> element - no
  // server round-trip, and it's exactly what the operator is looking at.
  const takeSnapshot = () => {
    const v = videoRef.current;
    if (!v || !v.videoWidth || !focusedCam) {
      setNotice({ kind: "err", text: "No video frame to capture yet." });
      return;
    }
    try {
      const canvas = document.createElement("canvas");
      canvas.width = v.videoWidth;
      canvas.height = v.videoHeight;
      const ctx = canvas.getContext("2d");
      if (!ctx) throw new Error("canvas unavailable");
      ctx.drawImage(v, 0, 0, canvas.width, canvas.height);
      canvas.toBlob((blob) => {
        if (!blob) {
          setNotice({ kind: "err", text: "Could not capture the frame." });
          return;
        }
        const p = partsInTz(cursor, timezone);
        const name = `${focusedCam.name}_${p.year}${pad(p.month)}${pad(p.day)}_${pad(p.hour)}${pad(p.minute)}${pad(p.second)}.png`;
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        setNotice({ kind: "ok", text: `Snapshot saved · ${name}` });
      }, "image/png");
    } catch {
      setNotice({ kind: "err", text: "Could not capture the frame." });
    }
  };

  return (
    <div className="page playback-page pb-vms">
      {/* ── Header: day + global actions ── */}
      <div className="page-header pb-head">
        <h1>Playback &amp; Export</h1>
        <div className="pb-head-actions">
          <input type="date" value={date} max={todayInTz(timezone)} onChange={(e) => setDate(e.target.value)} />
          <button className="btn btn-sm" onClick={jumpToLatest} title="Jump to the newest footage of this camera">
            Latest
          </button>
          <button className="btn btn-sm" onClick={zoomOutFull} title="Reset zoom to the full 24h day">
            Full day
          </button>
        </div>
      </div>

      {enabledCameras.length === 0 ? (
        <div className="empty-state">No enabled cameras to review.</div>
      ) : (
        <>
          {/* ── Camera strip ── */}
          <div className="pb-strip">
            {enabledCameras.map((cam) => {
              const recs = recsByCam[cam.id] ?? [];
              const isFocused = cam.id === focusedCam?.id;
              return (
                <div
                  key={cam.id}
                  className={`pb-card${isFocused ? " pb-card-on" : ""}`}
                  onClick={() => setFocusedId(cam.id)}
                  title={`Show ${cam.name} in the player`}
                >
                  <CameraThumb camera={cam} recordings={recs} />
                  <div className="pb-card-top">
                    <span className={`pb-dot ${cam.status === "online" ? "on" : "off"}`} />
                    <span className="pb-card-name">{cam.name}</span>
                    <button
                      className="pb-card-exp"
                      disabled={!clipValid || exportingId != null}
                      title={clipValid ? `Export the marked clip for ${cam.name}` : "Mark In and Out on the timeline first"}
                      onClick={(e) => {
                        e.stopPropagation();
                        runExport(cam);
                      }}
                    >
                      {exportingId === cam.id ? "…" : "⤓"}
                    </button>
                  </div>
                  <CoverageBar recordings={recs} dayStart={dayStart} dayEnd={dayEnd} />
                  <div className="pb-card-meta">{recs.length} clips</div>
                </div>
              );
            })}
          </div>

          {/* ── Main stage ── */}
          <div className="pb-stage">
            {focusedCam && (
              <PlaybackPlayer
                key={focusedCam.id}
                cameraName={focusedCam.name}
                recordings={focusedRecs}
                cursor={cursor}
                playing={playing}
                speed={speed}
                direction={direction}
                onPlayheadTime={reportPlayhead}
                videoRef={videoRef}
              />
            )}
          </div>

          {/* ── Dock: timeline + transport + export ── */}
          <div className="pb-dock">
            {notice && <div className={`pb-notice ${notice.kind}`}>{notice.text}</div>}

            <TimelineCanvas
              recordings={focusedRecs}
              events={focusedCam ? eventsByCam[focusedCam.id] ?? [] : []}
              timezone={timezone}
              dayStart={dayStart}
              dayEnd={dayEnd}
              viewStart={viewStart}
              viewEnd={viewEnd}
              cursor={cursor}
              clipIn={clipIn}
              clipOut={clipOut}
              height={54}
              onSeek={seekTo}
              onView={onView}
              onClip={(i, o) => {
                setClipIn(i);
                setClipOut(o);
              }}
            />

            <div className="pb-bar">
              {/* Transport */}
              <div className="pb-bar-grp">
                <button className="btn btn-sm" title="Frame back (−100ms)" onClick={() => stepFrame(-FRAME_STEP_MS)}>
                  ⏮
                </button>
                <button
                  className={`btn btn-sm ${playing && direction < 0 ? "btn-primary" : ""}`}
                  title="Reverse"
                  onClick={() => beginPlay(-1)}
                >
                  ◀◀
                </button>
                <button className="btn pb-play" onClick={togglePlay} title={playing ? "Pause" : "Play"}>
                  {playing ? "❚❚" : "▶"}
                </button>
                <button
                  className={`btn btn-sm ${playing && direction > 0 ? "btn-primary" : ""}`}
                  title="Forward"
                  onClick={() => beginPlay(1)}
                >
                  ▶▶
                </button>
                <button className="btn btn-sm" title="Frame forward (+100ms)" onClick={() => stepFrame(FRAME_STEP_MS)}>
                  ⏭
                </button>
              </div>

              {/* Clock */}
              <span className="pb-clock" title="Playhead position">
                {clock24(cursor, timezone)}
              </span>

              {/* Speed */}
              <div className="pb-bar-grp pb-speeds">
                {SPEEDS.map((s) => (
                  <button key={s} className={`btn btn-sm ${speed === s ? "btn-primary" : ""}`} onClick={() => setSpeed(s)}>
                    {s}×
                  </button>
                ))}
              </div>

              {/* Clip in/out */}
              <div className="pb-bar-grp">
                <button className="btn btn-sm" onClick={() => setClipIn(cursor)} title="Mark clip start at the playhead">
                  Set In
                </button>
                <button className="btn btn-sm" onClick={() => setClipOut(cursor)} title="Mark clip end at the playhead">
                  Set Out
                </button>
                <span className="pb-range">
                  {clipValid
                    ? `${clock24(clipIn!, timezone)} → ${clock24(clipOut!, timezone)} · ${fmtDur(clipSeconds)}`
                    : clipIn != null || clipOut != null
                      ? "Set both In and Out"
                      : "No clip marked"}
                </span>
                {(clipIn != null || clipOut != null) && (
                  <button
                    className="btn btn-sm"
                    title="Clear the clip markers"
                    onClick={() => {
                      setClipIn(null);
                      setClipOut(null);
                    }}
                  >
                    ✕
                  </button>
                )}
              </div>

              {/* Export + snapshot */}
              <div className="pb-bar-grp pb-bar-end">
                <span className="pb-fmt" title="H.264 re-encodes so the download plays in any browser; original is a faster stream-copy in the camera's codec">
                  <button className={`btn btn-sm ${!exportH264 ? "btn-primary" : ""}`} onClick={() => setExportH264(false)}>
                    Original
                  </button>
                  <button className={`btn btn-sm ${exportH264 ? "btn-primary" : ""}`} onClick={() => setExportH264(true)}>
                    H.264
                  </button>
                </span>
                <button className="btn btn-sm" onClick={takeSnapshot} title="Save the current frame as a PNG">
                  📷 Snapshot
                </button>
                <button
                  className="btn btn-primary btn-sm"
                  disabled={!clipValid || exportingId != null || !focusedCam}
                  onClick={() => focusedCam && runExport(focusedCam)}
                  title={clipValid ? "Download the marked clip for the camera in the player" : "Mark In and Out on the timeline first"}
                >
                  {exportingId != null ? "Exporting…" : "⤓ Export clip"}
                </button>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
