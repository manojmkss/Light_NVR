"""One motion event, watched by AI from start to finish.

While motion is active the session grabs the newest native-resolution frame
every `interval_ms` (500ms by default) and runs object detection on it. Ticks
never queue up: if an inference is still running when the next tick is due,
that tick is skipped. Detections are tied together across ticks by a small
tracker, so "a person walked across" is one person, not six.

It produces:
- one immediate alert per event - on the first confirmed object (or plain
  motion when AI is off/failing, or after a short grace when objects-only
  alerting is off) - plus a follow-up when another person arrives later;
- one end-of-event summary: when, how long, what was seen (counts, peak,
  appear/leave timeline, how long someone stood still), the clip, and
  optionally a VLM description of the clearest frame.

What keeps an event open is decided per kind of object (CLASS_POLICY):
- a person keeps it open while seen, moving or not - someone waiting at the
  gate for five minutes is one visit - up to a 15-minute stationary cap;
- a vehicle that stops becomes *parked*: it never holds the event, never
  alerts, and is remembered across events so the car in the driveway isn't
  "seen" again every time someone walks past.
Objects whose position is outside the watched area (motion zones) are tracked
but never alert, hold or count.
"""

import asyncio
import logging
import math
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

import cv2
import numpy as np

from app.services.ai.scheduler import P_MOVING, P_START, P_STATIONARY, SKIPPED
from app.services.ai.types import DetectedObject

logger = logging.getLogger(__name__)

_PLURALS = {"person": "people", "bus": "buses"}

# YOLO flips between neighbouring classes from frame to frame (car <-> truck);
# tracks match within a group so one vehicle stays one track.
LABEL_GROUPS = {
    "person": "person",
    "car": "vehicle", "truck": "vehicle", "bus": "vehicle",
    "motorcycle": "two_wheeler", "bicycle": "two_wheeler",
    "dog": "animal", "cat": "animal", "bird": "animal", "horse": "animal", "cow": "animal", "sheep": "animal",
    # A stock detector has no "parcel" class; these carried-item classes are
    # the closest it gets (see types.PACKAGE_PROXY_CLASSES).
    "backpack": "package", "handbag": "package", "suitcase": "package",
}


@dataclass(frozen=True)
class ClassPolicy:
    lost_after: float = 4.0  # seconds unseen before a track counts as gone
    static_hold_max: float = 60.0  # how long a non-moving one keeps the event open
    park_after: float | None = None  # stationary this long -> parked (never holds/alerts)


CLASS_POLICY: dict[str, ClassPolicy] = {
    "person": ClassPolicy(lost_after=6.0, static_hold_max=900.0),
    "vehicle": ClassPolicy(lost_after=4.0, static_hold_max=0.0, park_after=20.0),
    "two_wheeler": ClassPolicy(lost_after=4.0, static_hold_max=0.0, park_after=30.0),
    "animal": ClassPolicy(lost_after=4.0, static_hold_max=60.0),
    # Left lying for 10s = a delivered package; it never holds or alerts as
    # an object, but its arrival and removal are events.
    "package": ClassPolicy(lost_after=4.0, static_hold_max=0.0, park_after=10.0),
}
DEFAULT_POLICY = ClassPolicy()


def group_of(label: str) -> str:
    return LABEL_GROUPS.get(label, label)


def policy_of(group: str) -> ClassPolicy:
    return CLASS_POLICY.get(group, DEFAULT_POLICY)


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


def _grow(box: tuple, frac: float) -> tuple:
    x, y, w, h = box
    return (x - w * frac, y - h * frac, w * (1 + 2 * frac), h * (1 + 2 * frac))


def _center(box: tuple) -> tuple[float, float]:
    return box[0] + box[2] / 2, box[1] + box[3] / 2


def anchor_point(group: str, box: tuple) -> tuple[float, float]:
    """Where an object *is* for zone purposes: a person stands at their feet
    (the head of someone on the pavement can overlap the garden), anything
    else at its centre."""
    x, y, w, h = box
    if group == "person":
        return x + w / 2, y + h
    return x + w / 2, y + h / 2


