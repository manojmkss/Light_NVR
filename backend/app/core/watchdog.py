"""Process watchdog: turn a hang into a restart.

Docker restarts a container that *exits*, not one that is alive but stuck -
and a stuck NVR (event loop blocked by a hung NAS call, a deadlock, runaway
memory) silently stops recording and alerting. This watchdog runs in its own
OS thread, independent of the asyncio loop:

- the loop must tick a heartbeat every few seconds; if it hasn't for
  LOOP_STALL_SECONDS, every thread's stack is logged (so the cause can be
  found afterwards) and the process exits - Docker's restart policy brings
  it straight back;
- if memory stays above MEMORY_EXIT_FRACTION of the container limit, the
  process exits cleanly-ish before the kernel OOM killer picks a victim at
  random (which could leave the DB mid-write).

Exit codes: 70 = event loop stalled, 71 = memory exhausted.
"""

import asyncio
import faulthandler
import logging
import os
import sys
import threading
import time

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 5.0
LOOP_STALL_SECONDS = float(os.environ.get("LIGHTNVR_WATCHDOG_STALL_SECONDS", "90"))
MEMORY_EXIT_FRACTION = 0.95
MEMORY_STRIKES = 3  # consecutive over-limit checks before exiting


def _read_int(path: str) -> int | None:
    try:
        with open(path) as f:
            v = f.read().strip()
        return None if v == "max" else int(v)
    except (OSError, ValueError):
        return None


class Watchdog:
    def __init__(self) -> None:
        self._last_beat = time.monotonic()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._mem_strikes = 0
        self.enabled = os.environ.get("LIGHTNVR_WATCHDOG", "1") != "0"

    def _beat(self) -> None:
        self._last_beat = time.monotonic()

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._loop = loop
        self._beat()
        self._thread = threading.Thread(target=self._run, name="watchdog", daemon=True)
        self._thread.start()
        logger.info("Watchdog armed (event loop stall limit %ss)", int(LOOP_STALL_SECONDS))

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            try:
                loop.call_soon_threadsafe(self._beat)
            except RuntimeError:
                return  # loop closed during shutdown
            stalled = time.monotonic() - self._last_beat
            if stalled > LOOP_STALL_SECONDS:
                self._die(70, f"event loop unresponsive for {stalled:.0f}s")
            self._check_memory()

    def _check_memory(self) -> None:
        limit = _read_int("/sys/fs/cgroup/memory.max")
        used = _read_int("/sys/fs/cgroup/memory.current")
        if not limit or not used:
            return
        # Page cache counts towards memory.current but is reclaimable; use
        # the anonymous (really used) part when the kernel reports it.
        try:
            with open("/sys/fs/cgroup/memory.stat") as f:
                stat = dict(line.split() for line in f if line.count(" ") == 1)
            used = int(stat.get("anon", used))
        except (OSError, ValueError):
            pass
        if used > MEMORY_EXIT_FRACTION * limit:
            self._mem_strikes += 1
            logger.warning("Memory at %.0f%% of the container limit", 100 * used / limit)
            if self._mem_strikes >= MEMORY_STRIKES:
                self._die(71, f"memory {used // 2**20} MB of {limit // 2**20} MB")
        else:
            self._mem_strikes = 0

    def _die(self, code: int, why: str) -> None:
        try:
            logger.critical("WATCHDOG: %s - restarting the process. Thread stacks follow.", why)
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            sys.stderr.flush()
        finally:
            os._exit(code)


watchdog = Watchdog()
