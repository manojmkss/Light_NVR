import { useCallback, useEffect, useRef } from "react";
import type { NvrEvent, Recording } from "../api/types";
import { partsInTz, tzOffsetMs } from "../utils/datetime";

// Colour language for the timeline track. Continuous recording is a calm,
// low-attention blue; motion recording is a bright alert orange; motion
// *events* are fine red ticks layered on top (line-crossing / detection
// markers); anything with no recording at all stays the muted grey track.
const COLORS = {
  track: "#12171d",
  gap: "#232b34",
  continuous: "#2f7fb5",
  motion: "#f59e0b",
  event: "#ef4444",
  playhead: "#f8fafc",
  clipFill: "rgba(59, 130, 246, 0.22)",
  clipHandle: "#3b82f6",
  grid: "rgba(255, 255, 255, 0.08)",
  label: "#8b94a3",
};

const HANDLE_HIT_PX = 7;
const CLICK_SLOP_PX = 4;
const MIN_SPAN_MS = 30_000; // deepest zoom: a 30-second window

export interface TimelineCanvasProps {
  recordings: Recording[];
  events?: NvrEvent[];
  /** IANA display timezone; "" / undefined uses the viewer's local zone. */
  timezone?: string;
  /** Outer clamp for zoom-out / panning (usually the selected day). */
  dayStart: number;
  dayEnd: number;
  /** Current visible window (epoch ms). */
  viewStart: number;
  viewEnd: number;
  /** Playhead position (epoch ms), or null when unset. */
  cursor: number | null;
  clipIn?: number | null;
  clipOut?: number | null;
  height?: number;
  onSeek: (ms: number) => void;
  onView: (start: number, end: number) => void;
  onClip?: (inMs: number | null, outMs: number | null) => void;
}

function recEndMs(rec: Recording): number {
  if (rec.ended_at) return new Date(rec.ended_at).getTime();
  return new Date(rec.started_at).getTime() + (rec.duration_seconds ?? 0) * 1000;
}

// Choose a "nice" gridline interval (ms) for the current span so labels stay
// readable from a 24h overview down to a 30s micro-window.
function tickInterval(spanMs: number): number {
  const steps = [
    10_000, 30_000, 60_000, 5 * 60_000, 10 * 60_000, 15 * 60_000, 30 * 60_000,
    60 * 60_000, 2 * 60 * 60_000, 3 * 60 * 60_000, 6 * 60 * 60_000,
  ];
  const target = spanMs / 8; // aim for ~8 gridlines
  for (const s of steps) if (s >= target) return s;
  return steps[steps.length - 1];
}

function fmtTick(ms: number, spanMs: number, tz?: string): string {
  const p = partsInTz(ms, tz);
  const hh = p.hour.toString().padStart(2, "0");
  const mm = p.minute.toString().padStart(2, "0");
  if (spanMs <= 5 * 60_000) {
    const ss = p.second.toString().padStart(2, "0");
    return `${hh}:${mm}:${ss}`;
  }
  return `${hh}:${mm}`;
}

