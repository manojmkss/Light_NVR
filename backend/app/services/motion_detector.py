"""Motion detection: before/after frame comparison, with a background model to
keep an event open while something stays in the scene.

Pure logic - no threads, no I/O, no clock of its own - so it can be driven by
synthetic frames in tests exactly as it is by the live decoder.

Why it is built this way (each point fixes something the previous pure-MOG2
detector got wrong):

- The trigger is frame differencing: the current frame against the one from
  ~0.4s earlier. That is a direct "what changed" comparison, and unlike a
  learned background it can't slowly absorb a person who walks in.
- MOG2 is still used, but only as a "something is still here" signal, with a
  fixed low learning rate. A person who stops walking produces no frame
  difference, yet stays foreground to MOG2 for minutes, so the event (and the
  recording) doesn't end while they are standing at the door.
- Changes are measured as a percentage of the watched area and summed across
  blobs, so a person split into several pieces, or a smaller/distant one,
  still counts, and the number means the same thing on any camera resolution.
- A trigger needs several of the last few samples to agree. A single noisy
  frame, a compression glitch, or one bird is not an event.
- If most of the scene changes at once, that is lighting (IR switching, lights
  on, auto-exposure, a cloud), not an intruder: the sample is rejected and the
  background relearned instead of starting an event.
- Frames are blurred first so sensor noise and compression artefacts don't
  register as change.
- Warmup is counted in samples actually processed, and repeats after every
  reconnect and every lighting reset.
"""

from __future__ import annotations

import collections
import logging
from dataclasses import dataclass

import cv2
import numpy as np

logger = logging.getLogger(__name__)

WORK_WIDTH = 640  # analysis width; height follows the camera's aspect ratio


@dataclass
class MotionSample:
    """The detector's view of one analysed frame."""

    active: bool  # is a motion event in progress after this sample
    started: bool  # did an event start on this sample
    stopped: bool  # did an event end on this sample
    change_pct: float  # % of the watched area that changed vs ~0.4s ago
    presence_pct: float  # % of the watched area that differs from the background
    bbox: tuple[float, float, float, float] | None  # union box of the change, normalised x,y,w,h
    rejected: bool = False  # sample ignored as a whole-scene lighting change


def sensitivity_to_pct(sensitivity: int) -> float:
    """Slider 1-100 -> % of the watched area that must change to trigger.

    Geometric rather than linear: the useful range spans an order of magnitude
    (a person filling the doorway vs one across the street), and a linear map
    spends most of the slider on settings nobody can tell apart.
    1 -> 3.0%, 50 -> ~0.68%, 100 -> 0.15%.
    """
    s = max(1, min(100, int(sensitivity)))
    return 3.0 * (0.05 ** ((s - 1) / 99))


def build_zone_mask(zones: list | None, width: int, height: int) -> np.ndarray | None:
    """Rasterise motion zones (normalised 0-1 polygons) into a uint8 mask
    (255 = watch, 0 = ignore) at the given size. With include zones, only those
    are watched; otherwise the whole frame minus exclude zones. None means
    "watch everything". Malformed data degrades to None rather than crashing the
    detection thread."""
    if not zones:
        return None
    try:

        def to_pts(points: list) -> np.ndarray:
            return np.array(
                [[int(round(float(x) * width)), int(round(float(y) * height))] for x, y in points],
                dtype=np.int32,
            )

        includes = [z for z in zones if z.get("kind") == "include" and len(z.get("points", [])) >= 3]
        excludes = [z for z in zones if z.get("kind") == "exclude" and len(z.get("points", [])) >= 3]
        if not includes and not excludes:
            return None

        if includes:
            mask = np.zeros((height, width), dtype=np.uint8)
            for z in includes:
                cv2.fillPoly(mask, [to_pts(z["points"])], 255)
        else:
            mask = np.full((height, width), 255, dtype=np.uint8)
        for z in excludes:
            cv2.fillPoly(mask, [to_pts(z["points"])], 0)
        return mask
    except Exception:
        logger.warning("Ignoring malformed motion zones (bad data)")
        return None


