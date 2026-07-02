import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { listCameras } from "../api/cameras";
import { listRecordings } from "../api/recordings";
import { listEvents } from "../api/system";
import type { Camera, NvrEvent, Recording } from "../api/types";
import { PlaybackRow } from "../components/PlaybackRow";
import { useSettings } from "../context/SettingsContext";
import { clock24, startOfDayInTz, todayInTz } from "../utils/datetime";

const DAY_MS = 24 * 3600 * 1000;
const SPEEDS = [0.5, 1, 2, 4, 16, 64];
const FRAME_STEP_MS = 100;
// The transport clock ticks at ~20fps: smooth enough for the playhead while
// keeping seek-scrub modes and per-camera re-renders cheap.
const TICK_MS = 50;

// Video column widths for the size control.
const SIZE_PX: Record<string, number> = { S: 220, M: 320, L: 460, XL: 640 };
const SIZES = ["S", "M", "L", "XL"] as const;
type SizeKey = (typeof SIZES)[number];

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

// Multi-select dropdown for choosing which cameras to review.
function CameraSelect({
  cameras,
  selectedIds,
  onToggle,
}: {
  cameras: Camera[];
  selectedIds: number[];
  onToggle: (id: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const label = selectedIds.length === 0 ? "Select cameras" : `${selectedIds.length} camera${selectedIds.length > 1 ? "s" : ""}`;

  return (
    <div className="pb-dropdown" ref={ref}>
      <button className="btn btn-sm" onClick={() => setOpen((o) => !o)}>
        {label} ▾
      </button>
      {open && (
        <div className="pb-dropdown-menu">
          {cameras.length === 0 ? (
            <div className="pb-dropdown-empty">No enabled cameras</div>
          ) : (
            cameras.map((c) => (
              <label key={c.id} className="pb-dropdown-item">
                <input type="checkbox" checked={selectedIds.includes(c.id)} onChange={() => onToggle(c.id)} />
                {c.name}
              </label>
            ))
          )}
        </div>
      )}
    </div>
  );
}

export function PlaybackPage() {
  const { timezone } = useSettings();
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [date, setDate] = useState(() => todayInTz(timezone));
  const [size, setSize] = useState<SizeKey>(() => {
    const s = localStorage.getItem("lightnvr_pb_size");
    return (SIZES as readonly string[]).includes(s ?? "") ? (s as SizeKey) : "M";
  });

  const [recsByCam, setRecsByCam] = useState<Record<number, Recording[]>>({});
  const [eventsByCam, setEventsByCam] = useState<Record<number, NvrEvent[]>>({});

  // Day boundaries are the selected calendar date's midnight-to-midnight window
  // in the NVR's timezone, so the 24h timeline lines up with local wall time.
  const dayStart = useMemo(() => startOfDayInTz(date, timezone), [date, timezone]);
  const dayEnd = dayStart + DAY_MS;

  // Shared time axis (all timelines zoom/pan together).
  const [viewStart, setViewStart] = useState(dayStart);
  const [viewEnd, setViewEnd] = useState(dayEnd);

  // Transport state.
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [direction, setDirection] = useState(1);
  const [syncLocked, setSyncLocked] = useState(true);

  // Cursors: one master (locked mode) plus per-camera cursors (unlocked mode).
  const [masterCursor, setMasterCursor] = useState(dayStart);
  const [indieCursors, setIndieCursors] = useState<Record<number, number>>({});

  // Clip export markers (epoch ms).
  const [clipIn, setClipIn] = useState<number | null>(null);
  const [clipOut, setClipOut] = useState<number | null>(null);
  const [copied, setCopied] = useState(false);

  // Refs mirror state for the rAF loop (avoids stale closures / re-subscribes).
  const stateRef = useRef({ playing, speed, direction, syncLocked, dayStart, dayEnd, selectedIds });
  stateRef.current = { playing, speed, direction, syncLocked, dayStart, dayEnd, selectedIds };
  const masterRef = useRef(masterCursor);
  masterRef.current = masterCursor;
  const indieRef = useRef(indieCursors);
  indieRef.current = indieCursors;
  const viewRef = useRef({ start: viewStart, end: viewEnd });
  viewRef.current = { start: viewStart, end: viewEnd };
  // Merged recording intervals across selected cameras, for gap-skipping.
  const intervalsRef = useRef<Interval[]>([]);
  // Timestamp of the last position report from the clock video; a fresh value
  // means the video is actively playing and driving the cursor itself.
  const lastReportRef = useRef(0);
  const lastSetRef = useRef(0);

  useEffect(() => {
    localStorage.setItem("lightnvr_pb_size", size);
  }, [size]);

  const effCursor = useCallback(
    (id: number) => (syncLocked ? masterCursor : indieCursors[id] ?? masterCursor),
    [syncLocked, masterCursor, indieCursors],
  );

  // The clock video reports its real position here; it steers the shared cursor
  // (synced mode only) so playback follows the element instead of a free timer.
  const reportPlayhead = useCallback((ms: number) => {
    if (!stateRef.current.syncLocked) return;
    const now = performance.now();
    lastReportRef.current = now;
    masterRef.current = ms;
    // Throttle the React state update to ~30fps so a high-fps recording can't
    // flood re-renders; masterRef stays exact for the transport logic.
    if (now - lastSetRef.current >= 33) {
      lastSetRef.current = now;
      setMasterCursor(ms);
    }
  }, []);

  // Load camera list once.
  useEffect(() => {
    listCameras().then((cams) => {
      setCameras(cams);
      setSelectedIds(cams.filter((c) => c.enabled).map((c) => c.id).slice(0, 4));
    });
  }, []);

  // Reset the view window and clock whenever the day changes.
  useEffect(() => {
    setViewStart(dayStart);
    setViewEnd(dayEnd);
    setMasterCursor(dayStart);
    setIndieCursors({});
    setPlaying(false);
  }, [dayStart, dayEnd]);

  // Load recordings + motion events for the selected cameras / day.
  useEffect(() => {
    if (selectedIds.length === 0) {
      setRecsByCam({});
      setEventsByCam({});
      return;
    }
    const startIso = new Date(dayStart).toISOString();
    const endIso = new Date(dayEnd).toISOString();
    let cancelled = false;

    Promise.all(
      selectedIds.map(async (id) => {
        const [recs, evs] = await Promise.all([
          listRecordings({ camera_id: id, start: startIso, end: endIso, limit: 500 }),
          listEvents(id, 500),
        ]);
        return { id, recs, evs };
      }),
    ).then((results) => {
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
    });

    return () => {
      cancelled = true;
    };
  }, [selectedIds, dayStart, dayEnd]);

  // Rebuild the merged interval list and, on a fresh day, drop the playhead on
  // the first available footage instead of leaving it stuck at empty midnight.
  useEffect(() => {
    const iv: Interval[] = [];
    for (const id of selectedIds) {
      for (const r of recsByCam[id] ?? []) {
        iv.push({ start: recStartMs(r), end: recEndMs(r) });
      }
    }
    iv.sort((a, b) => a.start - b.start);
    intervalsRef.current = iv;

    if (iv.length && masterRef.current <= dayStart) {
      masterRef.current = iv[0].start;
      setMasterCursor(iv[0].start);
    }
  }, [recsByCam, selectedIds, dayStart]);

  // The transport clock: advances the playhead(s) while playing, skips over
  // gaps with no footage, and pans the view to follow when zoomed in.
  useEffect(() => {
    let last = performance.now();
    let acc = 0;
    let raf = 0;
    // Pan the view to keep the playhead on screen when zoomed in.
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
          if (st.syncLocked) {
            const iv = intervalsRef.current;
            const isNative = st.direction > 0 && st.speed > 0 && st.speed <= 4;
            const c0 = masterRef.current;
            const clockAlive = performance.now() - lastReportRef.current < 1000;
            if (isNative && clockAlive && coveredAt(iv, c0)) {
              // The clock camera's video is the timebase; don't double-advance.
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
              masterRef.current = c;
              setMasterCursor(c);
              applyFollow(c, st.dayStart, st.dayEnd);
            }
          } else {
            const next: Record<number, number> = { ...indieRef.current };
            for (const id of st.selectedIds) {
              let c = (next[id] ?? masterRef.current) + deltaMs;
              c = Math.min(st.dayEnd, Math.max(st.dayStart, c));
              next[id] = c;
            }
            indieRef.current = next;
            setIndieCursors(next);
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
  // Give the clock video a grace window so the timer doesn't advance the cursor
  // (and make the sync effect re-seek, interrupting play()) before the element
  // has had a chance to actually start playing.
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

  const seekTo = useCallback(
    (id: number, ms: number) => {
      lastReportRef.current = performance.now();
      if (syncLocked) {
        setMasterCursor(ms);
        masterRef.current = ms;
      } else {
        setIndieCursors((prev) => ({ ...prev, [id]: ms }));
      }
    },
    [syncLocked],
  );

  const stepFrame = (deltaMs: number) => {
    setPlaying(false);
    if (syncLocked) {
      const c = Math.min(dayEnd, Math.max(dayStart, masterCursor + deltaMs));
      setMasterCursor(c);
      masterRef.current = c;
    } else {
      setIndieCursors((prev) => {
        const next = { ...prev };
        for (const id of selectedIds) {
          next[id] = Math.min(dayEnd, Math.max(dayStart, (next[id] ?? masterCursor) + deltaMs));
        }
        return next;
      });
    }
  };

  const onView = useCallback((s: number, e: number) => {
    setViewStart(s);
    setViewEnd(e);
  }, []);

  const zoomOutFull = () => {
    setViewStart(dayStart);
    setViewEnd(dayEnd);
  };

  const toggleCamera = (id: number) => {
    setSelectedIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  };

  // Clip export helpers.
  const clipUnixIn = clipIn != null ? Math.floor(clipIn / 1000) : null;
  const clipUnixOut = clipOut != null ? Math.floor(clipOut / 1000) : null;
  const clipValid = clipUnixIn != null && clipUnixOut != null && clipUnixOut > clipUnixIn;
  const clipText = clipValid ? `${clipUnixIn},${clipUnixOut}` : "";

  const copyClip = async () => {
    if (!clipText) return;
    try {
      await navigator.clipboard.writeText(clipText);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked; the values are still shown on screen */
    }
  };

  const enabledCameras = cameras.filter((c) => c.enabled);
  const selectedCameras = cameras.filter((c) => selectedIds.includes(c.id));
  const referenceCursor = syncLocked ? masterCursor : effCursor(selectedIds[0] ?? -1);
  // In synced mode the first row's playing video is the shared timebase.
  const clockId = syncLocked ? selectedCameras[0]?.id ?? -1 : -1;

  return (
    <div className="page playback-page">
      <div className="page-header">
        <div className="pb-title-group">
          <h1>Playback &amp; Export</h1>
          <CameraSelect cameras={enabledCameras} selectedIds={selectedIds} onToggle={toggleCamera} />
          <span className="pb-cam-hint">Select the cameras to view playback here</span>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <input type="date" value={date} max={todayInTz(timezone)} onChange={(e) => setDate(e.target.value)} />
          <div className="pb-size-ctrl" title="Video size">
            {SIZES.map((s) => (
              <button key={s} className={`btn btn-sm ${size === s ? "btn-primary" : ""}`} onClick={() => setSize(s)}>
                {s}
              </button>
            ))}
          </div>
          <button className="btn btn-sm" onClick={zoomOutFull} title="Reset zoom to the full 24h day">
            Full day
          </button>
        </div>
      </div>

      {selectedCameras.length === 0 ? (
        <div className="empty-state">Select one or more cameras to review footage.</div>
      ) : (
        <>
          {/* Transport bar */}
          <div className="pb-transport">
            <div className="pb-transport-group">
              <button className="btn btn-sm" title="Frame back (−100ms)" onClick={() => stepFrame(-FRAME_STEP_MS)}>
                ⏮ 100ms
              </button>
              <button
                className={`btn btn-sm ${playing && direction < 0 ? "btn-primary" : ""}`}
                title="Reverse"
                onClick={() => beginPlay(-1)}
              >
                ◀◀
              </button>
              <button className="btn" onClick={togglePlay}>
                {playing ? "❚❚ Pause" : "▶ Play"}
              </button>
              <button
                className={`btn btn-sm ${playing && direction > 0 ? "btn-primary" : ""}`}
                title="Forward"
                onClick={() => beginPlay(1)}
              >
                ▶▶
              </button>
              <button className="btn btn-sm" title="Frame forward (+100ms)" onClick={() => stepFrame(FRAME_STEP_MS)}>
                100ms ⏭
              </button>
            </div>

            <div className="pb-transport-group pb-speeds">
              {SPEEDS.map((s) => (
                <button
                  key={s}
                  className={`btn btn-sm ${speed === s ? "btn-primary" : ""}`}
                  onClick={() => setSpeed(s)}
                >
                  {s}×
                </button>
              ))}
            </div>

            <div className="pb-transport-group">
              <span className="pb-clock">
                {clock24(referenceCursor, timezone)}
                <span className="pb-unix">{Math.floor(referenceCursor / 1000)}</span>
              </span>
              <button
                className={`btn btn-sm ${syncLocked ? "btn-primary" : ""}`}
                title="Lock all cameras to the same timestamp"
                onClick={() => setSyncLocked((v) => !v)}
              >
                {syncLocked ? "🔒 Synced" : "🔓 Independent"}
              </button>
            </div>
          </div>

          {/* Clip export bar */}
          <div className="pb-clip-bar">
            <span className="pb-clip-label">Clip export:</span>
            <button className="btn btn-sm" onClick={() => setClipIn(referenceCursor)}>
              Set In
            </button>
            <button className="btn btn-sm" onClick={() => setClipOut(referenceCursor)}>
              Set Out
            </button>
            <span className="pb-clip-readout">
              In: {clipIn != null ? `${clock24(clipIn, timezone)} (${clipUnixIn})` : "—"}
              {"  "}·{"  "}
              Out: {clipOut != null ? `${clock24(clipOut, timezone)} (${clipUnixOut})` : "—"}
              {clipValid && <span className="pb-clip-dur"> · {clipUnixOut! - clipUnixIn!}s</span>}
            </span>
            <button className="btn btn-sm" disabled={!clipValid} onClick={copyClip}>
              {copied ? "Copied!" : "Copy timestamps"}
            </button>
            {(clipIn != null || clipOut != null) && (
              <button
                className="btn btn-sm"
                onClick={() => {
                  setClipIn(null);
                  setClipOut(null);
                }}
              >
                Clear
              </button>
            )}
            <span className="pb-clip-hint">
              Set In/Out, then use each camera's <b>⤓ Clip</b> button to download.
            </span>
          </div>

          {/* Legend */}
          <div className="pb-legend">
            <span className="pb-legend-item">
              <i className="pb-swatch" style={{ background: "#2f7fb5" }} /> Continuous
            </span>
            <span className="pb-legend-item">
              <i className="pb-swatch" style={{ background: "#f59e0b" }} /> Motion
            </span>
            <span className="pb-legend-item">
              <i className="pb-swatch pb-swatch-line" style={{ background: "#ef4444" }} /> Motion event
            </span>
            <span className="pb-legend-item">
              <i className="pb-swatch" style={{ background: "#232b34" }} /> No recording
            </span>
            <span className="pb-legend-item pb-legend-tip">Scroll a timeline to zoom · drag to pan · drag the blue handles to trim a clip</span>
          </div>

          {/* Camera rows */}
          <div className="pb-rows" style={{ "--pb-video-w": `${SIZE_PX[size]}px` } as CSSProperties}>
            {selectedCameras.map((cam) => (
              <PlaybackRow
                key={cam.id}
                camera={cam}
                recordings={recsByCam[cam.id] ?? []}
                events={eventsByCam[cam.id] ?? []}
                dayStart={dayStart}
                dayEnd={dayEnd}
                viewStart={viewStart}
                viewEnd={viewEnd}
                cursor={effCursor(cam.id)}
                playing={playing}
                speed={speed}
                direction={direction}
                clipIn={clipIn}
                clipOut={clipOut}
                isClock={cam.id === clockId}
                onSeek={(ms) => seekTo(cam.id, ms)}
                onView={onView}
                onClip={(i, o) => {
                  setClipIn(i);
                  setClipOut(o);
                }}
                onPlayheadTime={reportPlayhead}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