export function TimelineCanvas({
  recordings,
  events = [],
  timezone,
  dayStart,
  dayEnd,
  viewStart,
  viewEnd,
  cursor,
  clipIn = null,
  clipOut = null,
  height = 64,
  onSeek,
  onView,
  onClip,
}: TimelineCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const widthRef = useRef(0);

  // Drag bookkeeping kept in a ref so the pointer handlers stay stable.
  const drag = useRef<{
    mode: "pan" | "clipIn" | "clipOut" | "none";
    startX: number;
    moved: boolean;
    startViewStart: number;
    startViewEnd: number;
  } | null>(null);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    const W = widthRef.current || canvas.clientWidth;
    const H = height;
    if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const span = Math.max(1, viewEnd - viewStart);
    const xOf = (t: number) => ((t - viewStart) / span) * W;
    const trackTop = 16;
    const trackH = H - trackTop - 4;

    // Base track: everything is "no recording" grey until a clip paints over it.
    ctx.fillStyle = COLORS.gap;
    ctx.fillRect(0, trackTop, W, trackH);

    // Recording intervals.
    for (const rec of recordings) {
      const s = new Date(rec.started_at).getTime();
      const e = recEndMs(rec);
      if (e < viewStart || s > viewEnd) continue;
      const x1 = Math.max(0, xOf(s));
      const x2 = Math.min(W, xOf(e));
      ctx.fillStyle = rec.trigger === "motion" ? COLORS.motion : COLORS.continuous;
      ctx.fillRect(x1, trackTop, Math.max(1, x2 - x1), trackH);
    }

    // Motion events → fine red target lines on top of the track.
    ctx.strokeStyle = COLORS.event;
    ctx.lineWidth = 1;
    for (const ev of events) {
      if (ev.type !== "motion") continue;
      const t = new Date(ev.created_at).getTime();
      if (t < viewStart || t > viewEnd) continue;
      const x = xOf(t);
      ctx.beginPath();
      ctx.moveTo(x, trackTop);
      ctx.lineTo(x, trackTop + trackH);
      ctx.stroke();
    }

    // Time gridlines + labels.
    const step = tickInterval(span);
    ctx.fillStyle = COLORS.label;
    ctx.font = "10px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
    ctx.textBaseline = "top";
    // Align gridlines to the chosen timezone's local step boundaries (so an
    // hourly tick reads :00 in that zone, not :30 for a +5:30 offset).
    const off = tzOffsetMs(viewStart, timezone);
    const first = Math.ceil((viewStart + off) / step) * step - off;
    for (let t = first; t <= viewEnd; t += step) {
      const x = xOf(t);
      ctx.strokeStyle = COLORS.grid;
      ctx.beginPath();
      ctx.moveTo(x, trackTop);
      ctx.lineTo(x, trackTop + trackH);
      ctx.stroke();
      ctx.fillText(fmtTick(t, span, timezone), x + 3, 2);
    }

    // Clip selection region + draggable handles.
    if (clipIn != null && clipOut != null && clipOut > clipIn) {
      const x1 = xOf(clipIn);
      const x2 = xOf(clipOut);
      ctx.fillStyle = COLORS.clipFill;
      ctx.fillRect(x1, trackTop, x2 - x1, trackH);
    }
    for (const [mark, isIn] of [[clipIn, true], [clipOut, false]] as [number | null, boolean][]) {
      if (mark == null) continue;
      const x = xOf(mark);
      ctx.fillStyle = COLORS.clipHandle;
      ctx.fillRect(x - 1.5, trackTop, 3, trackH);
      // Grab tab
      ctx.beginPath();
      ctx.moveTo(x, trackTop);
      ctx.lineTo(x + (isIn ? -8 : 8), trackTop - 6);
      ctx.lineTo(x + (isIn ? -8 : 8), trackTop + 6);
      ctx.closePath();
      ctx.fill();
    }

    // Playhead.
    if (cursor != null && cursor >= viewStart && cursor <= viewEnd) {
      const x = xOf(cursor);
      ctx.strokeStyle = COLORS.playhead;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, H);
      ctx.stroke();
      ctx.fillStyle = COLORS.playhead;
      ctx.beginPath();
      ctx.arc(x, 6, 4, 0, Math.PI * 2);
      ctx.fill();
    }
  }, [recordings, events, timezone, viewStart, viewEnd, cursor, clipIn, clipOut, height]);

  // Redraw on any state change.
  useEffect(() => {
    draw();
  }, [draw]);

  // Track element width (responsive + high-DPI correctness).
  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const ro = new ResizeObserver((entries) => {
      widthRef.current = entries[0].contentRect.width;
      draw();
    });
    ro.observe(wrap);
    widthRef.current = wrap.clientWidth;
    return () => ro.disconnect();
  }, [draw]);

  const timeAtX = useCallback(
    (clientX: number): number => {
      const canvas = canvasRef.current!;
      const rect = canvas.getBoundingClientRect();
      const frac = (clientX - rect.left) / rect.width;
      return viewStart + frac * (viewEnd - viewStart);
    },
    [viewStart, viewEnd],
  );

  const xOfTime = useCallback(
    (t: number): number => {
      const canvas = canvasRef.current!;
      const rect = canvas.getBoundingClientRect();
      return ((t - viewStart) / (viewEnd - viewStart)) * rect.width;
    },
    [viewStart, viewEnd],
  );

  // Scroll-to-zoom, centred on the cursor so the point under the mouse stays put.
  const onWheel = useCallback(
    (e: React.WheelEvent) => {
      e.preventDefault();
      const focus = timeAtX(e.clientX);
      const span = viewEnd - viewStart;
      const factor = e.deltaY > 0 ? 1.25 : 0.8; // down = zoom out, up = zoom in
      let newSpan = Math.min(dayEnd - dayStart, Math.max(MIN_SPAN_MS, span * factor));
      const leftFrac = (focus - viewStart) / span;
      let ns = focus - leftFrac * newSpan;
      let ne = ns + newSpan;
      // Clamp inside the day bounds without changing the span.
      if (ns < dayStart) { ns = dayStart; ne = ns + newSpan; }
      if (ne > dayEnd) { ne = dayEnd; ns = ne - newSpan; }
      onView(ns, ne);
    },
    [timeAtX, viewStart, viewEnd, dayStart, dayEnd, onView],
  );

  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      (e.target as HTMLElement).setPointerCapture(e.pointerId);
      const x = e.clientX;
      let mode: "pan" | "clipIn" | "clipOut" = "pan";
      if (onClip) {
        if (clipIn != null && Math.abs(xOfTime(clipIn) - (x - canvasRef.current!.getBoundingClientRect().left)) <= HANDLE_HIT_PX) {
          mode = "clipIn";
        } else if (clipOut != null && Math.abs(xOfTime(clipOut) - (x - canvasRef.current!.getBoundingClientRect().left)) <= HANDLE_HIT_PX) {
          mode = "clipOut";
        }
      }
      drag.current = {
        mode,
        startX: x,
        moved: false,
        startViewStart: viewStart,
        startViewEnd: viewEnd,
      };
    },
    [clipIn, clipOut, onClip, xOfTime, viewStart, viewEnd],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent) => {
      const d = drag.current;
      if (!d) return;
      if (Math.abs(e.clientX - d.startX) > CLICK_SLOP_PX) d.moved = true;

      if (d.mode === "clipIn" || d.mode === "clipOut") {
        const t = Math.round(timeAtX(e.clientX));
        if (d.mode === "clipIn") onClip?.(Math.min(t, clipOut ?? t), clipOut ?? null);
        else onClip?.(clipIn ?? null, Math.max(t, clipIn ?? t));
        return;
      }

      // Pan: translate the window by the dragged distance, clamped to bounds.
      const rect = canvasRef.current!.getBoundingClientRect();
      const span = d.startViewEnd - d.startViewStart;
      const dt = ((d.startX - e.clientX) / rect.width) * span;
      let ns = d.startViewStart + dt;
      let ne = d.startViewEnd + dt;
      if (ns < dayStart) { ns = dayStart; ne = ns + span; }
      if (ne > dayEnd) { ne = dayEnd; ns = ne - span; }
      onView(ns, ne);
    },
    [timeAtX, clipIn, clipOut, onClip, dayStart, dayEnd, onView],
  );

  const onPointerUp = useCallback(
    (e: React.PointerEvent) => {
      const d = drag.current;
      drag.current = null;
      if (!d) return;
      // A pan that never really moved is a click → seek there.
      if (d.mode === "pan" && !d.moved) {
        onSeek(Math.round(timeAtX(e.clientX)));
      }
    },
    [timeAtX, onSeek],
  );

  return (
    <div ref={wrapRef} className="timeline-wrap">
      <canvas
        ref={canvasRef}
        className="timeline-canvas"
        style={{ width: "100%", height }}
        onWheel={onWheel}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
      />
    </div>
  );
}
