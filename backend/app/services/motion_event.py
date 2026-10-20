"""One motion event, watched by AI from start to finish.

While motion is active the session grabs the newest native-resolution frame
every `interval_ms` (500ms by default) and runs object detection on it. Ticks
never queue up: if an inference is still running when the next tick is due,
that tick is skipped. Detections are tied together across ticks by a small
IoU tracker, so "a person walked across" is one person, not six.

It produces:
- one immediate alert per event - on the first confirmed object (or plain
  motion when AI is off/failing, or after a short grace when objects-only
  alerting is off), with the boxes drawn on a full-resolution frame;
- one end-of-event summary: when, how long, what was seen (counts, peak,
  appear/leave timeline), the clip, and optionally a VLM description of the
  clearest frame.

While AI still sees a target that has moved recently, it keeps the motion
event open even if pixel motion stops (someone standing at the door).
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import cv2
import numpy as np

from app.services.ai.types import DetectedObject

logger = logging.getLogger(__name__)

_PLURALS = {"person": "people", "bus": "buses"}


def plural(label: str, n: int) -> str:
    if n == 1:
        return f"1 {label}"
    return f"{n} {_PLURALS.get(label, label + 's')}"


def _iou(a: tuple, b: tuple) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _center(box: tuple) -> tuple[float, float]:
    return box[0] + box[2] / 2, box[1] + box[3] / 2


@dataclass
class Track:
    id: int  # per-label sequence number: person #1, person #2...
    label: str
    first_seen: float
    last_seen: float
    box: tuple
    hits: int = 1
    confirmed: bool = False
    left_at: float | None = None
    anchor: tuple[float, float] = (0.0, 0.0)  # where it was when it last "moved"
    last_moved: float = 0.0
    best_score: float = -1.0
    best_conf: float = 0.0
    best_box: tuple = (0.0, 0.0, 0.0, 0.0)
    best_frame: np.ndarray | None = None
    best_at: float = 0.0


class ObjectTracker:
    """Greedy IoU matching per label, with a centre-distance fallback for
    fast movers. A track must be seen twice (or once with high confidence)
    before it counts - that filters single-frame false positives."""

    MATCH_IOU = 0.2
    LOST_AFTER = 4.0  # seconds unseen before a track counts as gone
    CONFIRM_HITS = 2
    CONFIRM_CONF = 0.7
    MOVE_EPS = 0.03  # centre shift (fraction of frame) that counts as movement

    def __init__(self) -> None:
        self.tracks: list[Track] = []
        self.peak: dict[str, int] = {}
        self._seq: dict[str, int] = {}
        self.timeline: list[tuple[float, str]] = []  # (wall time, text)

    def _match_score(self, track: Track, obj: DetectedObject) -> float:
        box = (obj.x, obj.y, obj.w, obj.h)
        iou = _iou(track.box, box)
        if iou >= self.MATCH_IOU:
            return iou
        (tx, ty), (ox, oy) = _center(track.box), _center(box)
        size = max(track.box[2], track.box[3], obj.w, obj.h)
        return 0.1 if math.hypot(tx - ox, ty - oy) < 0.6 * size else 0.0

    def update(self, objects: list[DetectedObject], now: float, wall: float, frame: np.ndarray | None) -> list[Track]:
        """Feed one tick's detections. Returns tracks confirmed this tick."""
        for t in self.tracks:
            if t.left_at is None and now - t.last_seen > self.LOST_AFTER:
                t.left_at = t.last_seen
                if t.confirmed:
                    self.timeline.append((wall - (now - t.last_seen), f"{t.label} #{t.id} left"))
        active = [t for t in self.tracks if t.left_at is None]

        pairs = []
        for ti, t in enumerate(active):
            for oi, o in enumerate(objects):
                if o.label == t.label:
                    s = self._match_score(t, o)
                    if s > 0:
                        pairs.append((s, ti, oi))
        pairs.sort(reverse=True)
        used_t: set[int] = set()
        used_o: set[int] = set()
        matched: list[tuple[Track, DetectedObject]] = []
        for _s, ti, oi in pairs:
            if ti in used_t or oi in used_o:
                continue
            used_t.add(ti)
            used_o.add(oi)
            matched.append((active[ti], objects[oi]))

        for oi, o in enumerate(objects):
            if oi in used_o:
                continue
            box = (o.x, o.y, o.w, o.h)
            t = Track(id=0, label=o.label, first_seen=now, last_seen=now, box=box, hits=0,
                      anchor=_center(box), last_moved=now)
            self.tracks.append(t)
            matched.append((t, o))

        newly_confirmed: list[Track] = []
        for t, o in matched:
            box = (o.x, o.y, o.w, o.h)
            t.box = box
            t.last_seen = now
            t.hits += 1
            c = _center(box)
            if math.hypot(c[0] - t.anchor[0], c[1] - t.anchor[1]) > self.MOVE_EPS:
                t.anchor, t.last_moved = c, now
            # Clearest view: confident and large (close to the camera).
            score = o.confidence * o.w * o.h
            if score > t.best_score:
                t.best_score, t.best_conf, t.best_box = score, o.confidence, box
                t.best_frame, t.best_at = frame, wall
            if not t.confirmed and (t.hits >= self.CONFIRM_HITS or o.confidence >= self.CONFIRM_CONF):
                t.confirmed = True
                self._seq[t.label] = self._seq.get(t.label, 0) + 1
                t.id = self._seq[t.label]
                self.timeline.append((wall, f"{t.label} #{t.id} appeared"))
                newly_confirmed.append(t)

        counts: dict[str, int] = {}
        for t in self.tracks:
            if t.confirmed and t.left_at is None and t.last_seen == now:
                counts[t.label] = counts.get(t.label, 0) + 1
        for label, n in counts.items():
            self.peak[label] = max(self.peak.get(label, 0), n)
        return newly_confirmed

    def confirmed(self) -> list[Track]:
        return [t for t in self.tracks if t.confirmed]

    def totals(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.confirmed():
            out[t.label] = out.get(t.label, 0) + 1
        return out

    def holding(self, now: float, static_max: float) -> bool:
        """Something real is still in view and has moved within `static_max`
        seconds - a person at the door, not a car parked for the night."""
        return any(
            t.confirmed and t.left_at is None and now - t.last_seen < 1.5 and now - t.last_moved < static_max
            for t in self.tracks
        )


def draw_boxes(frame: np.ndarray, boxes: list[tuple[str, float, tuple]]) -> np.ndarray:
    """Copy of `frame` with labelled boxes; boxes are normalised (x, y, w, h)."""
    out = frame.copy()
    h, w = out.shape[:2]
    thick = max(2, round(w / 400))
    scale = max(0.5, w / 1280)
    for label, conf, (bx, by, bw, bh) in boxes:
        x1, y1 = int(bx * w), int(by * h)
        x2, y2 = int((bx + bw) * w), int((by + bh) * h)
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 200, 255), thick)
        text = f"{label} {round(conf * 100)}%"
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
        ty = max(th + 4, y1)
        cv2.rectangle(out, (x1, ty - th - 4), (x1 + tw + 4, ty), (0, 200, 255), -1)
        cv2.putText(out, text, (x1 + 2, ty - 3), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thick, cv2.LINE_AA)
    return out


