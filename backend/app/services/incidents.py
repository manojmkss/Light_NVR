"""Ties motion events on different cameras into one visit (an "incident").

A new event joins an open incident when the evidence says it's the same visit:
- time and place: same area within SAME_AREA_GAP seconds, or an area linked
  to one the incident has been in, within that link's walking time;
- identity: the same recognised person (faces) joins even further apart;
- a different recognised person never joins on time alone.

Without any areas configured, the whole property is one area, so cameras
active within SAME_AREA_GAP of each other form one visit out of the box.

Notification policy: the first event of an incident alerts as usual; later
events on other cameras alert only when the visit escalates (reaches a more
private area, e.g. street -> yard, or a new unknown person appears). One
visit summary is sent when the incident closes, instead of one per camera.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SAME_AREA_GAP = 90.0
LINK_SLACK = 1.5  # a link's max walking time is stretched by this much
JOIN_SCORE = 0.6
IDLE_CLOSE = 60.0  # close an incident this long after its last member event ended
MAX_INCIDENT = 3600.0
DEFAULT_AREA = "Property"

# More private = later in this list.
AREA_RANK = ["street", "perimeter", "gate", "driveway", "garden", "yard", "porch", "entry", "door", "garage",
             "backyard", "interior", "house", "inside"]
# Reaching one of these (from somewhere less private) alerts again mid-visit:
# someone at the front door is expected, someone in the back yard is news.
PRIVATE_FROM = AREA_RANK.index("backyard")


def area_rank(name: str | None) -> int:
    n = (name or "").lower()
    best = -1
    for i, key in enumerate(AREA_RANK):
        if key in n:
            best = max(best, i)
    return best


@dataclass
class Link:
    a: str
    b: str
    min_s: float
    max_s: float


@dataclass
class Member:
    camera_id: int
    camera_name: str
    area: str
    started: float
    ended: float | None = None
    counts: dict = field(default_factory=dict)
    identities: list = field(default_factory=list)
    waited: list = field(default_factory=list)  # (who, seconds, zone/area)
    clip: tuple | None = None
    summary_event_id: int | None = None


@dataclass
class OpenIncident:
    id: int | None
    started: float
    members: list = field(default_factory=list)
    alerted: bool = False
    max_rank: int = -1
    identities: set = field(default_factory=set)
    last_activity: dict = field(default_factory=dict)  # area -> wall time
    snapshot: bytes | None = None
    mode: str | None = None

    @property
    def active(self) -> bool:
        return any(m.ended is None for m in self.members)

    @property
    def last_end(self) -> float:
        ends = [m.ended for m in self.members if m.ended is not None]
        return max(ends) if ends else self.started

    @property
    def camera_ids(self) -> list[int]:
        out: list[int] = []
        for m in self.members:
            if m.camera_id not in out:
                out.append(m.camera_id)
        return out

    @property
    def areas_path(self) -> list[str]:
        out: list[str] = []
        for m in sorted(self.members, key=lambda m: m.started):
            if not out or out[-1] != m.area:
                out.append(m.area)
        return out


def _linked(links: list[Link], a: str, b: str) -> Link | None:
    for ln in links:
        if {ln.a.lower(), ln.b.lower()} == {a.lower(), b.lower()}:
            return ln
    return None


def score(inc: OpenIncident, area: str, now: float, links: list[Link], identity: str | None = None) -> float:
    s = 0.0
    if area in inc.last_activity and now - inc.last_activity[area] <= SAME_AREA_GAP:
        s = 1.0
    elif inc.active and area in inc.last_activity:
        s = 1.0
    else:
        for other, t in inc.last_activity.items():
            ln = _linked(links, area, other)
            if ln is not None and ln.min_s - 5 <= now - t <= ln.max_s * LINK_SLACK:
                s = max(s, 0.8)
            elif ln is not None and inc.active:
                s = max(s, 0.8)
    if identity:
        if identity in inc.identities:
            s += 1.0
        elif inc.identities:
            s -= 1.0  # a different known person: a separate visit
    return s


class IncidentManager:
    def __init__(self, persist=None, emit=None, clock=time.time) -> None:
        # persist(inc) -> id ; emit(kind, message, snapshot, payload, camera_id) ; injected for tests
        self._persist = persist
        self._emit = emit
        self._clock = clock
        self.open: list[OpenIncident] = []
        self.links: list[Link] = []
        self.closed: list[OpenIncident] = []

    def set_links(self, links: list[Link]) -> None:
        self.links = links

    # ── membership ───────────────────────────────────────────────────────────

    async def join(self, camera_id: int, camera_name: str, area: str | None, mode: str | None = None) -> tuple[OpenIncident, Member, bool]:
        """A motion event started on a camera. Returns (incident, member,
        should_alert): whether this event's own first alert should go out."""
        area = (area or "").strip() or DEFAULT_AREA
        now = self._clock()
        best, best_s = None, 0.0
        for inc in self.open:
            if now - inc.started > MAX_INCIDENT:
                continue
            s = score(inc, area, now, self.links)
            if s > best_s:
                best, best_s = inc, s
        member = Member(camera_id, camera_name, area, now)
        if best is None or best_s < JOIN_SCORE:
            inc = OpenIncident(id=None, started=now, mode=mode)
            self.open.append(inc)
            should_alert = True
        else:
            inc = best
            rank = area_rank(area)
            # Escalation: the visit reached a private part of the property.
            should_alert = not inc.alerted or (rank >= PRIVATE_FROM and inc.max_rank < PRIVATE_FROM)
        inc.members.append(member)
        inc.last_activity[area] = now
        inc.max_rank = max(inc.max_rank, area_rank(area))
        if self._persist is not None:
            try:
                inc.id = await self._persist(inc)
            except Exception:
                logger.exception("Could not save incident")
        return inc, member, should_alert

    def mark_alerted(self, inc: OpenIncident) -> None:
        inc.alerted = True

    def add_identity(self, inc: OpenIncident, name: str) -> bool:
        """A recognised person on a member. True if new to this incident."""
        if name in inc.identities:
            return False
        inc.identities.add(name)
        return True

    def note_activity(self, inc: OpenIncident, area: str | None) -> None:
        inc.last_activity[(area or "").strip() or DEFAULT_AREA] = self._clock()

    async def leave(self, inc: OpenIncident, member: Member, counts: dict, waited: list, clip,
                    snapshot: bytes | None, identities: list) -> bool:
        """A member event ended. Returns True when the caller should send its
        own per-camera summary (single-camera visit so far)."""
        member.ended = self._clock()
        member.counts = counts or {}
        member.waited = waited or []
        member.clip = clip
        member.identities = identities or []
        inc.last_activity[member.area] = member.ended
        if snapshot and (inc.snapshot is None or counts):
            inc.snapshot = snapshot
        if self._persist is not None:
            try:
                await self._persist(inc)
            except Exception:
                logger.exception("Could not save incident")
        return len(inc.camera_ids) <= 1

    # ── closing ──────────────────────────────────────────────────────────────

    async def sweep(self) -> list[OpenIncident]:
        """Close incidents whose members have all ended and stayed quiet.
        Multi-camera visits get one visit summary."""
        now = self._clock()
        closed = []
        for inc in list(self.open):
            overdue = now - inc.started > MAX_INCIDENT + IDLE_CLOSE
            if inc.active and not overdue:
                continue
            if overdue:
                # A member that never reported its end (its camera worker was
                # restarted mid-event): close it here rather than keep the
                # visit - and its memory - open forever.
                for m in inc.members:
                    if m.ended is None:
                        m.ended = now
            elif now - inc.last_end < IDLE_CLOSE and now - inc.started < MAX_INCIDENT:
                continue
            self.open.remove(inc)
            closed.append(inc)
            self.closed = (self.closed + [inc])[-50:]
            if self._persist is not None:
                try:
                    await self._persist(inc, closed=True)
                except Exception:
                    logger.exception("Could not close incident")
            if len(inc.camera_ids) > 1 and self._emit is not None:
                message, payload = self.describe(inc)
                try:
                    await self._emit("incident_summary", message, inc.snapshot, payload, None, inc)
                except Exception:
                    logger.exception("Could not send visit summary")
        return closed

    def describe(self, inc: OpenIncident, tz=timezone.utc) -> tuple[str, dict]:
        start = datetime.fromtimestamp(inc.started, tz)
        end = datetime.fromtimestamp(inc.last_end, tz)
        dur = max(0, round(inc.last_end - inc.started))
        m, s = divmod(dur, 60)
        dur_txt = f"{m}m {s}s" if m else f"{s}s"
        counts: dict[str, int] = {}
        for mem in inc.members:
            for label, n in mem.counts.items():
                counts[label] = max(counts.get(label, 0), n)  # the same person on several cameras
        seen = ", ".join(f"{n} {label if n == 1 else (label + 's' if label != 'person' else 'people')}"
                         for label, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        who = sorted(inc.identities)
        lines = [f"Visit {start.strftime('%d %b %H:%M:%S')} - {end.strftime('%H:%M:%S')} {start.strftime('%Z')} ({dur_txt})".rstrip()]
        lines.append("Route: " + " -> ".join(inc.areas_path))
        if seen:
            lines.append("Seen: " + seen + (f" ({', '.join(who)})" if who else ""))
        waits = [f"{w[0]} waited ~{round(w[1] / 60)}m at {w[2]}" for mem in inc.members for w in mem.waited if w[1] >= 60]
        if waits:
            lines.append("; ".join(waits))
        lines.append("Cameras: " + ", ".join(dict.fromkeys(m.camera_name for m in inc.members)))
        clips = [m.clip for m in inc.members if m.clip]
        if clips:
            lines.append(f"{len(clips)} clip(s)")
        payload = {
            "incident_id": inc.id, "cameras": inc.camera_ids, "areas": inc.areas_path, "counts": counts,
            "objects": sorted(counts), "identities": who, "duration_seconds": dur,
        }
        return "\n".join(lines), payload


incident_manager = IncidentManager()
