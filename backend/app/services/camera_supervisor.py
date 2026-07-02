import logging

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.camera import Camera
from app.services.camera_worker import CameraWorker

logger = logging.getLogger(__name__)


class CameraSupervisor:
    """Owns the set of running CameraWorkers and is the single place that
    starts/stops them - on app boot (recovery), on camera create/update/delete,
    and on app shutdown.
    """

    def __init__(self):
        self._workers: dict[int, CameraWorker] = {}

    async def start_all(self) -> None:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Camera).where(Camera.enabled.is_(True)))
            cameras = result.scalars().all()
        for camera in cameras:
            await self._start_worker(camera)
        logger.info("Camera supervisor started %d worker(s)", len(self._workers))

    async def sync_camera(self, camera: Camera) -> None:
        await self.remove_camera(camera.id)
        if camera.enabled:
            await self._start_worker(camera)

    async def remove_camera(self, camera_id: int) -> None:
        worker = self._workers.pop(camera_id, None)
        if worker:
            await worker.stop()

    async def shutdown(self) -> None:
        for camera_id in list(self._workers.keys()):
            await self.remove_camera(camera_id)

    def get_active_camera_ids(self) -> list[int]:
        return list(self._workers.keys())

    async def _start_worker(self, camera: Camera) -> None:
        worker = CameraWorker(camera)
        await worker.start()
        self._workers[camera.id] = worker


supervisor = CameraSupervisor()