def _jpeg(frame: np.ndarray, quality: int = 90) -> bytes | None:
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buf.tobytes() if ok else None


def format_duration(seconds: float) -> str:
    s = max(0, round(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


@dataclass
class EventSummary:
    message: str
    snapshot: bytes | None
    seen: dict[str, int] = field(default_factory=dict)


class MotionEventSession:
    AI_GRACE_SECONDS = 2.0  # objects-only off: wait this long for an object before a plain motion alert
    HOLD_SECONDS = 1.5
    STATIC_HOLD_MAX = 30.0  # a target that hasn't moved for this long stops holding the event open
    VLM_TIMEOUT = 30.0

    def __init__(
        self,
        camera_id: int,
        camera_name: str,
        settings,  # AISettings | None (detached copy)
        *,
        motion_active: Callable[[], bool],
        hold_motion: Callable[[float], None],
        motion_level: Callable[[], float],
        get_frame: Callable[[int], tuple[np.ndarray, float] | None],
        detect,  # async (settings, jpeg) -> list[DetectedObject] | None
        emit,  # async (camera_id, type, message, snapshot_bytes) -> event id | None
        describe=None,  # async (settings, jpeg, labels, camera_name, prompt) -> str
        tz=timezone.utc,
    ):
        self.camera_id = camera_id
        self.camera_name = camera_name
        self.settings = settings
        self._motion_active = motion_active
        self._hold_motion = hold_motion
        self._motion_level = motion_level
        self._get_frame = get_frame
        self._detect = detect
        self._emit = emit
        self._describe = describe
        self.tz = tz

        s = settings
        self.ai_on = bool(s and s.enabled and s.detection_enabled)
        self.objects_only = bool(s and s.alert_on_objects_only)
        interval_ms = getattr(s, "monitor_interval_ms", None) or 500
        self.interval = max(0.1, interval_ms / 1000.0)

        self.tracker = ObjectTracker()
        self.started_wall = time.time()
        self.started_mono = time.monotonic()
        self.ended_wall: float | None = None
        self.alert_event_id: int | None = None
        self.alerted = False
        self.ai_worked = False
        self.ai_failed = False
        self.peak_motion = 0.0
        self.ticks = 0
        self._last_frame: tuple[np.ndarray, float] | None = None
        self._task: asyncio.Task | None = None
        self.skip_description = False  # set on shutdown: no slow VLM call

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop_monitoring(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self.ended_wall is None:
            self.ended_wall = time.time()

    def mark_motion_end(self) -> None:
        """Motion stopped (the grace period may still bring it back)."""
        self.ended_wall = time.time()

    def mark_motion_resume(self) -> None:
        self.ended_wall = None

    # ── monitoring loop ──────────────────────────────────────────────────────

    async def _loop(self) -> None:
        try:
            if not self.ai_on:
                await self._plain_alert()
            next_at = time.monotonic()
            while True:
                now = time.monotonic()
                if now < next_at:
                    await asyncio.sleep(next_at - now)
                # Scheduled from when the tick was due, not when it finished:
                # a slow inference skips ticks instead of backing them up.
                next_at += self.interval
                if time.monotonic() > next_at:
                    next_at = time.monotonic() + self.interval
                if not self._motion_active():
                    continue  # in the post-roll grace: nothing to analyse
                await self._tick()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Motion event monitoring crashed for camera %s", self.camera_id)

    async def _tick(self) -> None:
        self.ticks += 1
        self.peak_motion = max(self.peak_motion, self._motion_level() or 0.0)
        item = self._get_frame(self.camera_id)
        if item is None:
            return
        frame, captured_mono = item
        self._last_frame = item
        if not self.ai_on:
            return
        wall = time.time() - (time.monotonic() - captured_mono)
        jpeg = await asyncio.to_thread(_jpeg, frame, 85)
        objects = await self._detect(self.settings, jpeg) if jpeg else None
        if objects is None:
            # AI broken for this tick. Never let that silence the event: fall
            # back to the plain motion alert, exactly as without AI.
            self.ai_failed = True
            if not self.alerted:
                await self._plain_alert()
            return
        self.ai_worked = True
        newly = self.tracker.update(objects, captured_mono, wall, frame)
        if self.tracker.holding(captured_mono, self.STATIC_HOLD_MAX):
            self._hold_motion(self.HOLD_SECONDS)

        if not self.alerted and newly:
            boxes = [(t.label, t.best_conf, t.box) for t in self.tracker.confirmed() if t.left_at is None and t.last_seen == captured_mono]
            snap = await asyncio.to_thread(lambda: _jpeg(draw_boxes(frame, boxes)))
            totals = {}
            for label, _c, _b in boxes:
                totals[label] = totals.get(label, 0) + 1
            what = " + ".join(plural(lbl, n) if n > 1 else lbl.capitalize() for lbl, n in sorted(totals.items()))
            await self._send_alert(f"{what} detected on '{self.camera_name}'", snap)
        elif not self.alerted and not self.objects_only and time.monotonic() - self.started_mono >= self.AI_GRACE_SECONDS:
            await self._plain_alert()

    async def _plain_alert(self) -> None:
        item = self._get_frame(self.camera_id)
        snap = await asyncio.to_thread(_jpeg, item[0]) if item else None
        await self._send_alert(f"Motion detected on '{self.camera_name}'", snap)

    async def _send_alert(self, message: str, snapshot: bytes | None) -> None:
        self.alerted = True
        try:
            self.alert_event_id = await self._emit(self.camera_id, "motion", message, snapshot)
        except Exception:
            logger.exception("Could not emit motion alert for camera %s", self.camera_id)

    # ── end of event ─────────────────────────────────────────────────────────

    def _clock(self, wall: float) -> str:
        return datetime.fromtimestamp(wall, self.tz).strftime("%H:%M:%S")

    async def build_summary(self, clip: tuple[float, float] | None = None) -> EventSummary | None:
        """None when there's nothing worth telling: objects-only mode and the
        AI worked but saw nothing of interest (a tree, a shadow)."""
        end = self.ended_wall or time.time()
        now_mono = time.monotonic()
        for t in self.tracker.confirmed():
            if t.left_at is None:
                # Still tracked when motion ended: they left when last seen.
                t.left_at = t.last_seen
                self.tracker.timeline.append((time.time() - (now_mono - t.last_seen), f"{t.label} #{t.id} left"))
        seen = self.tracker.totals()
        if self.ai_on and self.ai_worked and not self.ai_failed and not seen and self.objects_only:
            return None

        start_dt = datetime.fromtimestamp(self.started_wall, self.tz)
        tzname = start_dt.strftime("%Z") or ""
        lines = [
            f"Motion on '{self.camera_name}' {start_dt.strftime('%d %b %H:%M:%S')}"
            f" - {self._clock(end)} {tzname} ({format_duration(end - self.started_wall)})".rstrip()
        ]

        best: Track | None = None
        if seen:
            lines.append("Seen: " + ", ".join(plural(lbl, n) for lbl, n in sorted(seen.items(), key=lambda kv: -kv[1])))
            peaks = [plural(lbl, n) for lbl, n in self.tracker.peak.items() if n > 1]
            if peaks:
                lines.append("Most at once: " + ", ".join(peaks))
            events = sorted(self.tracker.timeline)[:12]
            if events:
                lines.append("Timeline:")
                lines += [f"  {self._clock(w)}  {text}" for w, text in events]
            best = max(
                self.tracker.confirmed(),
                key=lambda t: (t.label == "person", t.best_score),
            )
        elif self.ai_on and self.ai_worked:
            lines.append("No people, vehicles or animals were recognised.")
        else:
            lines.append(f"Peak motion: {self.peak_motion:.1f}% of the watched area. (Object detection off.)")

        if clip:
            lines.append(f"Clip: {self._clock(clip[0])} - {self._clock(clip[1])} ({format_duration(clip[1] - clip[0])})")

        snapshot: bytes | None = None
        if best is not None and best.best_frame is not None:
            boxes = [(t.label, t.best_conf, t.best_box) for t in self.tracker.confirmed() if t.best_frame is best.best_frame]
            snapshot = await asyncio.to_thread(lambda: _jpeg(draw_boxes(best.best_frame, boxes)))
            description = await self._vlm_description(best, seen, end)
            if description:
                lines.insert(1, description)
        elif self._last_frame is not None:
            snapshot = await asyncio.to_thread(_jpeg, self._last_frame[0])

        message = "\n".join(lines)
        # Telegram/WhatsApp photo captions stop at 1024 characters, and the
        # alert subject line is prepended to this.
        if len(message) > 950:
            message = message[:947].rsplit("\n", 1)[0] + "\n..."
        return EventSummary(message, snapshot, seen)

    async def _vlm_description(self, best: Track, seen: dict[str, int], end: float) -> str:
        s = self.settings
        if self.skip_description or not (self._describe and s and s.vlm_enabled and getattr(s, "event_summary_vlm", True)):
            return ""
        jpeg = await asyncio.to_thread(_jpeg, best.best_frame)
        if not jpeg:
            return ""
        seen_text = ", ".join(plural(lbl, n) for lbl, n in seen.items())
        prompt = (
            f"This is the clearest frame from a {format_duration(end - self.started_wall)} motion event on the "
            f"home security camera '{self.camera_name}'. Seen during the event: {seen_text}. Describe what is "
            "happening in at most two factual sentences (40 words). No preamble, no speculation beyond the image."
        )
        try:
            return await asyncio.wait_for(
                self._describe(s, jpeg, list(seen), self.camera_name, prompt), timeout=self.VLM_TIMEOUT
            )
        except Exception as exc:
            logger.warning("Event summary description failed for camera %s (%s)", self.camera_id, exc)
            return ""
