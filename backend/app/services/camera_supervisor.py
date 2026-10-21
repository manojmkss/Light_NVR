import asyncio
import logging
import time

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.camera import Camera
from app.services.camera_worker import CameraWorker

logger = logging.getLogger(__name__)

HEALTH_INTERVAL = 30.0
FIRST_RESTART_DELAY = 60.0  # minimum gap between automatic restarts of one camera
MAX_RESTART_DELAY = 3600.0


class CameraSupervisor:
    """Owns the set of running CameraWorkers and is the single place that
    starts/stops them - on app boot (recovery), on camera create/update/delete,
    and on app shutdown.

    It is also the self-healing layer: every HEALTH_INTERVAL seconds each
    worker reports whether it is making progress (live-stream thread alive and
    moving, recordings/buffer producing while the camera is online). A worker
    that isn't gets restarted, with an increasing delay between attempts if
    the problem keeps coming back, and an event so the user knows.
    """

    def __init__(self):
        self._workers: dict[int, CameraWorker] = {}
        # Serialises start/stop per camera: two quick edits of the same camera
        # (or an edit racing a self-heal restart) must not leave two workers -
        # and two ffmpeg recorders - running for one camera.
        self._locks: dict[int, asyncio.Lock] = {}
        self._restarts: dict[int, tuple[int, float]] = {}  # camera -> (count, last restart, monotonic)
        self._health_task: asyncio.Task | None = None

    def _lock(self, camera_id: int) -> asyncio.Lock:
        return self._locks.setdefault(camera_id, asyncio.Lock())

    async def start_all(self) -> None:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Camera).where(Camera.enabled.is_(True)))
            cameras = result.scalars().all()
        for camera in cameras:
            # One camera that fails to start (bad config, a transient error)
            # must not keep the others - or the whole NVR - from starting.
            try:
                async with self._lock(camera.id):
                    await self._start_worker(camera)
            except Exception:
                logger.exception("Could not start camera %s ('%s')", camera.id, camera.name)
        logger.info("Camera supervisor started %d worker(s)", len(self._workers))
        if self._health_task is None:
            self._health_task = asyncio.create_task(self._health_loop())

    async def sync_camera(self, camera: Camera) -> None:
        async with self._lock(camera.id):
            await self._remove_locked(camera.id)
            if camera.enabled:
                await self._start_worker(camera)

    async def remove_camera(self, camera_id: int) -> None:
        async with self._lock(camera_id):
            await self._remove_locked(camera_id)

    async def _remove_locked(self, camera_id: int) -> None:
        worker = self._workers.pop(camera_id, None)
        if worker:
            try:
                # Generous overall cap: worker.stop() bounds each of its own
                # steps, this is the backstop that guarantees a restart can't
                # be blocked forever by a stuck stop.
                await asyncio.wait_for(worker.stop(), timeout=60)
            except Exception:
                logger.exception("Camera %s did not stop cleanly - continuing", camera_id)

    async def shutdown(self) -> None:
        if self._health_task is not None:
            self._health_task.cancel()
            await asyncio.gather(self._health_task, return_exceptions=True)
            self._health_task = None
        # In parallel: each worker may spend a few seconds saving an open
        # motion clip and its summary, and a container stop only allows a
        # limited time in total before everything is killed.
        results = await asyncio.gather(
            *(self.remove_camera(cid) for cid in list(self._workers.keys())), return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                logger.error("Camera worker failed to stop cleanly: %r", result)

    def get_active_camera_ids(self) -> list[int]:
        return list(self._workers.keys())

    def health(self) -> dict[int, dict]:
        out = {}
        for cid, w in self._workers.items():
            ok, reason = w.health()
            count, _ = self._restarts.get(cid, (0, 0.0))
            out[cid] = {"healthy": ok, "reason": reason, "auto_restarts": count}
        return out

    async def _start_worker(self, camera: Camera) -> None:
        worker = CameraWorker(camera)
        try:
            await worker.start()
        except Exception:
            # Half-started (e.g. the buffer ffmpeg launched, then the viewer
            # failed): stop what did start, so nothing is left orphaned.
            try:
                await asyncio.wait_for(worker.stop(), timeout=30)
            except Exception:
                logger.exception("Cleanup after a failed start of camera %s also failed", camera.id)
            raise
        self._workers[camera.id] = worker

    # ── self-healing ─────────────────────────────────────────────────────────

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(HEALTH_INTERVAL)
            try:
                await self._check_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Camera health check failed")

    async def _check_all(self) -> None:
        now = time.monotonic()
        for cid, worker in list(self._workers.items()):
            try:
                ok, reason = worker.health()
            except Exception as exc:
                ok, reason = False, f"health check raised {exc!r}"
            if ok:
                count, last = self._restarts.get(cid, (0, 0.0))
                # Healthy for a good while again: forget past restarts.
                if count and now - last > MAX_RESTART_DELAY:
                    self._restarts.pop(cid, None)
                continue
            count, last = self._restarts.get(cid, (0, 0.0))
            delay = min(MAX_RESTART_DELAY, FIRST_RESTART_DELAY * (2 ** count))
            if count and now - last < delay:
                continue  # backing off: the last restart didn't fix it
            self._restarts[cid] = (count + 1, now)
            await self._self_heal(cid, worker, reason, count + 1)

    async def _self_heal(self, camera_id: int, worker: CameraWorker, reason: str, attempt: int) -> None:
        logger.warning("Camera %s ('%s') unhealthy: %s - restarting it (attempt %d)",
                       camera_id, worker.name, reason, attempt)
        try:
            from app.services.events import emit_event

            await emit_event(
                camera_id, "camera_error",
                f"Camera '{worker.name}': {reason} - restarted automatically"
                + (f" (attempt {attempt}; will keep retrying less often)" if attempt > 1 else ""),
            )
        except Exception:
            logger.exception("Could not record self-heal event")
        async with AsyncSessionLocal() as db:
            camera = await db.get(Camera, camera_id)
            if camera is not None:
                db.expunge(camera)
        if camera is None:
            await self.remove_camera(camera_id)
            return
        try:
            await self.sync_camera(camera)
        except Exception:
            logger.exception("Self-heal restart of camera %s failed", camera_id)


supervisor = CameraSupervisor()
