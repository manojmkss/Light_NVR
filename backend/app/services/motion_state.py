from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class _MotionState:
    is_active: bool = False
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class MotionStateRegistry:
    """Live per-camera "is motion active right now" state, keyed by camera_id.

    Unlike stream_stats (written from the OS decode thread, so it needs a
    lock), this is only ever touched from CameraWorker's async motion
    callbacks - which already run on the event loop, scheduled via
    asyncio.run_coroutine_threadsafe - so a plain dict is safe with no lock:
    there's no `await` between a read and a write here, and nothing accesses
    it from another thread.

    emit_event() only logs a one-shot "motion started" row (see
    CameraWorker._handle_motion_start) - there's no historical "motion
    stopped" event and nothing else tracks the current active/inactive state,
    which is what this registry exists to expose to API consumers (e.g. a
    Home Assistant motion sensor) without polling the event log.
    """

    def __init__(self) -> None:
        self._states: dict[int, _MotionState] = {}

    def set_motion(self, camera_id: int, is_active: bool) -> None:
        self._states[camera_id] = _MotionState(is_active=is_active, last_updated=datetime.now(timezone.utc))

    def snapshot(self) -> dict[int, _MotionState]:
        return dict(self._states)


motion_state_registry = MotionStateRegistry()
