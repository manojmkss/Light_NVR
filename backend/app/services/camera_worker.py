import asyncio
import logging
import os
import time
from datetime import datetime, timezone

import cv2

from app.models.camera import Camera
from app.services.ai.digest import _display_tz
from app.services.ai.pipeline import ai_pipeline
from app.services.ai.types import DetectedObject
from app.services.events import emit_event
from app.services.ffmpeg_recorder import ContinuousRecorder, MotionRecorder
from app.services.frame_bus import frame_bus
from app.services.motion_event import MotionEventSession
from app.services.motion_state import motion_state_registry
from app.services.status_tracker import mark_offline
from app.services.stream_viewer import StreamViewer

logger = logging.getLogger(__name__)


def _encode_jpeg(frame, quality: int = 90) -> bytes | None:
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buf.tobytes() if ok else None


async def _describe(settings, jpeg: bytes, labels: list[str], camera_name: str, prompt: str) -> str:
    from app.services.ai.vlm import describe_frame

    return await describe_frame(settings, jpeg, labels, camera_name, prompt=prompt)


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
        # Record the H.264 sub-stream as a browser-playback copy only when the
        # main is H.265 (what browsers can't decode) - an H.264 main already
        # plays directly, so a copy would just waste a stream + disk. The
        # recorders further require the sub to be a distinct URL from the main.
        self._playback_sub_url = self.rtsp_sub_url if camera.codec == "h265" else None
        self.has_audio = camera.has_audio
        self.recording_mode = camera.recording_mode
        self.motion_enabled = camera.motion_enabled or camera.recording_mode == "motion"
        self.motion_sensitivity = camera.motion_sensitivity
        self.motion_zones = camera.motion_zones
        self.pre_roll_seconds = camera.pre_roll_seconds if camera.pre_roll_seconds is not None else 5
        self.post_roll_seconds = camera.post_roll_seconds if camera.post_roll_seconds is not None else 10

        self._tasks: list[asyncio.Task] = []
        # Held only so the loop keeps a strong reference to in-flight AI
        # analyses (asyncio only weakly references tasks); entries remove
        # themselves on completion.
        self._announce_tasks: set[asyncio.Task] = set()
        self._continuous_recorder: ContinuousRecorder | None = None
        self._motion_recorder: MotionRecorder | None = None
        self._stream_viewer: StreamViewer | None = None
        self._stopping = False
        # The motion event being watched by AI, and the post-roll timer that
        # ends it (cancelled if motion returns).
        self._session: MotionEventSession | None = None
        self._session_grace: asyncio.Task | None = None
        # Recently saved motion clips, so an event's summary can link its clip.
        self._clips: list[tuple[object, float, float]] = []
        self._clips_changed = asyncio.Condition()

    async def start(self) -> None:
        if self.recording_mode == "continuous":
            self._tasks.append(asyncio.create_task(self._run_continuous_supervised()))

        if self.recording_mode == "motion":
            self._motion_recorder = MotionRecorder(
                self.camera_id,
                self.rtsp_main_url,
                self.has_audio,
                sub_url=self._playback_sub_url,
                pre_roll_seconds=self.pre_roll_seconds,
                post_roll_seconds=self.post_roll_seconds,
                on_clip_saved=self._on_clip_saved,
            )
            # The rolling buffer runs all the time, so a clip can start before
            # the motion that triggered it.
            await self._motion_recorder.start()

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
        if self._session_grace is not None:
            self._session_grace.cancel()
            self._session_grace = None
        if self._motion_recorder:
            await self._motion_recorder.stop()
        # An event still open (or in its post-roll) gets its summary now. The
        # clip was just saved by the recorder's stop above.
        session, self._session = self._session, None
        if session is not None:
            try:
                await asyncio.wait_for(self._finalize_session(session, clip_timeout=2), timeout=15)
            except asyncio.TimeoutError:
                logger.warning("Timed out finalising motion event for camera %s on stop", self.camera_id)
        if self._announce_tasks:
            await asyncio.wait(list(self._announce_tasks), timeout=10)
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
            self._continuous_recorder = ContinuousRecorder(
                self.camera_id, self.rtsp_main_url, self.has_audio, sub_url=self._playback_sub_url
            )
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
        # Recording first and never waits on AI: the buffer makes this instant,
        # and the clip already reaches back pre_roll seconds.
        if self._motion_recorder:
            await self._motion_recorder.on_motion_start()

        if self._session_grace is not None:
            # Motion came back within the post-roll: same event continues.
            self._session_grace.cancel()
            self._session_grace = None
        if self._session is not None:
            self._session.mark_motion_resume()
            return

        try:
            settings = await ai_pipeline.settings()
        except Exception:
            logger.exception("Could not load AI settings for camera %s - plain motion event", self.camera_id)
            settings = None
        tz = timezone.utc
        try:
            tz = await _display_tz()
        except Exception:
            pass
        viewer = self._stream_viewer
        self._session = MotionEventSession(
            self.camera_id,
            self.name,
            settings,
            motion_active=lambda: bool(viewer and viewer.motion_active),
            hold_motion=lambda seconds: viewer.hold_motion(seconds) if viewer else None,
            motion_level=lambda: (viewer.last_sample.change_pct if viewer and viewer.last_sample else 0.0),
            get_frame=frame_bus.get_fresh,
            detect=ai_pipeline.detect_frame,
            emit=self._emit_with_snapshot,
            describe=_describe,
            tz=tz,
        )
        self._session.start()
        if not (viewer and viewer.motion_active):
            # Motion already ended while settings were loading - its stop
            # callback found no session, so end this one the same way.
            await self._handle_motion_stop()

    async def _handle_motion_stop(self) -> None:
        motion_state_registry.set_motion(self.camera_id, False)
        if self._motion_recorder:
            await self._motion_recorder.on_motion_stop()
        if self._session is not None:
            self._session.mark_motion_end()
            if self._session_grace is not None:
                self._session_grace.cancel()
            # Same window the recorder keeps the clip open for, so one event =
            # one clip = one summary.
            self._session_grace = asyncio.create_task(self._end_session_after(self.post_roll_seconds))

    async def _end_session_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        session, self._session, self._session_grace = self._session, None, None
        if session is not None:
            self._track(asyncio.create_task(self._finalize_session(session)))

    def _track(self, task: asyncio.Task) -> None:
        self._announce_tasks.add(task)
        task.add_done_callback(self._announce_tasks.discard)

    async def _emit_with_snapshot(self, camera_id: int, event_type: str, message: str, jpeg: bytes | None) -> int | None:
        snapshot_path = await asyncio.to_thread(_write_event_snapshot, camera_id, jpeg) if jpeg else None
        event = await emit_event(camera_id, event_type, message, snapshot_path=snapshot_path)
        return event.id

    async def _on_clip_saved(self, recording, clip_start: float, clip_end: float) -> None:
        async with self._clips_changed:
            self._clips.append((recording, clip_start, clip_end))
            del self._clips[:-10]
            self._clips_changed.notify_all()

    async def _clip_for(self, session: MotionEventSession, timeout: float) -> tuple[object | None, tuple[float, float] | None]:
        """The motion clip(s) covering this event - saved a moment after the
        post-roll, once the buffer segment containing the end has closed."""
        end = session.ended_wall or time.time()
        deadline = time.time() + timeout

        def overlapping():
            return [c for c in self._clips if c[2] > session.started_wall and c[1] < end]

        async with self._clips_changed:
            while not any(c[2] >= end - 1 for c in overlapping()):
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self._clips_changed.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
            clips = overlapping()
        if not clips:
            return None, None
        return clips[0][0], (min(c[1] for c in clips), max(c[2] for c in clips))

    async def _finalize_session(self, session: MotionEventSession, clip_timeout: float = 45) -> None:
        try:
            await session.stop_monitoring()
            recording, clip = (None, None)
            if self._motion_recorder is not None:
                recording, clip = await self._clip_for(session, timeout=clip_timeout)

            event_id = session.alert_event_id
            s = session.settings
            if s is None or getattr(s, "event_summary_enabled", True) is not False:
                summary = await session.build_summary(clip)
                if summary is not None:
                    event_id = await self._emit_with_snapshot(
                        self.camera_id, "motion_summary", summary.message, summary.snapshot
                    ) or event_id

            # One detection row per tracked object (not per frame), with its
            # clearest frame - linked to the event and the clip.
            recording_id = getattr(recording, "id", None)
            for t in session.tracker.confirmed():
                if t.best_frame is None:
                    continue
                jpeg = await asyncio.to_thread(_encode_jpeg, t.best_frame)
                if not jpeg:
                    continue
                await ai_pipeline.store_detections(
                    self.camera_id,
                    [DetectedObject(t.label, t.best_conf, *t.best_box)],
                    jpeg,
                    event_id=event_id,
                    recording_id=recording_id,
                    seen_at=datetime.fromtimestamp(t.best_at, timezone.utc),
                )
        except Exception:
            logger.exception("Could not finalise motion event for camera %s", self.camera_id)
