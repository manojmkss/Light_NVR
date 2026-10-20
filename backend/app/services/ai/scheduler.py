"""Shares inference fairly between cameras.

Before this, one global lock serialised every detection: with several cameras
in motion, a new event's first frame - the one that decides how fast the alert
goes out - queued behind stationary re-checks from other cameras, and its frame
was already stale by the time it ran.

Now every request has a priority and a deadline:
- P0 event start (first look) > P1 something moving > P2 stationary re-check
  > P3 enrichment > P4 background;
- each camera has a mailbox of one per priority: a newer frame replaces an
  older one still waiting (the old request resolves to None = "superseded"),
  so nothing ever queues up behind itself;
- requests older than their max age are dropped rather than run late;
- within a priority, the camera served longest ago goes next.

A superseded or expired request resolves to SKIPPED ("no answer this tick",
like a skipped tick); a failed one to None (the caller's "AI is broken" path).
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

P_START, P_MOVING, P_STATIONARY, P_ENRICH, P_BACKGROUND = 0, 1, 2, 3, 4
SKIPPED = object()
MAX_AGE = {P_START: 1.5, P_MOVING: 1.5, P_STATIONARY: 5.0, P_ENRICH: 60.0, P_BACKGROUND: 300.0}


@dataclass
class _Request:
    camera_id: int
    priority: int
    run: Callable[[], Awaitable[Any]]
    submitted: float
    future: asyncio.Future


@dataclass
class CameraStats:
    submitted: int = 0
    served: int = 0
    superseded: int = 0
    expired: int = 0
    failed: int = 0
    waits: deque = field(default_factory=lambda: deque(maxlen=200))
    runs: deque = field(default_factory=lambda: deque(maxlen=200))


def _pct(values, q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(q * len(s)))] * 1000, 1)


class InferenceScheduler:
    def __init__(self, slots: int = 1) -> None:
        self._slots = max(1, slots)
        self._pending: dict[tuple[int, int], _Request] = {}  # (priority, camera) -> request
        self._last_served: dict[int, float] = {}
        self._cond: asyncio.Condition | None = None
        self._workers: list[asyncio.Task] = []
        self._busy = 0
        self._latency = 0.0  # EWMA of run time, seconds
        self.stats: dict[int, CameraStats] = {}
        self._active_cameras: dict[int, float] = {}

    # ── public ───────────────────────────────────────────────────────────────

    def set_slots(self, slots: int) -> None:
        slots = max(1, slots)
        if slots != self._slots:
            self._slots = slots
            self._ensure_workers()

    async def submit(self, camera_id: int, priority: int, run: Callable[[], Awaitable[Any]]) -> Any:
        """Run `run()` when a slot is free and this request's turn comes.
        Returns its result, SKIPPED if superseded/expired, None if it failed."""
        self._ensure_workers()
        loop = asyncio.get_running_loop()
        req = _Request(camera_id, priority, run, time.monotonic(), loop.create_future())
        st = self.stats.setdefault(camera_id, CameraStats())
        st.submitted += 1
        self._active_cameras[camera_id] = req.submitted
        async with self._cond:
            old = self._pending.pop((priority, camera_id), None)
            if old is not None and not old.future.done():
                st.superseded += 1
                old.future.set_result(SKIPPED)
            self._pending[(priority, camera_id)] = req
            self._cond.notify()
        try:
            return await req.future
        except asyncio.CancelledError:
            async with self._cond:
                if self._pending.get((priority, camera_id)) is req:
                    del self._pending[(priority, camera_id)]
            raise

    def suggested_interval(self, wanted: float) -> float:
        """The interval a camera should actually use: what the user asked for,
        stretched when there are more cameras in motion than the hardware can
        serve at that rate - slowing down beats queueing."""
        now = time.monotonic()
        active = sum(1 for t in self._active_cameras.values() if now - t < 3.0)
        if not active or not self._latency:
            return wanted
        return max(wanted, self._latency * active / self._slots)

    def snapshot(self) -> dict:
        now = time.monotonic()
        cams = {}
        for cid, st in self.stats.items():
            cams[cid] = {
                "submitted": st.submitted, "served": st.served, "superseded": st.superseded,
                "expired": st.expired, "failed": st.failed,
                "wait_p50_ms": _pct(st.waits, 0.5), "wait_p95_ms": _pct(st.waits, 0.95),
                "infer_p50_ms": _pct(st.runs, 0.5), "infer_p95_ms": _pct(st.runs, 0.95),
            }
        return {
            "slots": self._slots,
            "busy": self._busy,
            "queued": len(self._pending),
            "avg_infer_ms": round(self._latency * 1000, 1),
            "active_cameras": sum(1 for t in self._active_cameras.values() if now - t < 3.0),
            "cameras": cams,
        }

    # ── workers ──────────────────────────────────────────────────────────────

    def _ensure_workers(self) -> None:
        if self._cond is None:
            self._cond = asyncio.Condition()
        self._workers = [w for w in self._workers if not w.done()]
        while len(self._workers) < self._slots:
            self._workers.append(asyncio.create_task(self._worker()))

    def _pick(self) -> _Request | None:
        now = time.monotonic()
        # Drop anything already too old to be worth running.
        for key, req in list(self._pending.items()):
            if now - req.submitted > MAX_AGE.get(req.priority, 5.0):
                del self._pending[key]
                self.stats[req.camera_id].expired += 1
                if not req.future.done():
                    req.future.set_result(SKIPPED)
        if not self._pending:
            return None
        best = min(
            self._pending.values(),
            key=lambda r: (r.priority, self._last_served.get(r.camera_id, 0.0), r.submitted),
        )
        del self._pending[(best.priority, best.camera_id)]
        return best

    async def _worker(self) -> None:
        while True:
            async with self._cond:
                req = self._pick()
                while req is None:
                    await self._cond.wait()
                    req = self._pick()
                if len(self._workers) > self._slots:
                    # Slots were reduced: this worker retires after handing back.
                    self._pending[(req.priority, req.camera_id)] = req
                    self._workers.remove(asyncio.current_task())
                    self._cond.notify()
                    return
            if req.future.done():  # caller went away
                continue
            st = self.stats[req.camera_id]
            started = time.monotonic()
            st.waits.append(started - req.submitted)
            self._last_served[req.camera_id] = started
            self._busy += 1
            try:
                result = await req.run()
                took = time.monotonic() - started
                st.runs.append(took)
                st.served += 1
                self._latency = took if not self._latency else 0.8 * self._latency + 0.2 * took
                if not req.future.done():
                    req.future.set_result(result)
            except Exception as exc:
                st.failed += 1
                logger.warning("AI: inference failed for camera %s (%s)", req.camera_id, exc)
                if not req.future.done():
                    req.future.set_result(None)
            finally:
                self._busy -= 1


scheduler = InferenceScheduler()
