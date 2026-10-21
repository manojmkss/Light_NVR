"""MotionEventSession + ObjectTracker with a scripted fake detector."""

import asyncio
import time
from types import SimpleNamespace

import numpy as np

from app.services.ai.types import DetectedObject
from app.services.motion_detector import MotionDetector, point_in_watch_area
from app.services.motion_event import (
    MotionEventSession,
    ObjectTracker,
    ParkedObject,
    format_duration,
    plural,
)


def person(x, y=0.3, conf=0.8):
    return DetectedObject("person", conf, x, y, 0.1, 0.3)


def car(x, y=0.6, conf=0.9):
    return DetectedObject("car", conf, x, y, 0.25, 0.2)


def settings(**kw):
    base = dict(
        enabled=True, detection_enabled=True, alert_on_objects_only=True, monitor_interval_ms=100,
        event_summary_enabled=True, event_summary_vlm=True, vlm_enabled=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── tracker ──────────────────────────────────────────────────────────────────


def test_walking_person_is_one_track():
    tr = ObjectTracker()
    frame = np.zeros((10, 10, 3), np.uint8)
    for i in range(10):  # moving 0.03 of the frame per tick
        tr.update([person(0.1 + i * 0.03)], now=i * 0.5, wall=1000 + i * 0.5, frame=frame)
    assert tr.totals() == {"person": 1}
    assert tr.peak == {"person": 1}


def test_two_people_counted_and_peak():
    tr = ObjectTracker()
    for i in range(4):
        tr.update([person(0.1), person(0.7)], now=i * 0.5, wall=i * 0.5, frame=None)
    assert tr.totals() == {"person": 2}
    assert tr.peak == {"person": 2}


def test_single_frame_false_positive_not_confirmed():
    tr = ObjectTracker()
    tr.update([person(0.5, conf=0.55)], now=0, wall=0, frame=None)
    for i in range(1, 5):
        tr.update([], now=i * 0.5, wall=i * 0.5, frame=None)
    assert tr.totals() == {}


def test_person_leaving_and_new_person_arriving_counts_two():
    tr = ObjectTracker()
    for i in range(3):
        tr.update([person(0.1)], now=i * 0.5, wall=i * 0.5, frame=None)
    for i in range(3, 15):  # gone > LOST_AFTER
        tr.update([], now=i * 0.5, wall=i * 0.5, frame=None)
    for i in range(15, 18):
        tr.update([person(0.8)], now=i * 0.5, wall=i * 0.5, frame=None)
    assert tr.totals() == {"person": 2}
    texts = [t for _w, t in tr.timeline]
    assert texts == ["person #1 appeared", "person #1 left", "person #2 appeared"]


def jitter(i, amp=0.003):
    return amp if i % 2 else -amp


def test_standing_person_holds_for_five_minutes_then_cap():
    tr = ObjectTracker()
    tr.update([person(0.4)], now=0, wall=0, frame=None)
    for i in range(1, 600):  # 300s at 2 fps, small jitter only
        tr.update([person(0.4 + jitter(i))], now=i * 0.5, wall=i * 0.5, frame=None)
        assert tr.holding(now=i * 0.5), f"lost hold at {i * 0.5}s"
    assert tr.confirmed()[0].max_still >= 290
    # Past the 15-minute stationary cap the person no longer keeps it open.
    t = 600 * 0.5
    while t < 905:
        t += 0.5
        tr.update([person(0.4 + jitter(int(t * 2)))], now=t, wall=t, frame=None)
    assert tr.holding(now=t) is False


def test_vehicle_parks_and_never_holds():
    tr = ObjectTracker()
    for i in range(0, 60):  # 30s, not moving
        tr.update([car(0.4)], now=i * 0.5, wall=i * 0.5, frame=None)
    t = tr.tracks[0]
    assert t.parked
    assert tr.holding(now=59 * 0.5) is False
    assert any("parked" in txt for _w, txt in tr.timeline)


def test_parked_car_from_previous_event_is_not_counted_until_it_leaves():
    seed = [ParkedObject("vehicle", "car", (0.4, 0.6, 0.25, 0.2), since=0.0)]
    tr = ObjectTracker(parked=seed)
    newly = []
    for i in range(6):
        newly += tr.update([car(0.4), person(0.1 + i * 0.03)], now=i * 0.5, wall=i * 0.5, frame=None)
    assert tr.totals() == {"person": 1}  # the parked car isn't "seen" again
    assert [t.group for t in newly] == ["person"]
    # Now it drives off.
    for i in range(6, 12):
        newly = tr.update([car(0.4 + (i - 5) * 0.08)], now=i * 0.5, wall=i * 0.5, frame=None)
        if newly:
            break
    assert newly and newly[0].group == "vehicle"
    assert any("started moving" in txt for _w, txt in tr.timeline)


def test_parked_car_that_disappears_has_left():
    seed = [ParkedObject("vehicle", "car", (0.4, 0.6, 0.25, 0.2), since=0.0)]
    tr = ObjectTracker(parked=seed)
    for i in range(4):
        tr.update([], now=i * 0.5, wall=i * 0.5, frame=None)
    assert tr.parked_objects() == []
    assert any("parked car left" in txt for _w, txt in tr.timeline)


def test_person_outside_watched_area_never_counts_or_holds():
    # Exclude the bottom-right quarter (say, the street). Feet are at y + h.
    zones = [{"kind": "exclude", "points": [[0.5, 0.5], [1, 0.5], [1, 1], [0.5, 1]]}]
    tr = ObjectTracker(in_zone=lambda x, y: point_in_watch_area(zones, x, y))
    for i in range(6):
        tr.update([person(0.7 + i * 0.01, y=0.4)], now=i * 0.5, wall=i * 0.5, frame=None)
    assert tr.totals() == {}
    assert tr.holding(now=2.5) is False
    # ...but walking into the garden makes them count.
    for i in range(6, 10):
        tr.update([person(0.7 - (i - 5) * 0.08, y=0.2)], now=i * 0.5, wall=i * 0.5, frame=None)
    assert tr.totals() == {"person": 1}


def test_car_truck_label_flips_are_one_vehicle():
    tr = ObjectTracker()
    for i in range(8):
        label = "truck" if i % 3 == 0 else "car"
        tr.update([DetectedObject(label, 0.8, 0.2 + i * 0.03, 0.6, 0.25, 0.2)], now=i * 0.5, wall=i * 0.5, frame=None)
    assert tr.totals() == {"car": 1}


def test_movement_is_relative_to_object_size():
    tr = ObjectTracker()
    # Large box close to the camera: a 0.02 shift is fidgeting, not walking.
    for i in range(30):
        tr.update([DetectedObject("person", 0.9, 0.3 + jitter(i, 0.01), 0.1, 0.3, 0.8)], now=i * 0.5, wall=i * 0.5, frame=None)
    assert tr.confirmed()[0].max_still >= 14


def test_formatting_helpers():
    assert plural("person", 2) == "2 people"
    assert plural("car", 1) == "1 car"
    assert format_duration(95) == "1m 35s"


# ── session ──────────────────────────────────────────────────────────────────


class Harness:
    def __init__(self, script, ai_settings, fail=False):
        self.script = script  # callable(elapsed) -> list[DetectedObject]
        self.settings = ai_settings
        self.fail = fail
        self.active = True
        self.holds = []
        self.emitted = []
        self.calls = 0
        self.t0 = time.monotonic()
        self.frame = np.full((360, 640, 3), 120, np.uint8)

    def get_frame(self, _cam):
        return self.frame, time.monotonic()

    async def detect(self, _settings, _frame, _camera_id=0, _priority=1):
        self.calls += 1
        if self.fail:
            return None
        return self.script(time.monotonic() - self.t0)

    async def emit(self, cam, kind, message, snapshot, payload=None):
        self.emitted.append((kind, message, snapshot))
        return len(self.emitted)

    def session(self):
        return MotionEventSession(
            7, "Front Door", self.settings,
            motion_active=lambda: self.active,
            hold_motion=self.holds.append,
            motion_level=lambda: 2.5,
            get_frame=self.get_frame,
            detect=self.detect,
            emit=self.emit,
        )


def run_session(h: Harness, seconds: float):
    async def go():
        s = h.session()
        s.start()
        await asyncio.sleep(seconds)
        s.mark_motion_end()
        await s.stop_monitoring()
        return s, await s.build_summary(clip=(s.started_wall - 5, time.time() + 10))

    return asyncio.run(go())


def test_alert_on_first_confirmed_person_with_boxes_then_summary():
    h = Harness(lambda t: [person(0.2 + t * 0.05)] if t > 0.3 else [], settings())
    s, summary = run_session(h, 1.5)
    alerts = [e for e in h.emitted if e[0] == "motion"]
    assert len(alerts) == 1
    assert alerts[0][1] == "Person detected on 'Front Door'"
    assert alerts[0][2] and alerts[0][2][:2] == b"\xff\xd8"  # a JPEG
    assert h.holds, "a moving person keeps the motion event open"
    assert summary is not None
    assert "Seen: 1 person" in summary.message
    assert "person #1 appeared" in summary.message
    assert "Clip:" in summary.message
    assert summary.snapshot is not None


def test_ticks_run_every_interval_while_active():
    h = Harness(lambda t: [], settings(monitor_interval_ms=100))
    run_session(h, 1.05)
    assert 8 <= h.calls <= 12


def test_no_ai_calls_while_motion_inactive():
    h = Harness(lambda t: [], settings(monitor_interval_ms=100))
    h.active = False
    run_session(h, 0.6)
    assert h.calls == 0


def test_objects_only_nothing_seen_means_silence():
    h = Harness(lambda t: [], settings(alert_on_objects_only=True))
    _s, summary = run_session(h, 1.0)
    assert h.emitted == []
    assert summary is None


def test_not_objects_only_falls_back_to_motion_alert_after_grace(monkeypatch):
    monkeypatch.setattr(MotionEventSession, "AI_GRACE_SECONDS", 0.3)
    h = Harness(lambda t: [], settings(alert_on_objects_only=False))
    _s, summary = run_session(h, 0.8)
    assert [e[1] for e in h.emitted] == ["Motion detected on 'Front Door'"]
    assert "No people, vehicles or animals" in summary.message


def test_ai_failure_still_alerts_plain_motion():
    h = Harness(lambda t: [], settings(), fail=True)
    _s, summary = run_session(h, 0.5)
    # Flagged so the user (and the fail-safe rule matching) know it wasn't checked.
    assert [e[1] for e in h.emitted] == [
        "Motion detected on 'Front Door' (AI unavailable - not checked for people)"
    ]
    assert summary is not None and "Peak motion" in summary.message


def test_ai_off_alerts_immediately_and_summarises_motion_only():
    h = Harness(lambda t: [], settings(enabled=False))
    _s, summary = run_session(h, 0.5)
    assert h.calls == 0
    assert [e[1] for e in h.emitted] == ["Motion detected on 'Front Door'"]
    assert "Peak motion: 2.5%" in summary.message


def test_vlm_description_added_to_summary():
    calls = []

    async def describe(_s, jpeg, labels, cam, prompt):
        calls.append((labels, prompt))
        return "A person in a red jacket walks to the door."

    h = Harness(lambda t: [person(0.3 + t * 0.05)], settings(vlm_enabled=True))

    async def go():
        s = h.session()
        s._describe = describe
        s.start()
        await asyncio.sleep(1.0)
        await s.stop_monitoring()
        return await s.build_summary()

    summary = asyncio.run(go())
    assert calls and calls[0][0] == ["person"]
    assert summary.message.splitlines()[1] == "A person in a red jacket walks to the door."


def test_follow_up_alert_when_another_person_arrives(monkeypatch):
    monkeypatch.setattr(MotionEventSession, "FOLLOW_UP_AFTER", 0.3)
    h = Harness(
        lambda t: [person(0.1 + t * 0.05)] + ([person(0.7 - t * 0.05)] if t > 0.9 else []),
        settings(monitor_interval_ms=100),
    )
    run_session(h, 1.6)
    kinds = [e[0] for e in h.emitted]
    assert kinds == ["motion", "motion_update"]
    assert "2 people now" in h.emitted[1][1]


def test_person_still_visible_after_pixels_go_quiet_revives_the_event(monkeypatch):
    monkeypatch.setattr(MotionEventSession, "GRACE_INTERVAL", 0.2)
    revived = []

    async def on_revive(hold):
        revived.append(hold)

    h = Harness(lambda t: [person(0.4)], settings(monitor_interval_ms=100))

    async def go():
        s = h.session()
        s._on_revive = on_revive
        s.start()
        await asyncio.sleep(0.6)
        h.active = False  # detector closed the event: they're standing still
        s.mark_motion_end()
        await asyncio.sleep(1.0)
        await s.stop_monitoring()
        return s

    s = asyncio.run(go())
    assert len(revived) == 1  # once per grace, not on every tick
    assert revived[0] >= MotionEventSession.MIN_HOLD_SECONDS
    assert s.revives == 1


def test_no_revive_when_nobody_is_left(monkeypatch):
    monkeypatch.setattr(MotionEventSession, "GRACE_INTERVAL", 0.2)
    revived = []

    async def on_revive(hold):
        revived.append(hold)

    h = Harness(lambda t: [person(0.2 + t * 0.1)] if t < 0.5 else [], settings(monitor_interval_ms=100))

    async def go():
        s = h.session()
        s._on_revive = on_revive
        s.start()
        await asyncio.sleep(0.7)
        h.active = False
        s.mark_motion_end()
        await asyncio.sleep(0.8)
        await s.stop_monitoring()

    asyncio.run(go())
    assert revived == []


def test_checks_slow_down_while_everyone_stands_still(monkeypatch):
    monkeypatch.setattr(MotionEventSession, "STILL_AFTER", 0.3)
    monkeypatch.setattr(MotionEventSession, "STILL_INTERVAL", 0.5)
    h = Harness(lambda t: [person(0.4)], settings(monitor_interval_ms=100))
    run_session(h, 2.0)
    assert h.calls <= 10  # ~20 at the full 100ms rate


def test_hold_survives_slow_inference():
    h = Harness(lambda t: [person(0.4)], settings(monitor_interval_ms=100))

    async def slow_detect(_s, _f, _c=0, _p=1):
        await asyncio.sleep(0.6)  # slower than the 100ms interval
        return [person(0.4)]

    h.detect = slow_detect
    run_session(h, 2.0)
    assert h.holds and min(h.holds) >= 1.2  # >= 2x the observed latency


def test_summary_reports_how_long_someone_waited(monkeypatch):
    monkeypatch.setattr(MotionEventSession, "STILL_NOTE_MIN", 0.5)
    h = Harness(lambda t: [person(0.4)], settings(monitor_interval_ms=100))
    _s, summary = run_session(h, 1.5)
    assert "Waited: person #1 stood still ~1s" in summary.message


def test_detector_revive_reopens_without_a_start_and_ends_later():
    rng = np.random.default_rng(1)
    frame = (rng.integers(0, 255, (360, 640, 3))).astype(np.uint8)
    d = MotionDetector(50)
    t = 0.0
    while t < 4.0:  # warm up on a static scene
        d.process(frame, t)
        t += 0.1
    d.revive(now=t, until=t + 5.0)
    assert d.active
    samples = []
    while t < 15.0:
        s = d.process(frame, t)
        if s:
            samples.append((t, s))
        t += 0.1
    assert not any(s.started for _t, s in samples)
    stops = [st for st, s in samples if s.stopped]
    assert len(stops) == 1 and stops[0] >= 8.9  # held 5s (quiet the whole time)


# ── named zones and lines ────────────────────────────────────────────────────

DOORSTEP = {"kind": "zone", "name": "Doorstep", "points": [[0.3, 0.5], [0.7, 0.5], [0.7, 0.9], [0.3, 0.9]],
            "loiter_seconds": 10, "objects": ["person"]}
GATE = {"kind": "line", "name": "Gate", "points": [[0.5, 0.0], [0.5, 1.0]], "direction": "both", "objects": ["person"]}


def test_loitering_fires_once_after_dwell():
    tr = ObjectTracker(rules=[DOORSTEP])
    events = []
    for i in range(40):  # feet at y=0.3+0.3=0.6 inside the doorstep, 20s
        tr.update([person(0.45 + jitter(i))], now=i * 0.5, wall=i * 0.5, frame=None)
        events += tr.drain_events()
    kinds = [e[0] for e in events]
    assert kinds == ["loitering"]
    assert events[0][3] >= 10
    assert tr.confirmed()[0].zones == ["Doorstep"]


def test_line_crossing_both_ways_and_direction_filter():
    tr = ObjectTracker(rules=[GATE])
    events = []
    for i in range(12):  # walk left -> right across x=0.5
        tr.update([person(0.2 + i * 0.05)], now=i * 0.5, wall=i * 0.5, frame=None)
        events += tr.drain_events()
    assert [e[0] for e in events] == ["line_cross"]
    one_way = dict(GATE, direction="backward")  # only right -> left
    tr2 = ObjectTracker(rules=[one_way])
    events2 = []
    for i in range(12):
        tr2.update([person(0.2 + i * 0.05)], now=i * 0.5, wall=i * 0.5, frame=None)
        events2 += tr2.drain_events()
    assert events2 == []


def test_zone_rules_ignore_other_objects():
    tr = ObjectTracker(rules=[DOORSTEP])
    events = []
    for i in range(40):
        tr.update([DetectedObject("dog", 0.9, 0.45, 0.5, 0.1, 0.1)], now=i * 0.5, wall=i * 0.5, frame=None)
        events += tr.drain_events()
    assert events == []


def test_session_emits_loitering_event_and_summary_lists_zone(monkeypatch):
    zone = dict(DOORSTEP, loiter_seconds=1)
    h = Harness(lambda t: [person(0.45)], settings(monitor_interval_ms=100))

    async def go():
        s = MotionEventSession(
            7, "Front Door", h.settings, motion_active=lambda: h.active, hold_motion=h.holds.append,
            motion_level=lambda: 1.0, get_frame=h.get_frame, detect=h.detect, emit=h.emit, rules=[zone],
        )
        s.start()
        await asyncio.sleep(1.8)
        s.mark_motion_end()
        await s.stop_monitoring()
        return await s.build_summary()

    summary = asyncio.run(go())
    kinds = [e[0] for e in h.emitted]
    assert kinds[0] == "motion" and "loitering" in kinds
    assert "Zones: person #1 Doorstep" in summary.message


def test_person_walking_off_after_standing_is_still_one_person():
    tr = ObjectTracker()
    t = 0.0
    for i in range(40):  # stand 20s at 0.5s ticks
        tr.update([person(0.4)], now=t, wall=t, frame=None)
        t += 0.5
    # Slowed-down cadence: next look 2s later, they've walked 0.45 of the frame.
    for k in range(1, 4):
        t += 2.0
        tr.update([person(0.4 + k * 0.15)], now=t, wall=t, frame=None)
    assert tr.totals() == {"person": 1}


def test_line_not_fired_beyond_its_ends():
    short_gate = {"kind": "line", "name": "Gate", "points": [[0.5, 0.0], [0.5, 0.3]], "objects": ["person"]}
    tr = ObjectTracker(rules=[short_gate])
    events = []
    for i in range(12):  # feet at y=0.6+0.3: far below the short gate line
        tr.update([person(0.2 + i * 0.05, y=0.6)], now=i * 0.5, wall=i * 0.5, frame=None)
        events += tr.drain_events()
    assert events == []



def bag(x, y=0.7):
    return DetectedObject("backpack", 0.8, x, y, 0.06, 0.08)


def test_package_left_after_person_walks_away_then_taken():
    tr = ObjectTracker()
    events = []
    t = 0.0
    for i in range(30):  # courier stands with the parcel at their feet for 15s
        tr.update([person(0.4), bag(0.42)], now=t, wall=t, frame=None)
        events += tr.drain_events()
        t += 0.5
    assert events == []  # someone is right next to it: not "left" yet
    for i in range(10):  # courier walks off
        tr.update([person(0.4 + (i + 1) * 0.1), bag(0.42)], now=t, wall=t, frame=None)
        events += tr.drain_events()
        t += 0.5
    assert [e[0] for e in events] == ["package_left"]
    assert tr.totals() == {"person": 1}  # the bag is an event, not a "seen" object
    parked = tr.parked_objects()
    assert parked and parked[0].group == "package"
    # Next event: the bag disappears -> taken.
    tr2 = ObjectTracker(parked=parked)
    ev2 = []
    for i in range(4):
        tr2.update([], now=i * 0.5, wall=i * 0.5, frame=None)
        ev2 += tr2.drain_events()
    assert [e[0] for e in ev2] == ["package_removed"]


def test_backpack_on_a_walking_person_is_not_a_package():
    tr = ObjectTracker()
    events = []
    for i in range(30):
        x = 0.1 + i * 0.02
        tr.update([person(x), bag(x + 0.02, y=0.35)], now=i * 0.5, wall=i * 0.5, frame=None)
        events += tr.drain_events()
    assert events == []
