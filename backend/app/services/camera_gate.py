"""One shared view per camera of "is it refusing us?".

Several parts of the NVR connect to each camera on their own: the recorder,
the motion-clip buffer, the motion/AI decoder, the face-capture main stream,
live view. When a camera stops answering - it rebooted, its Wi-Fi dropped, or
it says "403 Forbidden" because it has too many connections or has locked us
out after too many failed attempts - each of them used to retry on its own,
every few seconds. Together they kept a weak camera refusing far longer than
needed (seen: a Wi-Fi camera's 04:00 reboot turning into two hours lost).

Every connector now reports here, and asks before connecting:

- Optional connections (face capture, live view) rest after any failure.
- Essential ones (recording, motion detection) rest only while the camera
  is actually refusing (403) - otherwise they keep their own quick retries,
  so a failing optional stream can never delay a recording.
- After a rest, one connection tries first; the rest follow once it works,
  instead of a just-rebooted camera getting five connections at once.

Thread-safe: the decoders run in OS threads, the rest on the asyncio loop.
"""

import asyncio
import threading
import time
from dataclasses import dataclass

OPTIONAL_REST = (10.0, 300.0)  # first rest, longest rest (seconds), doubling
REFUSED_REST = (15.0, 120.0)  # recording waits at most 2 min after a camera stops refusing
PROBE_SECONDS = 30.0  # how long the one trying connection has before others may try

REFUSED_MARKS = ("403", "forbidden", "401 unauthorized", "too many", "max connection")
UNREACHABLE_MARKS = ("timed out", "timeout", "connection refused", "no route", "unreachable",
                     "404", "connection reset", "end of file", "could not open")


def looks_refused(detail: str | None) -> bool:
    d = (detail or "").lower()
    return any(m in d for m in REFUSED_MARKS)


def looks_like_connection_problem(detail: str | None) -> bool:
    """The camera wasn't reachable, or refused us - as opposed to something
    about the stream itself (its sound, say)."""
    d = (detail or "").lower()
    return looks_refused(d) or any(m in d for m in UNREACHABLE_MARKS)


@dataclass
class _State:
    fails: int = 0
    refusals: int = 0
    optional_until: float = 0.0
    refused_until: float = 0.0
    probing_until: float = 0.0  # an optional connection is trying
    essential_probing_until: float = 0.0  # an essential one is (it never waits for an optional one)


def _rest(n: int, first: float, longest: float) -> float:
    return min(first * 2 ** max(0, n - 1), longest)


class CameraGate:
    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._states: dict[int, _State] = {}

    def _s(self, camera_id: int) -> _State:
        return self._states.setdefault(camera_id, _State())

    # ── reports ──────────────────────────────────────────────────────────

    def failed(self, camera_id: int, detail: str | None = None) -> None:
        now = self._clock()
        with self._lock:
            s = self._s(camera_id)
            s.fails += 1
            s.probing_until = s.essential_probing_until = 0.0
            s.optional_until = now + _rest(s.fails, *OPTIONAL_REST)
            if looks_refused(detail):
                s.refusals += 1
                s.refused_until = now + _rest(s.refusals, *REFUSED_REST)

    def ok(self, camera_id: int) -> None:
        with self._lock:
            self._states.pop(camera_id, None)

    # ── asking ───────────────────────────────────────────────────────────

    def wait_seconds(self, camera_id: int, essential: bool) -> float:
        """0 = go ahead now (and, after a rest, this caller is the one that
        tries first). Otherwise how long to wait before asking again."""
        now = self._clock()
        with self._lock:
            s = self._states.get(camera_id)
            if s is None:
                return 0.0
            until = s.refused_until if essential else max(s.optional_until, s.refused_until)
            if now < until:
                return until - now
            if not essential and s.fails == 0:
                return 0.0
            if essential and s.refusals == 0:
                return 0.0
            # The rest is over: one caller tries, the others wait for its answer.
            if essential:
                if now < s.essential_probing_until:
                    return min(s.essential_probing_until - now, 5.0)
                s.essential_probing_until = now + PROBE_SECONDS
                return 0.0
            busy = max(s.probing_until, s.essential_probing_until)
            if now < busy:
                return min(busy - now, 5.0)
            s.probing_until = now + PROBE_SECONDS
            return 0.0

    async def wait(self, camera_id: int, essential: bool, stop: asyncio.Event | None = None) -> bool:
        """Wait until this camera may be tried. False if `stop` was set."""
        while True:
            left = self.wait_seconds(camera_id, essential)
            if left <= 0:
                return True
            if stop is None:
                await asyncio.sleep(min(left, 5.0))
            else:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=min(left, 5.0))
                    return False
                except asyncio.TimeoutError:
                    pass

    def wait_thread(self, camera_id: int, essential: bool, stop: threading.Event) -> bool:
        """The same, for the decoder threads. False if `stop` was set."""
        while not stop.is_set():
            left = self.wait_seconds(camera_id, essential)
            if left <= 0:
                return True
            stop.wait(min(left, 5.0))
        return False

    def status(self) -> dict[int, dict]:
        now = self._clock()
        with self._lock:
            return {cid: {"fails": s.fails, "refusals": s.refusals,
                          "optional_rest_s": round(max(0.0, s.optional_until - now)),
                          "refused_rest_s": round(max(0.0, s.refused_until - now))}
                    for cid, s in self._states.items()}


camera_gate = CameraGate()