def _side(a: tuple, b: tuple, p: tuple) -> float:
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def _within_span(a: tuple, b: tuple, p: tuple, margin: float = 0.05) -> bool:
    """Is p alongside the segment a-b (not beyond its ends)? A tripwire across
    the gate shouldn't fire for someone walking past far down the street."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return False
    u = ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2
    return -margin <= u <= 1 + margin


def _inside(points: list, p: tuple) -> bool:
    pts = np.array([[float(x), float(y)] for x, y in points], dtype=np.float32)
    return cv2.pointPolygonTest(pts, (float(p[0]), float(p[1])), False) >= 0


def _wants(rule: dict, group: str) -> bool:
    objects = rule.get("objects") or []
    return not objects or group in {group_of(o) for o in objects}


@dataclass
class ParkedObject:
    """A vehicle known to be standing still on a camera, carried between events."""

    group: str
    label: str
    box: tuple
    since: float  # wall clock


@dataclass
class Track:
    id: int  # per-label sequence number once announced: person #1, person #2...
    group: str
    first_seen: float
    last_seen: float
    box: tuple
    labels: Counter = field(default_factory=Counter)
    hits: int = 0
    confirmed: bool = False
    announced: bool = False  # confirmed + in the watched area + not parked: counts, alerts
    in_zone: bool = False
    parked: bool = False
    seeded: bool = False  # came from the parked registry, not seen arriving
    misses: int = 0  # consecutive analysed ticks without a match
    left_at: float | None = None
    anchor: tuple = (0.0, 0.0, 0.0, 0.0)  # box when it last "moved"
    last_moved: float = 0.0
    max_still: float = 0.0  # longest stretch standing still, seconds
    best_score: float = -1.0
    best_conf: float = 0.0
    best_box: tuple = (0.0, 0.0, 0.0, 0.0)
    best_frame: np.ndarray | None = None
    best_at: float = 0.0
    since_wall: float = 0.0
    vel: tuple = (0.0, 0.0)  # centre velocity, frame fractions per second
    package_pending: bool = False
    last_wall: float = 0.0
    zones: list = field(default_factory=list)  # named zones entered, in order
    zone_hits: dict = field(default_factory=dict)  # consecutive observations inside, per zone
    dwell: dict = field(default_factory=dict)  # seconds spent inside, per zone
    loitered: set = field(default_factory=set)
    line_side: dict = field(default_factory=dict)  # last non-zero side of each line
    # Faces (only when face recognition is on).
    face_tries: int = 0
    last_face_try: float = -1e9
    face_votes: Counter = field(default_factory=Counter)  # person_id -> agreeing sightings
    identity: dict | None = None  # {"id", "name", "note", "category", "score"} once recognised
    possibly: str | None = None  # "probable" name, never treated as recognised
    best_face: object = None  # Face with the highest quality (saved for "Who is this?")
    best_face_score: float | None = None
    best_face_at: float | None = None  # wall time of the frame the best face is from
    unknown_face: bool = False
    # Re-identification within the event (see ObjectTracker._comeback).
    sig: tuple | None = None  # (upper-body, lower-body) colour histograms, averaged
    sig_n: int = 0
    reid_of: "Track | None" = None  # the earlier track this one is the same as
    came_back: bool = False  # a later track was found to be this one returning

    def predicted(self, now: float) -> tuple:
        """Where the box should be now, given how it was moving - ticks get
        skipped under load, and at 2 fps a walker moves a long way between
        looks."""
        dt = max(0.0, min(2.0, now - self.last_seen))
        x, y, w, h = self.box
        return (x + self.vel[0] * dt, y + self.vel[1] * dt, w, h)

    @property
    def label(self) -> str:
        # Majority vote: a car the model calls "truck" twice is still a car.
        return self.labels.most_common(1)[0][0] if self.labels else self.group

    @property
    def policy(self) -> ClassPolicy:
        return policy_of(self.group)


# ── appearance signature (re-identification within an event) ───────────────

REID_GROUPS = ("person", "animal")
REID_WINDOW = 300.0  # a comeback within 5 minutes of leaving is considered
REID_SIM = 0.8  # how alike the clothing colours must be (histogram correlation)


def _identities_differ(a: "Track", b: "Track") -> bool:
    return bool(a.identity and b.identity and a.identity.get("id") != b.identity.get("id"))


def appearance_signature(frame: np.ndarray, box: tuple) -> tuple | None:
    """Colour make-up of the upper and lower half of a box: what someone is
    wearing, roughly - enough to tell "the same person came back into view"
    from "someone else arrived" without a heavyweight re-ID model."""
    H, W = frame.shape[:2]
    x, y, w, h = box
    x1, y1 = max(0, int(x * W)), max(0, int(y * H))
    x2, y2 = min(W, int((x + w) * W)), min(H, int((y + h) * H))
    if x2 - x1 < 8 or y2 - y1 < 16:
        return None
    crop = cv2.resize(frame[y1:y2, x1:x2], (32, 64), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    out = []
    for part in (hsv[6:32], hsv[32:60]):  # skip the very top (head/background) and the feet
        hist = cv2.calcHist([part], [0, 1], None, [12, 6], [0, 180, 0, 256]).ravel()
        total = float(hist.sum()) or 1.0
        out.append((hist / total).astype(np.float32))
    return tuple(out)


def signature_similarity(a: tuple, b: tuple) -> float:
    return float(np.mean([cv2.compareHist(x, y, cv2.HISTCMP_CORREL) for x, y in zip(a, b)]))


class ObjectTracker:
    """Greedy IoU matching per label group, with a centre-distance fallback for
    fast movers. A track must be seen twice (or once with high confidence)
    before it counts - that filters single-frame false positives."""

    MATCH_IOU = 0.2
    LOW_MATCH_IOU = 0.3  # weak detections may only extend a track, and need a closer match
    CONFIRM_HITS = 2
    CONFIRM_CONF = 0.7
    # Movement is relative to the object's own size, so fidgeting close to the
    # camera isn't "walking" and a distant walker isn't "standing still".
    MOVE_SHIFT = 0.25  # centre shift as a fraction of max(w, h)
    MOVE_IOU = 0.6  # ...or overlap with where it last moved below this
    PARKED_MISSES = 3  # a parked vehicle unseen this many analysed ticks has left
    MAX_SPEED = 0.3  # frame fractions per second assumed when widening the match radius

    ZONE_INERTIA = 2  # observations inside before a track "entered" a named zone

    def __init__(self, in_zone: Callable[[float, float], bool] | None = None,
                 parked: list[ParkedObject] | None = None, rules: list | None = None) -> None:
        self.tracks: list[Track] = []
        # Named zones ({"kind": "zone", "name", "points", "loiter_seconds"?,
        # "objects"?}) and lines ({"kind": "line", "name", "points": [a, b],
        # "direction": "both"|"forward"|"backward", "objects"?}).
        rules = rules or []
        self._named = [r for r in rules if r.get("kind") == "zone" and len(r.get("points", [])) >= 3]
        self._lines = [r for r in rules if r.get("kind") == "line" and len(r.get("points", [])) >= 2]
        self.events: list[tuple[str, Track, dict, float]] = []  # (kind, track, rule, value) for the session
        self.peak: dict[str, int] = {}
        self._seq: dict[str, int] = {}
        self.timeline: list[tuple[float, str]] = []  # (wall time, text)
        self._in_zone = in_zone or (lambda _x, _y: True)
        for p in parked or []:
            t = Track(id=0, group=p.group, first_seen=0.0, last_seen=0.0, box=p.box,
                      labels=Counter({p.label: 1}), hits=self.CONFIRM_HITS, confirmed=True,
                      parked=True, seeded=True, anchor=p.box, since_wall=p.since)
            t.in_zone = self._in_zone(*anchor_point(p.group, p.box))
            self.tracks.append(t)

    def _match_score(self, track: Track, obj: DetectedObject, now: float, weak: bool = False) -> float:
        box = (obj.x, obj.y, obj.w, obj.h)
        pred = track.predicted(now)
        iou = max(_iou(pred, box), _iou(track.box, box))
        if iou >= (self.LOW_MATCH_IOU if weak else self.MATCH_IOU):
            return iou
        if weak:
            return 0.0
        (tx, ty), (ox, oy) = _center(pred), _center(box)
        size = max(track.box[2], track.box[3], obj.w, obj.h)
        # The longer it went unseen (skipped or slowed-down ticks), the further
        # it can have walked: widen the search with the gap.
        gap = max(0.0, min(3.0, now - track.last_seen))
        radius = 0.6 * size + self.MAX_SPEED * gap
        return 0.1 if math.hypot(tx - ox, ty - oy) < radius else 0.0

    def _moved(self, t: Track, box: tuple) -> bool:
        (ax, ay), (bx, by) = _center(t.anchor), _center(box)
        size = max(t.anchor[2], t.anchor[3], 1e-6)
        return math.hypot(bx - ax, by - ay) > self.MOVE_SHIFT * size or _iou(t.anchor, box) < self.MOVE_IOU

    def _announce(self, t: Track, wall: float, text: str, now: float | None = None) -> None:
        t.announced = True
        old = self._comeback(t, now) if now is not None else None
        if old is not None:
            # The same person walking back into view: same number, counted once.
            t.id, t.reid_of, old.came_back = old.id, old, True
            if old.identity and not t.identity:
                t.identity = old.identity
            self.timeline.append((wall, f"{t.label} #{t.id} came back"))
            return
        self._seq[t.label] = self._seq.get(t.label, 0) + 1
        t.id = self._seq[t.label]
        self.timeline.append((wall, text.format(name=f"{t.label} #{t.id}")))

    def _comeback(self, t: Track, now: float) -> Track | None:
        """An earlier track this new one is the same person (or animal) as:
        it left the view no more than REID_WINDOW ago, nobody has come back
        as it already, they weren't both in view at once, recognised faces
        don't say otherwise, and the clothing colours match closely."""
        if t.group not in REID_GROUPS or t.sig is None:
            return None
        best, best_sim = None, REID_SIM
        for old in self.tracks:
            if (old is t or not old.announced or old.group != t.group or old.left_at is None or old.came_back
                    or old.sig is None or old.left_at > t.first_seen or now - old.left_at > REID_WINDOW):
                continue
            if old.identity and t.identity and old.identity.get("id") != t.identity.get("id"):
                continue
            sim = signature_similarity(old.sig, t.sig)
            if sim >= best_sim:
                best, best_sim = old, sim
        return best

    def _update_signature(self, t: Track, frame: np.ndarray | None, box: tuple) -> None:
        # Every look at first, then every other one: cheap, and enough.
        if frame is None or t.group not in REID_GROUPS or (t.sig_n >= 4 and t.hits % 2):
            return
        sig = appearance_signature(frame, box)
        if sig is None:
            return
        if t.sig is None:
            t.sig = sig
        else:
            t.sig = tuple((0.7 * a + 0.3 * b).astype(np.float32) for a, b in zip(t.sig, sig))
        t.sig_n += 1

    def update(self, objects: list[DetectedObject], now: float, wall: float, frame: np.ndarray | None,
               high: float = 0.0) -> list[Track]:
        """Feed one tick's detections. Returns tracks announced this tick
        (newly confirmed, in the watched area, not parked).

        Two-stage matching (ByteTrack's idea): detections at or above `high`
        are matched first and may start new tracks; weaker ones are then used
        only to keep following tracks left unmatched - a person half behind a
        pillar or turning away scores low, but is still the same person."""
        for t in self.tracks:
            if t.left_at is not None:
                continue
            if t.parked:
                continue  # parked tracks leave on misses, below
            if now - t.last_seen > t.policy.lost_after:
                t.left_at = t.last_seen
                if t.announced:
                    self.timeline.append((wall - (now - t.last_seen), f"{t.label} #{t.id} left"))
        active = [t for t in self.tracks if t.left_at is None]

        strong = [i for i, o in enumerate(objects) if o.confidence >= high]
        weak = [i for i, o in enumerate(objects) if o.confidence < high]
        used_t: set[int] = set()
        used_o: set[int] = set()
        matched: list[tuple[Track, DetectedObject]] = []
        for indices, is_weak in ((strong, False), (weak, True)):
            pairs = []
            for ti, t in enumerate(active):
                if ti in used_t:
                    continue
                for oi in indices:
                    o = objects[oi]
                    if group_of(o.label) == t.group:
                        s = self._match_score(t, o, now, weak=is_weak)
                        if s > 0:
                            pairs.append((s, ti, oi))
            pairs.sort(reverse=True)
            for _s, ti, oi in pairs:
                if ti in used_t or oi in used_o:
                    continue
                used_t.add(ti)
                used_o.add(oi)
                matched.append((active[ti], objects[oi]))

        for ti, t in enumerate(active):
            if ti not in used_t and t.parked:
                t.misses += 1
                if t.misses >= self.PARKED_MISSES:
                    t.left_at = t.last_seen or now
                    if t.group == "package" and t.in_zone:
                        self.events.append(("package_removed", t, {"name": (t.zones[-1] if t.zones else None)}, 0.0))
                    elif t.in_zone:
                        name = f"{t.label} #{t.id}" if t.announced else f"parked {t.label}"
                        self.timeline.append((wall, f"{name} left"))

        for oi in strong:
            if oi in used_o:
                continue
            o = objects[oi]
            box = (o.x, o.y, o.w, o.h)
            t = Track(id=0, group=group_of(o.label), first_seen=now, last_seen=now, box=box,
                      anchor=box, last_moved=now, since_wall=wall)
            self.tracks.append(t)
            matched.append((t, o))

        newly: list[Track] = []
        for t, o in matched:
            box = (o.x, o.y, o.w, o.h)
            dt = now - t.last_seen
            if t.hits and 0 < dt <= 3.0:
                (ox, oy), (nx, ny) = _center(t.box), _center(box)
                v = ((nx - ox) / dt, (ny - oy) / dt)
                t.vel = (0.5 * t.vel[0] + 0.5 * v[0], 0.5 * t.vel[1] + 0.5 * v[1])
            t.box = box
            prev_seen = t.last_seen
            t.last_seen = now
            t.last_wall = wall
            t.hits += 1
            t.misses = 0
            t.labels[o.label] += 1
            if self._in_zone(*anchor_point(t.group, box)):
                t.in_zone = True

            if t.seeded and t.parked and self._moved(t, box):
                # The car that was standing in the driveway is driving off.
                t.parked = False
                t.anchor, t.last_moved = box, now
                if t.in_zone and not t.announced:
                    self._announce(t, wall, f"parked {t.label} started moving ({{name}})", now)
                    newly.append(t)
            elif self._moved(t, box):
                t.anchor, t.last_moved = box, now
            t.max_still = max(t.max_still, now - t.last_moved)

            self._update_signature(t, frame, box)
            score = o.confidence * o.w * o.h  # clearest view: confident and close
            if score > t.best_score:
                t.best_score, t.best_conf, t.best_box = score, o.confidence, box
                t.best_frame, t.best_at = frame, wall

            self._zone_rules(t, box, now - prev_seen if t.hits > 1 else 0.0, wall)

            if not t.confirmed and (t.hits >= self.CONFIRM_HITS or o.confidence >= self.CONFIRM_CONF):
                t.confirmed = True
            pol = t.policy
            if pol.park_after is not None and not t.parked and now - t.last_moved >= pol.park_after:
                t.parked = True
                if t.group == "package":
                    # Reported once nobody is holding/wearing it (below).
                    t.package_pending = t.in_zone and not t.seeded
                elif t.announced:
                    self.timeline.append((wall, f"{t.label} #{t.id} parked"))
            if t.confirmed and t.in_zone and not t.announced and not t.parked and t.group != "package":
                self._announce(t, wall, "{name} appeared", now)
                newly.append(t)

        # A bag standing still is only "left" once no person overlaps it -
        # otherwise it's just someone standing there with a backpack.
        people = [t.box for t in self.tracks if t.group == "person" and t.left_at is None and now - t.last_seen < 2.0]
        for t in self.tracks:
            if t.group == "package" and t.package_pending and t.left_at is None:
                # "Near" = within half a person's height (a parcel at their feet).
                if not any(_iou(t.box, _grow(p, 0.5)) > 0 for p in people):
                    t.package_pending = False
                    self.events.append(("package_left", t, {"name": (t.zones[-1] if t.zones else None)}, 0.0))

        counts: dict[str, int] = {}
        for t in self.tracks:
            if t.announced and t.left_at is None and t.last_seen == now and not t.parked:
                counts[t.label] = counts.get(t.label, 0) + 1
        for label, n in counts.items():
            self.peak[label] = max(self.peak.get(label, 0), n)
        return newly

    def _zone_rules(self, t: Track, box: tuple, dt: float, wall: float) -> None:
        point = anchor_point(t.group, box)
        dt = max(0.0, min(dt, 2.0))  # a skipped stretch doesn't count as dwelling
        for z in self._named:
            name = z.get("name") or "zone"
            if not _wants(z, t.group):
                continue
            if _inside(z["points"], point):
                t.zone_hits[name] = t.zone_hits.get(name, 0) + 1
                if t.zone_hits[name] >= self.ZONE_INERTIA:
                    if name not in t.zones:
                        t.zones.append(name)
                    t.dwell[name] = t.dwell.get(name, 0.0) + dt
                    limit = float(z.get("loiter_seconds") or 0)
                    if limit > 0 and t.dwell[name] >= limit and name not in t.loitered:
                        t.loitered.add(name)
                        self.events.append(("loitering", t, z, t.dwell[name]))
            else:
                t.zone_hits[name] = 0
        for ln in self._lines:
            if not _wants(ln, t.group):
                continue
            a, b = tuple(ln["points"][0]), tuple(ln["points"][1])
            name = ln.get("name") or "line"
            side = _side(a, b, point)
            if side == 0 or not _within_span(a, b, point):
                continue
            sign = 1 if side > 0 else -1
            prev = t.line_side.get(name)
            t.line_side[name] = sign
            if prev is not None and prev != sign:
                way = 1 if prev > 0 else -1  # +1: from the left of a->b to its right
                direction = ln.get("direction") or "both"
                if direction == "both" or (direction == "forward") == (way > 0):
                    self.events.append(("line_cross", t, ln, float(way)))

    def drain_events(self) -> list[tuple[str, Track, dict, float]]:
        """Zone/line events for announced tracks (one that's still being
        confirmed reports once it is). Events of tracks that never count are
        dropped."""
        ready, waiting = [], []
        for ev in self.events:
            t = ev[1]
            if t.announced or ev[0].startswith("package"):
                ready.append(ev)
            elif t.left_at is None and not t.parked:
                waiting.append(ev)
        self.events = waiting
        return ready

    def confirmed(self) -> list[Track]:
        """Tracks that count: announced (seen arriving in the watched area)."""
        return [t for t in self.tracks if t.announced]

    def totals(self) -> dict[str, int]:
        """Distinct people/objects: someone who left the view and came back is
        counted once (unless faces recognised later say it's someone else)."""
        out: dict[str, int] = {}
        for t in self.confirmed():
            if t.reid_of is not None and not _identities_differ(t, t.reid_of):
                continue
            out[t.label] = out.get(t.label, 0) + 1
        return out

    def visible_now(self, now: float, group: str | None = None) -> list[Track]:
        return [
            t for t in self.tracks
            if t.announced and t.left_at is None and not t.parked and t.last_seen == now
            and (group is None or t.group == group)
        ]

    def holding(self, now: float) -> bool:
        """Something real is still in view and its policy says it keeps the
        event open: a person at the door, moving or not (up to the cap)."""
        for t in self.tracks:
            if not t.announced or t.left_at is not None or t.parked:
                continue
            pol = t.policy
            if now - t.last_seen < pol.lost_after and now - t.last_moved < pol.static_hold_max:
                return True
        return False

    def all_still(self, now: float, for_seconds: float) -> list[Track] | None:
        """The visible announced tracks if every one of them has been standing
        still for `for_seconds`, else None."""
        vis = [t for t in self.tracks if t.announced and t.left_at is None and not t.parked
               and now - t.last_seen < t.policy.lost_after]
        if not vis or any(now - t.last_moved < for_seconds for t in vis):
            return None
        return vis

    def parked_objects(self) -> list[ParkedObject]:
        return [
            ParkedObject(t.group, t.label, t.box, t.since_wall)
            for t in self.tracks if t.parked and t.left_at is None
        ]


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


