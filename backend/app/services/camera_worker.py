import asyncio
import logging

from app.models.camera import Camera
from app.services.events import emit_event
from app.services.ffmpeg_recorder import ContinuousRecorder, MotionRecorder
from app.services.status_tracker import mark_offline
from app.services.stream_viewer import StreamViewer

logger = logging.getLogger(__name__)


class CameraWorker:
    """Owns the recording + live-view + motion-detection lifecycle for a
    single camera.

    recording_mode picks what drives the on-disk recording; motion_enabled is
    independent of that and just controls whether motion is detected/alerted
    at all - e.g. you can run continuous recording AND tag/alert on motion
    within it, or detect motion for alerting only with recording_mode=off.
    The stream viewer itself always runs while the camera is enabled, since
    live view needs frames regardless of recording mode.
    """

    def __init__(self, camera: Camera):
        self.camera_id = camera.id
        self.name = camera.name
        self.rtsp_main_url = camera.rtsp_main_url
        self.rtsp_sub_url = camera.rtsp_sub_url or camera.rtsp_main_url
        self.has_audio = camera.has_audio
        self.recording_mode = camera.recording_mode
        self.motion_enabled = camera.motion_enabled or camera.recording_mode == "motion"
        self.motion_sensitivity = camera.motion_sensitivity

        self._tasks: list[asyncio.Task] = []
        self._continuous_recorder: ContinuousRecorder | None = None
        self._motion_recorder: MotionRecorder | None = None
        self._stream_viewer: StreamViewer | None = None
        self._stopping = False

    async def start(self) -> None:
        if self.recording_mode == "continuous":
            self._tasks.append(asyncio.create_task(self._run_continuous_supervised()))

        if self.recording_mode == "motion":
            self._motion_recorder = MotionRecorder(self.camera_id, self.rtsp_main_url, self.has_audio)

        self._stream_viewer = StreamViewer(
            self.camera_id,
            self.rtsp_sub_url,
            motion_enabled=self.motion_enabled,
            sensitivity=self.motion_sensitivity,
            on_motion_start=self._handle_motion_start,
            on_motion_stop=self._handle_motion_stop,
        )
        await self._stream_viewer.start()

    async def stop(self) -> None:
        self._stopping = True
        if self._stream_viewer:
            await self._stream_viewer.stop()
        if self._motion_recorder:
            await self._motion_recorder.stop()
        if self._continuous_recorder:
            await self._continuous_recorder.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run_continuous_supervised(self) -> None:
        """ContinuousRecorder.run() already guards every iteration internally,
        so this should never actually need to restart anything - this is a
        backstop for the bug class that defense can't anticipate (e.g. a
        failure inside its own except handler), so a single camera's
        recording can never be permanently killed by one bad exception with
        nothing watching.
        """
        while not self._stopping:
            self._continuous_recorder = ContinuousRecorder(self.camera_id, self.rtsp_main_url, self.has_audio)
            try:
                await self._continuous_recorder.run()
                return  # clean stop (self._stop was set) - nothing to restart
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Continuous recorder task for camera %s ('%s') crashed - restarting", self.camera_id, self.name
                )
                await emit_event(
                    self.camera_id, "camera_error", f"Recording crashed unexpectedly on '{self.name}' and is restarting"
                )
                try:
                    await mark_offline(self.camera_id)
                except Exception:
                    pass
                await asyncio.sleep(5)

    async def _handle_motion_start(self) -> None:
        await emit_event(self.camera_id, "motion", f"Motion detected on '{self.name}'")
        if self._motion_recorder:
            await self._motion_recorder.on_motion_start()

    async def _handle_motion_stop(self) -> None:
        if self._motion_recorder:
            await self._motion_recorder.on_motion_stop()