class MotionDetector:
    SAMPLE_INTERVAL = 1 / 6  # analyse ~6 frames/s regardless of camera fps
    COMPARE_AGO = 0.4  # "before" frame is this many seconds older than "after"
    WARMUP_SECONDS = 3.0
    PERSIST_WINDOW = 5  # look at the last N samples...
    PERSIST_NEEDED = 3  # ...and require this many over threshold to start
    END_QUIET_SECONDS = 3.0  # end the event after this long with nothing seen
    PRESENCE_HOLD_MAX = 120.0  # background-only "still there" can hold an event this long
    GLOBAL_CHANGE_PCT = 45.0  # more than this changing at once = lighting, not motion
    REJECT_HOLD_SECONDS = 2.0
    DIFF_THRESHOLD = 25  # grey-level difference that counts as "changed"
    # MOG2 absorbs a new, static thing into the background after roughly
    # ln(0.9)/ln(1 - rate) samples. Idle: ~50 samples (~9s), so a moved chair
    # stops counting quickly. During an event: ~1000 samples (~3 min), so a
    # person who stops walking stays "present" - capped by PRESENCE_HOLD_MAX.
    MOG_LEARN_IDLE = 0.002
    MOG_LEARN_ACTIVE = 0.0001

    def __init__(self, sensitivity: int = 50, zones: list | None = None):
        self.trigger_pct = sensitivity_to_pct(sensitivity)
        self._zones = zones
        self._size: tuple[int, int] | None = None
        self._zone_mask: np.ndarray | None = None
        self._zone_px = 1
        self._kernel_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self._kernel_grow = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        self.reset(now=None)
        self.active = False
        self._external_hold_until = 0.0

    # ── lifecycle ─────────────────────────────────────────────────────────

    def reset(self, now: float | None) -> None:
        """Forget the scene: new background, empty comparison history, fresh
        warmup. Called on (re)connect and after a lighting change."""
        self._mog = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=25, detectShadows=True)
        self._past: collections.deque[tuple[float, np.ndarray]] = collections.deque()
        self._hits: collections.deque[bool] = collections.deque(maxlen=self.PERSIST_WINDOW)
        self._warm_until = None if now is None else now + self.WARMUP_SECONDS
        self._last_sample_at = -1e9
        self._last_change_at = -1e9
        self._last_presence_only_start: float | None = None

    def hold(self, until: float) -> None:
        """Keep an active event open until `until` (monotonic seconds) even with
        no pixel change - used when AI still sees a person standing still.
        Safe to call from another thread: a single float assignment."""
        self._external_hold_until = max(self._external_hold_until, until)

    def force_stop(self) -> bool:
        """End any active event (stream dropped). Returns whether one was active."""
        was = self.active
        self.active = False
        return was

    # ── per-frame ─────────────────────────────────────────────────────────

    def _prepare(self, frame_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h, w = frame_bgr.shape[:2]
        work_w = min(WORK_WIDTH, w)
        work_h = max(1, int(round(h * work_w / w)))
        if self._size != (work_w, work_h):
            # (Re)build the zone mask at the camera's real aspect ratio - the old
            # detector forced 640x360, which put 4:3 zones in the wrong place.
            self._size = (work_w, work_h)
            self._zone_mask = build_zone_mask(self._zones, work_w, work_h)
            self._zone_px = int(np.count_nonzero(self._zone_mask)) if self._zone_mask is not None else work_w * work_h
            self._zone_px = max(1, self._zone_px)
        small = cv2.resize(frame_bgr, self._size, interpolation=cv2.INTER_AREA) if (w, h) != self._size else frame_bgr
        small = cv2.GaussianBlur(small, (5, 5), 0)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        return small, gray

    def _masked_pct(self, mask: np.ndarray) -> float:
        if self._zone_mask is not None:
            mask = cv2.bitwise_and(mask, self._zone_mask)
        return 100.0 * np.count_nonzero(mask) / self._zone_px

    def _reference(self, now: float) -> np.ndarray | None:
        """The newest stored frame that is at least COMPARE_AGO old."""
        ref = None
        for t, g in self._past:
            if now - t >= self.COMPARE_AGO:
                ref = g
            else:
                break
        return ref

    def process(self, frame_bgr: np.ndarray, now: float) -> MotionSample | None:
        """Feed every decoded frame; returns a sample only for the frames
        actually analysed (~6/s), None for the rest."""
        if now - self._last_sample_at < self.SAMPLE_INTERVAL:
            return None
        self._last_sample_at = now
        if self._warm_until is None:
            self._warm_until = now + self.WARMUP_SECONDS

        small, gray = self._prepare(frame_bgr)
        warming = now < self._warm_until

        # Background model: learn fast while warming, slowly afterwards.
        if warming:
            rate = -1
        else:
            rate = self.MOG_LEARN_ACTIVE if self.active else self.MOG_LEARN_IDLE
        fg = self._mog.apply(small, learningRate=rate)
        _, fg = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)  # drops shadows (127)
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, self._kernel_open)
        presence_pct = self._masked_pct(fg)

        # Before/after comparison.
        ref = self._reference(now)
        self._past.append((now, gray))
        while self._past and now - self._past[0][0] > self.COMPARE_AGO * 3:
            self._past.popleft()

        change_pct = 0.0
        bbox = None
        if ref is not None:
            diff = cv2.absdiff(gray, ref)
            _, changed = cv2.threshold(diff, self.DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)
            changed = cv2.morphologyEx(changed, cv2.MORPH_OPEN, self._kernel_open)
            change_pct = self._masked_pct(changed)
            if change_pct > 0:
                grown = cv2.dilate(changed, self._kernel_grow, iterations=2)
                if self._zone_mask is not None:
                    grown = cv2.bitwise_and(grown, self._zone_mask)
                bbox = self._union_bbox(grown)

        started = stopped = False

        # Whole-scene change: lighting, not motion. Relearn instead of alerting.
        if change_pct >= self.GLOBAL_CHANGE_PCT:
            was_active = self.active
            self.reset(now)
            self.active = was_active
            self._warm_until = now + self.REJECT_HOLD_SECONDS
            # An event already in progress is not ended by a lighting change;
            # it ends normally once things go quiet.
            self._last_change_at = now if was_active else self._last_change_at
            return MotionSample(self.active, False, False, change_pct, presence_pct, None, rejected=True)

        hit = (not warming) and change_pct >= self.trigger_pct
        self._hits.append(hit)

        if hit or (self.active and change_pct >= self.trigger_pct * 0.5):
            # Hysteresis: once an event is running, half the trigger level keeps it alive.
            self._last_change_at = now
            self._last_presence_only_start = None

        if not self.active:
            if sum(self._hits) >= self.PERSIST_NEEDED:
                self.active = True
                started = True
                self._last_change_at = now
                self._last_presence_only_start = None
        else:
            quiet_for = now - self._last_change_at
            still_present = presence_pct >= self.trigger_pct
            if still_present and quiet_for > self.END_QUIET_SECONDS:
                if self._last_presence_only_start is None:
                    self._last_presence_only_start = now
                still_present = now - self._last_presence_only_start < self.PRESENCE_HOLD_MAX
            held = now < self._external_hold_until
            if quiet_for > self.END_QUIET_SECONDS and not still_present and not held:
                self.active = False
                stopped = True
                self._hits.clear()

        return MotionSample(self.active, started, stopped, change_pct, presence_pct, bbox)

    def _union_bbox(self, mask: np.ndarray) -> tuple[float, float, float, float] | None:
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        min_area = 0.0002 * self._zone_px  # ignore specks when sizing the box
        boxes = [cv2.boundingRect(c) for c in contours if cv2.contourArea(c) >= min_area]
        if not boxes:
            return None
        x1 = min(b[0] for b in boxes)
        y1 = min(b[1] for b in boxes)
        x2 = max(b[0] + b[2] for b in boxes)
        y2 = max(b[1] + b[3] for b in boxes)
        w, h = self._size
        return (x1 / w, y1 / h, (x2 - x1) / w, (y2 - y1) / h)