MAX_STORED_SIDE = 1920  # stored/alert pictures: plenty of detail, not 4MP files


def fit_for_storage(frame: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    side = max(h, w)
    if side <= MAX_STORED_SIDE:
        return frame
    k = MAX_STORED_SIDE / side
    return cv2.resize(frame, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA)


def _jpeg(frame: np.ndarray, quality: int = 90) -> bytes | None:
    ok, buf = cv2.imencode(".jpg", fit_for_storage(frame), [int(cv2.IMWRITE_JPEG_QUALITY), quality])
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


def _box_inside(inner: tuple, outer: tuple, margin: float = 0.15) -> bool:
    ox, oy, ow, oh = outer
    mx, my = ow * margin, oh * margin
    ix, iy, iw, ih = inner
    return ix >= ox - mx and iy >= oy - my and ix + iw <= ox + ow + mx and iy + ih <= oy + oh + my


@dataclass
class EventSummary:
    message: str
    snapshot: bytes | None
    seen: dict[str, int] = field(default_factory=dict)


class MotionEventSession:
    AI_GRACE_SECONDS = 2.0  # objects-only off: wait this long for an object before a plain motion alert
    MIN_HOLD_SECONDS = 4.0
    GRACE_INTERVAL = 1.0  # tick rate while motion has stopped (post-roll): can we still see someone?
    STILL_INTERVAL = 2.0  # tick rate while everything in view stands still
    STILL_AFTER = 10.0
    FOLLOW_UP_AFTER = 20.0  # another person arriving this long after the first alert gets its own alert
    STILL_NOTE_MIN = 30.0  # "stood still for ..." in the summary from this long
    # People rarely face the camera for long - someone walking or running about
    # turns towards it for a moment - so look often, for the whole time
    # they're in view (face search is cheap: only the top of each person box).
    FACE_RETRY = 1.5  # seconds between face attempts on one person
    FACE_MAX_TRIES = 40
    FACE_CLEAR_EXTRA = 0.08
    PRESENCE_HOLD_WITH_AI = 20.0  # background-model hold while AI is answering
    PRESENCE_HOLD_WITHOUT_AI = 120.0
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
        detect,  # async (settings, frame, camera_id, priority) -> list | None | SKIPPED
        emit,  # async (camera_id, type, message, snapshot_bytes, payload) -> event id | None
        describe=None,  # async (settings, jpeg, labels, camera_name, prompt) -> str
        tz=timezone.utc,
        in_zone: Callable[[float, float], bool] | None = None,
        parked: list[ParkedObject] | None = None,
        on_revive: Callable[[float], Awaitable[None]] | None = None,
        motion_bbox: Callable[[], tuple | None] | None = None,
        presence_hold: Callable[[float], None] | None = None,
        pace: Callable[[float], float] | None = None,
        rules: list | None = None,
        recognise=None,  # async (frame, boxes) -> [(index, Face, Match)]; None = faces off
        on_identity=None,  # async (track) -> None, when a track is recognised
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
        self._on_revive = on_revive
        self._motion_bbox = motion_bbox or (lambda: None)
        self._presence_hold = presence_hold or (lambda _s: None)
        self._pace = pace or (lambda wanted: wanted)
        self.high_conf = (getattr(settings, "detection_confidence", 50) or 50) / 100.0
        self._recognise = recognise
        self._on_identity = on_identity
        self._face_task: asyncio.Task | None = None
        self.tz = tz

        s = settings
        self.ai_on = bool(s and s.enabled and s.detection_enabled)
        self.objects_only = bool(s and s.alert_on_objects_only)
        interval_ms = getattr(s, "monitor_interval_ms", None) or 500
        self.interval = max(0.1, interval_ms / 1000.0)

        self.tracker = ObjectTracker(in_zone=in_zone, parked=parked, rules=rules)
        self.started_wall = time.time()
        self.started_mono = time.monotonic()
        self.ended_wall: float | None = None
        self.alert_event_id: int | None = None
        self.alerted = False
        self._alerted_mono = 0.0
        self.ai_worked = False
        self.ai_failed = False
        self.peak_motion = 0.0
        self.ticks = 0
        self.revives = 0
        self._revived_this_grace = False
        self._latency = 0.0  # EWMA of inference time
        self._last_frame: tuple[np.ndarray, float] | None = None
        self._task: asyncio.Task | None = None
        self.skip_description = False  # set on shutdown: no slow VLM call
        self._tick_errors = 0

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self.ai_on:
            self._presence_hold(self.PRESENCE_HOLD_WITHOUT_AI)
        self._task = asyncio.create_task(self._loop())

    async def stop_monitoring(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._face_task and not self._face_task.done():
            try:
                await asyncio.wait_for(self._face_task, timeout=5)
            except Exception:
                self._face_task.cancel()
        if self.ended_wall is None:
            self.ended_wall = time.time()

    def mark_motion_end(self) -> None:
        """Motion stopped (the post-roll grace may still bring it back)."""
        self.ended_wall = time.time()
        self._revived_this_grace = False

    def mark_motion_resume(self) -> None:
        self.ended_wall = None

    @property
    def hold_seconds(self) -> float:
        # Long enough that a slow or briefly-delayed inference can't let the
        # hold lapse between two ticks that both still see the person.
        return max(self.MIN_HOLD_SECONDS, 3 * self.interval, 2 * self._latency)

    def parked_objects(self) -> list[ParkedObject]:
        return self.tracker.parked_objects()

    # ── monitoring loop ──────────────────────────────────────────────────────

    def _next_interval(self) -> float:
        if not self._motion_active():
            return self.GRACE_INTERVAL
        still = self.tracker.all_still(time.monotonic(), self.STILL_AFTER) if self.ai_on else None
        if still:
            bbox = self._motion_bbox()
            # Whatever pixel motion there is sits on the people already standing
            # there (breathing, shifting weight): check less often.
            if bbox is None or any(_box_inside(bbox, t.box) for t in still):
                return max(self.STILL_INTERVAL, self._pace(self.STILL_INTERVAL))
        # Stretched when more cameras are busy than the hardware can serve.
        return self._pace(self.interval)

    def _priority(self, in_grace: bool) -> int:
        if in_grace:
            return P_STATIONARY
        if not self.alerted and self.ticks <= 3:
            return P_START  # the first looks decide how fast the alert goes out
        if self.tracker.all_still(time.monotonic(), self.STILL_AFTER):
            return P_STATIONARY
        return P_MOVING

    async def _loop(self) -> None:
        try:
            if not self.ai_on:
                await self._plain_alert()
            next_at = time.monotonic()
            slowed = False
            while True:
                now = time.monotonic()
                while now < next_at:
                    # Sleep in short steps so a slowed-down cadence (everyone
                    # standing still) ends the moment someone starts moving.
                    await asyncio.sleep(min(next_at - now, self.interval))
                    now = time.monotonic()
                    if (slowed and now < next_at and self._motion_active()
                            and self._next_interval() < self.STILL_INTERVAL):
                        next_at = now
                        break
                interval = self._next_interval()
                slowed = interval >= self.STILL_INTERVAL and self._motion_active()
                # Scheduled from when the tick was due, not when it finished:
                # a slow inference skips ticks instead of backing them up.
                next_at += interval
                if time.monotonic() > next_at:
                    next_at = time.monotonic() + interval
                active = self._motion_active()
                if not active and (not self.ai_on or self.ended_wall is None):
                    continue
                try:
                    await self._tick(in_grace=not active)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # One bad tick (a corrupt frame, a transient bug) must not
                    # end AI monitoring for the rest of the event.
                    self._tick_errors += 1
                    if self._tick_errors <= 3 or self._tick_errors % 100 == 0:
                        logger.exception("Motion event tick failed for camera %s (%d so far)",
                                         self.camera_id, self._tick_errors)
                    await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Motion event monitoring crashed for camera %s", self.camera_id)

    async def _tick(self, in_grace: bool = False) -> None:
        self.ticks += 1
        if not in_grace:
            self.peak_motion = max(self.peak_motion, self._motion_level() or 0.0)
        item = self._get_frame(self.camera_id)
        if item is None:
            return
        frame, captured_mono = item
        self._last_frame = item
        if not self.ai_on:
            return
        wall = time.time() - (time.monotonic() - captured_mono)
        t0 = time.monotonic()
        objects = await self._detect(self.settings, frame, self.camera_id, self._priority(in_grace))
        took = time.monotonic() - t0
        if objects is SKIPPED:
            return  # a newer frame took this one's place, or it ran out of time
        self._latency = took if self._latency == 0 else 0.8 * self._latency + 0.2 * took
        if objects is None:
            # AI broken for this tick. Never let that silence the event: fall
            # back to the plain motion alert, exactly as without AI, and let the
            # background model hold the event again.
            self.ai_failed = True
            self._presence_hold(self.PRESENCE_HOLD_WITHOUT_AI)
            if not self.alerted and not in_grace:
                await self._plain_alert(ai_failed=True)
            return
        if not self.ai_worked:
            self._presence_hold(self.PRESENCE_HOLD_WITH_AI)
        self.ai_worked = True
        newly = self.tracker.update(objects, captured_mono, wall, frame, high=self.high_conf)
        holding = self.tracker.holding(captured_mono)

        if in_grace:
            # Pixels went quiet, but is someone still standing there?
            if holding and self.tracker.visible_now(captured_mono, "person") and not self._revived_this_grace:
                self._revived_this_grace = True
                self.revives += 1
                if self._on_revive is not None:
                    await self._on_revive(self.hold_seconds)
            return

        if holding:
            self._hold_motion(self.hold_seconds)

        for kind, t, rule, value in self.tracker.drain_events():
            await self._zone_event(kind, t, rule, value, frame)

        self._maybe_faces(frame, captured_mono)

        if newly:
            visible = self.tracker.visible_now(captured_mono)
            if not self.alerted:
                await self._object_alert(frame, visible, captured_mono)
            elif any(t.group == "person" and t.reid_of is None for t in newly) and \
                    time.monotonic() - self._alerted_mono >= self.FOLLOW_UP_AFTER:
                people = len([t for t in visible if t.group == "person"])
                snap = await asyncio.to_thread(
                    lambda: _jpeg(draw_boxes(frame, [(t.label, t.best_conf, t.box) for t in visible]))
                )
                text = (
                    f"Another person arrived on '{self.camera_name}' ({plural('person', people)} now)"
                    if people > 1 else f"A person arrived on '{self.camera_name}' again"
                )
                await self._emit_safe("motion_update", text, snap, self._payload(visible))
        elif not self.alerted and not self.objects_only and time.monotonic() - self.started_mono >= self.AI_GRACE_SECONDS:
            await self._plain_alert()

    # ── faces ────────────────────────────────────────────────────────────────

    def _maybe_faces(self, frame: np.ndarray, now: float) -> None:
        """Look for faces on people who still need one - in the background,
        one batch at a time, so face work never delays the next detection."""
        if self._recognise is None or (self._face_task and not self._face_task.done()):
            return
        due = [t for t in self.tracker.visible_now(now, "person")
               if t.identity is None and t.face_tries < self.FACE_MAX_TRIES and now - t.last_face_try >= self.FACE_RETRY]
        if not due:
            return
        for t in due:
            t.face_tries += 1
            t.last_face_try = now
        wall = time.time() - max(0.0, time.monotonic() - now)
        self._face_task = asyncio.create_task(self._faces(frame, due, wall))

    async def _faces(self, frame: np.ndarray, tracks: list, wall: float | None = None) -> None:
        try:
            results = await self._recognise(frame, [t.box for t in tracks])
        except Exception as exc:
            logger.warning("Face recognition failed for camera %s (%s)", self.camera_id, exc)
            return
        from app.services.faces import threshold_for

        thr = threshold_for(self.settings)
        for idx, face, match in results:
            t = tracks[idx]
            if t.best_face is None or face.quality > t.best_face.quality:
                t.best_face, t.best_face_score = face, match.score
                t.best_face_at = wall or time.time()
            if match.band == "known":
                t.face_votes[match.person_id] += 1
                if t.face_votes[match.person_id] >= 2 or match.score >= thr + self.FACE_CLEAR_EXTRA:
                    t.identity = {"id": match.person_id, "name": match.name, "note": match.note,
                                  "category": match.category, "score": round(match.score, 3)}
                    t.unknown_face = False
                    self.tracker.timeline.append((t.last_wall or time.time(), f"{t.label} #{t.id} is {match.name}"))
                    if self._on_identity is not None:
                        try:
                            await self._on_identity(t)
                        except Exception:
                            logger.exception("Arrival handling failed")
            elif match.band == "probable":
                t.possibly = match.name
            elif not t.face_votes:
                t.unknown_face = True

    def identities(self) -> list[dict]:
        return [t.identity for t in self.tracker.confirmed() if t.identity]

    async def _zone_event(self, kind: str, t: Track, rule: dict, value: float, frame: np.ndarray) -> None:
        if kind.startswith("package"):
            where = f" at '{rule['name']}'" if rule.get("name") else ""
            left = kind == "package_left"
            text = (f"Package left{where} on '{self.camera_name}' ({t.label})" if left
                    else f"Package taken{where} on '{self.camera_name}'")
            self.tracker.timeline.append((t.last_wall or time.time(), f"{t.label} {'left' if left else 'taken'}{where}"))
            snap = await asyncio.to_thread(lambda: _jpeg(draw_boxes(frame, [(t.label, t.best_conf, t.box)])))
            payload = {"objects": [t.label], "zones": [rule["name"]] if rule.get("name") else [],
                       "package": "left" if left else "taken"}
            await self._emit_safe("package", text, snap, payload)
            return
        name = rule.get("name") or ("line" if kind == "line_cross" else "zone")
        who = t.label.capitalize()
        if kind == "loitering":
            text = f"{who} loitering in '{name}' on '{self.camera_name}' ({format_duration(value)})"
        else:
            text = f"{who} crossed '{name}' on '{self.camera_name}'"
        self.tracker.timeline.append((t.last_wall or time.time(), f"{t.label} #{t.id} {'loitered in' if kind == 'loitering' else 'crossed'} {name}"))
        snap = await asyncio.to_thread(lambda: _jpeg(draw_boxes(frame, [(t.label, t.best_conf, t.box)])))
        payload = {"objects": [t.label], "zones": [name], "track": f"{t.label} #{t.id}"}
        if kind == "loitering":
            payload["dwell_seconds"] = round(value, 1)
        await self._emit_safe(kind, text, snap, payload)

    async def _object_alert(self, frame: np.ndarray, visible: list[Track], now: float) -> None:
        boxes = [(t.label, t.best_conf, t.box) for t in visible]
        snap = await asyncio.to_thread(lambda: _jpeg(draw_boxes(frame, boxes)))
        totals: dict[str, int] = {}
        for t in visible:
            totals[t.label] = totals.get(t.label, 0) + 1
        what = " + ".join(plural(lbl, n) if n > 1 else lbl.capitalize() for lbl, n in sorted(totals.items()))
        await self._send_alert(f"{what} detected on '{self.camera_name}'", snap, self._payload(visible))

    async def _plain_alert(self, ai_failed: bool = False) -> None:
        item = self._get_frame(self.camera_id)
        snap = await asyncio.to_thread(_jpeg, item[0]) if item else None
        # ai_failed: the AI *should* have looked but couldn't - alert rules
        # then treat "which object?" as unknown rather than "nothing".
        payload = {"objects": [], "ai_failed": True} if ai_failed else None
        message = f"Motion detected on '{self.camera_name}'"
        if ai_failed:
            message += " (AI unavailable - not checked for people)"
        await self._send_alert(message, snap, payload)

    async def _send_alert(self, message: str, snapshot: bytes | None, payload: dict | None = None) -> None:
        self.alerted = True
        self._alerted_mono = time.monotonic()
        self.alert_event_id = await self._emit_safe("motion", message, snapshot, payload or {"objects": []})

    @staticmethod
    def _payload(tracks: list) -> dict:
        """Structured facts for alert rules and the UI: which kinds of object,
        which named zones they're in, and who (when faces are on)."""
        zones = sorted({z for t in tracks for z in t.zones})
        p = {"objects": sorted({t.label for t in tracks}), "zones": zones}
        ids = [t.identity for t in tracks if t.identity]
        if ids:
            p["identities"] = [f"person:{i['id']}" for i in ids]
            p["names"] = [i["name"] for i in ids]
            p["categories"] = sorted({i.get("category") or "" for i in ids})
        if any(t.unknown_face for t in tracks):
            p["unknown_faces"] = True
        return p

    def summary_payload(self) -> dict:
        tracks = self.tracker.confirmed()
        p = self._payload(tracks)
        p["counts"] = self.tracker.totals()
        p["duration_seconds"] = round((self.ended_wall or time.time()) - self.started_wall, 1)
        p["started_at"] = self.started_wall
        return p

    async def _emit_safe(self, kind: str, message: str, snapshot: bytes | None, payload: dict | None = None) -> int | None:
        try:
            return await self._emit(self.camera_id, kind, message, snapshot, payload)
        except Exception:
            logger.exception("Could not emit %s event for camera %s", kind, self.camera_id)
            return None

    # ── end of event ─────────────────────────────────────────────────────────

    def _clock(self, wall: float) -> str:
        return datetime.fromtimestamp(wall, self.tz).strftime("%H:%M:%S")

    async def build_summary(self, clip: tuple[float, float] | None = None, clip_parts: int = 1) -> EventSummary | None:
        """None when there's nothing worth telling: objects-only mode and the
        AI worked but saw nothing of interest (a tree, a shadow)."""
        end = self.ended_wall or time.time()
        now_mono = time.monotonic()
        for t in self.tracker.confirmed():
            if t.left_at is None and not t.parked:
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
            names = [t.identity["name"] for t in self.tracker.confirmed() if t.identity]
            unknown = sum(1 for t in self.tracker.confirmed() if t.unknown_face)
            who = ""
            if names or unknown:
                bits = list(dict.fromkeys(names)) + ([f"{unknown} unknown face{'s' if unknown > 1 else ''}"] if unknown else [])
                who = f" ({', '.join(bits)})"
            lines.append("Seen: " + ", ".join(plural(lbl, n) for lbl, n in sorted(seen.items(), key=lambda kv: -kv[1])) + who)
            peaks = [plural(lbl, n) for lbl, n in self.tracker.peak.items() if n > 1]
            if peaks:
                lines.append("Most at once: " + ", ".join(peaks))
            stood = [
                f"{t.label} #{t.id} stood still ~{format_duration(t.max_still)}"
                for t in self.tracker.confirmed()
                if t.group == "person" and t.max_still >= self.STILL_NOTE_MIN
            ]
            if stood:
                lines.append("Waited: " + ", ".join(stood))
            visited = [
                f"{t.label} #{t.id} " + ", ".join(
                    f"{z} ({format_duration(t.dwell.get(z, 0))})" for z in t.zones
                )
                for t in self.tracker.confirmed() if t.zones
            ]
            if visited:
                lines.append("Zones: " + "; ".join(visited))
            events = sorted(self.tracker.timeline)[:12]
            if events:
                lines.append("Timeline:")
                lines += [f"  {self._clock(w)}  {text}" for w, text in events]
            best = max(
                (t for t in self.tracker.confirmed() if t.best_frame is not None),
                key=lambda t: (t.group == "person", t.best_score),
                default=None,
            )
        elif self.ai_on and self.ai_worked:
            lines.append("No people, vehicles or animals were recognised.")
        else:
            lines.append(f"Peak motion: {self.peak_motion:.1f}% of the watched area. (Object detection off.)")

        if clip:
            parts = f", {clip_parts} parts" if clip_parts > 1 else ""
            lines.append(
                f"Clip: {self._clock(clip[0])} - {self._clock(clip[1])} ({format_duration(clip[1] - clip[0])}{parts})"
            )

        snapshot: bytes | None = None
        if best is not None:
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
        from app.services.faces import privacy_safe_jpeg

        jpeg = await privacy_safe_jpeg(s, best.best_frame)
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
