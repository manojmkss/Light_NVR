import asyncio
import logging
import os
from datetime import datetime, timezone

from app.models.camera import Camera
from app.services.ai.pipeline import ai_pipeline
from app.services.events import emit_event
from app.services.ffmpeg_recorder import ContinuousRecorder, MotionRecorder
from app.services.frame_bus import frame_bus
from app.services.motion_state import motion_state_registry
from app.services.status_tracker import mark_offline
from app.services.stream_viewer import StreamViewer

logger = logging.getLogger(__name__)


def _write_event_snapshot(camera_id: int, jpeg: bytes) -> str | None:
    """The frame at the moment motion started, for alert channels and the
    dashboard to show a picture instead of just text. Best-effort: a failed
    write just means the alert goes out without one, not a failed event."""
    try:
        from app.services.storage_manager import storage_manager

        directory = os.path.join(storage_manager.cache_dir(), "events", str(camera_id))
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}.jpg")
        with open(path, "wb") as f:
            f.write(jpeg)
        return path
    except Exception:
        logger.exception("Could not write motion snapshot for camera %s", camera_id)
        return None


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
        self.motion_zones = camera.motion_zones

        self._tasks: list[asyncio.Task] = []
        # Held only so the loop keeps a strong reference to in-flight AI
        # analyses (asyncio only weakly references tasks); entries remove
        # themselves on completion.
        self._announce_tasks: set[asyncio.Task] = set()
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
            motion_zones=self.motion_zones,
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
        motion_state_registry.set_motion(self.camera_id, True)
        # Recording starts first and never waits on AI: inference takes tens to
        # hundreds of milliseconds, and that is footage you would lose off the
        # front of the clip. The event/alert is what AI gets to filter, below.
        if self._motion_recorder:
            await self._motion_recorder.on_motion_start()

        # Fire-and-forget so a slow/hung inference can't stall the decode
        # thread's callback and back the motion pipeline up behind it.
        task = asyncio.create_task(self._announce_motion())
        self._announce_tasks.add(task)
        task.add_done_callback(self._announce_tasks.discard)

    async def _announce_motion(self) -> None:
        """Emit the motion event, optionally filtered/enriched by the AI layer.

        When AI is off, unconfigured, or failing, `analyze_motion` returns None
        and this is byte-for-byte the old behaviour - that fallback is the
        whole safety story for making AI optional.
        """
        jpeg = frame_bus.get_latest(self.camera_id)
        snapshot_path = await asyncio.to_thread(_write_event_snapshot, self.camera_id, jpeg) if jpeg else None

        try:
            result = await ai_pipeline.analyze_motion(self.camera_id, self.name)
        except Exception:
            logger.exception("AI analysis crashed for camera %s - emitting plain motion event", self.camera_id)
            result = None

        if result is None:
            await emit_event(self.camera_id, "motion", f"Motion detected on '{self.name}'", snapshot_path=snapshot_path)
            return
        if result.suppressed:
            return  # objects-only mode: nothing of interest was in frame
        await emit_event(
            self.camera_id,
            "motion",
            result.message or f"Motion detected on '{self.name}'",
            snapshot_path=snapshot_path,
        )

    async def _handle_motion_stop(self) -> None:
        motion_state_registry.set_motion(self.camera_id, False)
        if self._motion_recorder:
            await self._motion_recorder.on_motion_stop()
