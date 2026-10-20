import asyncio
import time

import numpy as np

from app.services.ai import scheduler as sched_mod
from app.services.ai.backends import decode_yolo_output
from app.services.ai.scheduler import (
    P_BACKGROUND,
    P_MOVING,
    P_START,
    P_STATIONARY,
    SKIPPED,
    InferenceScheduler,
)
from app.services.ai.types import DetectedObject
from app.services.motion_event import ObjectTracker


def job(order, name, delay=0.05, result=None):
    async def run():
        order.append(name)
        await asyncio.sleep(delay)
        return result if result is not None else name
    return run


def test_higher_priority_runs_first():
    async def go():
        s = InferenceScheduler(slots=1)
        order = []
        # Occupy the only slot, then queue P2 before P0.
        blocker = asyncio.create_task(s.submit(9, P_MOVING, job(order, "blocker", 0.1)))
        await asyncio.sleep(0.02)
        low = asyncio.create_task(s.submit(1, P_STATIONARY, job(order, "stationary")))
        high = asyncio.create_task(s.submit(2, P_START, job(order, "start")))
        await asyncio.gather(blocker, low, high)
        return order

    assert asyncio.run(go()) == ["blocker", "start", "stationary"]


def test_newer_frame_supersedes_waiting_one():
    async def go():
        s = InferenceScheduler(slots=1)
        order = []
        blocker = asyncio.create_task(s.submit(9, P_MOVING, job(order, "blocker", 0.1)))
        await asyncio.sleep(0.02)
        old = asyncio.create_task(s.submit(1, P_MOVING, job(order, "old")))
        await asyncio.sleep(0.01)
        new = asyncio.create_task(s.submit(1, P_MOVING, job(order, "new")))
        return await asyncio.gather(blocker, old, new), order, s.stats[1].superseded

    (results, order, superseded) = asyncio.run(go())
    assert results[1] is SKIPPED and results[2] == "new"
    assert "old" not in order and superseded == 1


def test_stale_requests_expire(monkeypatch):
    monkeypatch.setitem(sched_mod.MAX_AGE, P_STATIONARY, 0.05)

    async def go():
        s = InferenceScheduler(slots=1)
        order = []
        blocker = asyncio.create_task(s.submit(9, P_MOVING, job(order, "blocker", 0.2)))
        await asyncio.sleep(0.02)
        stale = await asyncio.gather(blocker, s.submit(1, P_STATIONARY, job(order, "late")))
        return stale, order, s.stats[1].expired

    (results, order, expired) = asyncio.run(go())
    assert results[1] is SKIPPED and "late" not in order and expired == 1


def test_round_robin_between_cameras():
    async def go():
        s = InferenceScheduler(slots=1)
        served = []

        async def camera(cid):
            for _ in range(5):
                await s.submit(cid, P_MOVING, job(served, cid, 0.01))

        await asyncio.gather(*(camera(c) for c in range(4)))
        return served

    served = asyncio.run(go())
    # Every camera got served before any camera got its 3rd turn.
    first_eight = served[:8]
    assert set(first_eight) == {0, 1, 2, 3}


def test_failure_returns_none_and_is_counted():
    async def boom():
        raise RuntimeError("model missing")

    async def go():
        s = InferenceScheduler(slots=1)
        return await s.submit(1, P_MOVING, boom), s.stats[1].failed

    result, failed = asyncio.run(go())
    assert result is None and failed == 1


def test_interval_stretches_under_load():
    async def go():
        s = InferenceScheduler(slots=1)
        await asyncio.gather(*(s.submit(c, P_MOVING, job([], c, 0.1)) for c in range(4)))
        return s.suggested_interval(0.5), s.snapshot()

    interval, snap = asyncio.run(go())
    assert interval > 0.3  # 4 busy cameras x ~0.1s each on one slot
    assert snap["slots"] == 1 and set(snap["cameras"]) == {0, 1, 2, 3}


def test_background_never_beats_live_work():
    async def go():
        s = InferenceScheduler(slots=1)
        order = []
        blocker = asyncio.create_task(s.submit(9, P_MOVING, job(order, "blocker", 0.05)))
        await asyncio.sleep(0.01)
        bg = asyncio.create_task(s.submit(1, P_BACKGROUND, job(order, "bg")))
        live = asyncio.create_task(s.submit(2, P_STATIONARY, job(order, "live")))
        await asyncio.gather(blocker, bg, live)
        return order

    assert asyncio.run(go()) == ["blocker", "live", "bg"]


# ── decoder ──────────────────────────────────────────────────────────────────


def test_yolo26_end_to_end_output_decodes_without_nms():
    # (1, 300, 6): x1, y1, x2, y2, score, class in letterboxed 640 pixels.
    out = np.zeros((1, 300, 6), dtype=np.float32)
    out[0, 0] = [100, 200, 200, 400, 0.9, 0]   # person
    out[0, 1] = [300, 300, 500, 400, 0.6, 2]   # car
    out[0, 2] = [0, 0, 10, 10, 0.1, 0]         # below threshold
    objs = decode_yolo_output(out, 1.0, 0, 0, 640, 640, 0.5)
    assert [o.label for o in objs] == ["person", "car"]
    assert abs(objs[0].x - 100 / 640) < 1e-6 and abs(objs[0].h - 200 / 640) < 1e-6


def test_yolov8_layout_still_decodes():
    out = np.zeros((1, 84, 8400), dtype=np.float32)
    out[0, :4, 0] = [320, 320, 100, 200]
    out[0, 4, 0] = 0.8  # class 0 = person
    objs = decode_yolo_output(out, 1.0, 0, 0, 640, 640, 0.5)
    assert len(objs) == 1 and objs[0].label == "person"


# ── two-stage tracking ───────────────────────────────────────────────────────


def test_weak_detection_keeps_a_known_person_but_never_starts_one():
    tr = ObjectTracker()
    for i in range(3):
        tr.update([DetectedObject("person", 0.8, 0.3, 0.3, 0.1, 0.3)], now=i * 0.5, wall=i * 0.5, frame=None, high=0.5)
    # Half hidden: confidence drops to 0.3 for 10 seconds (would be lost at 6s).
    for i in range(3, 23):
        tr.update([DetectedObject("person", 0.3, 0.3, 0.3, 0.1, 0.3)], now=i * 0.5, wall=i * 0.5, frame=None, high=0.5)
    assert tr.totals() == {"person": 1}
    assert tr.holding(now=22 * 0.5)
    # A weak box with no track behind it is ignored.
    tr2 = ObjectTracker()
    for i in range(5):
        tr2.update([DetectedObject("person", 0.3, 0.6, 0.3, 0.1, 0.3)], now=i * 0.5, wall=i * 0.5, frame=None, high=0.5)
    assert tr2.totals() == {}


def test_fast_walker_is_followed_using_its_velocity():
    tr = ObjectTracker()
    # 0.12 of the frame per tick: boxes 0.1 wide never overlap between ticks.
    for i in range(6):
        tr.update([DetectedObject("person", 0.8, 0.05 + i * 0.12, 0.3, 0.1, 0.3)], now=i * 0.5, wall=i * 0.5, frame=None, high=0.5)
    assert tr.totals() == {"person": 1}
