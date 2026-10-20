"""MotionEventSession + ObjectTracker with a scripted fake detector."""

import asyncio
import time
from types import SimpleNamespace

import numpy as np

from app.services.ai.types import DetectedObject
from app.services.motion_event import MotionEventSession, ObjectTracker, format_duration, plural


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


def test_parked_car_stops_holding_event():
    tr = ObjectTracker()
    for i in range(0, 80):  # 40s, not moving
        tr.update([car(0.4)], now=i * 0.5, wall=i * 0.5, frame=None)
    assert tr.holding(now=79 * 0.5, static_max=30.0) is False
    tr2 = ObjectTracker()
    for i in range(0, 6):
        tr2.update([person(0.4)], now=i * 0.5, wall=i * 0.5, frame=None)
    assert tr2.holding(now=5 * 0.5, static_max=30.0) is True


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

    async def detect(self, _settings, _jpeg):
        self.calls += 1
        if self.fail:
            return None
        return self.script(time.monotonic() - self.t0)

    async def emit(self, cam, kind, message, snapshot):
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
    assert [e[1] for e in h.emitted] == ["Motion detected on 'Front Door'"]
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
