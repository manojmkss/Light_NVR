import asyncio
import logging
import os
import time
from datetime import datetime, timezone

import cv2

from app.models.camera import Camera
from app.services.ai.digest import _display_tz
from app.services.ai.pipeline import ai_pipeline
from app.services.ai.scheduler import scheduler
from app.services.ai.types import DetectedObject
from app.services.events import emit_event
from app.services.ffmpeg_recorder import SEGMENT_SECONDS, ContinuousRecorder, MotionRecorder
from app.services.frame_bus import frame_bus
from app.services.faces import face_service, faces_enabled
from app.services.incidents import incident_manager
from app.services.motion_detector import point_in_watch_area
from app.services.motion_event import MotionEventSession, ParkedObject
from app.services.motion_state import motion_state_registry
from app.services.status_tracker import mark_offline
from app.services.stream_viewer import StreamViewer

logger = logging.getLogger(__name__)

# Last arrival alert per person (all cameras), for the per-person cooldown.
_last_arrival: dict[int, float] = {}


def _appearance(frame, group: str, box) -> dict:
    from app.vision.attributes import object_colour, person_colours

    try:
        return person_colours(frame, box) if group == "person" else object_colour(frame, box)
    except Exception:
        return {}


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
        self.area = getattr(camera, "area", None)

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
        # Vehicles standing still on this camera, carried from one event to the
        # next so a parked car isn't "seen" again whenever someone walks past.
        self._parked: list[ParkedObject] = []

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
            on_tamper=self._handle_tamper,
        )
        await self._stream_viewer.start()

    # ── health (used by the supervisor's self-healing loop) ─────────────────

    VIEWER_STUCK_SECONDS = 300.0  # no progress at all from the decode thread
    BUFFER_STALL_SECONDS = 180.0  # motion buffer ffmpeg producing nothing

    def health(self) -> tuple[bool, str]:
        """(healthy, reason). Only reports conditions that a restart of this
        worker can fix - a camera that is simply unreachable is "offline",
        handled by the viewer's own reconnect loop, not a fault."""
        now = time.monotonic()
        v = self._stream_viewer
        if v is not None:
            if not v.thread_alive:
                return False, "live-stream thread stopped"
            if now - v.last_activity > self.VIEWER_STUCK_SECONDS:
                return False, f"live stream stuck for {int(now - v.last_activity)}s"
        for t in self._tasks:
            if t.done() and not self._stopping:
                return False, "recording task ended unexpectedly"
        rec = self._continuous_recorder
        if rec is not None and v is not None and now - v.last_frame_at < 60:
            # Camera is delivering pictures, so recording should be producing
            # segments too: a full segment + generous slack without one = stalled.
            since = now - (rec.last_segment_at or rec.started_at)
            if since > SEGMENT_SECONDS * 2 + 120:
                return False, f"no recording saved for {int(since // 60)} min while the camera is online"
        mr = self._motion_recorder
        if mr is not None and v is not None and now - v.last_frame_at < 60:
            buf = mr.buffer
            since = now - (buf.last_segment_at or buf.started_at)
            if since > self.BUFFER_STALL_SECONDS:
                return False, f"recording buffer idle for {int(since)}s while the camera is online"
        return True, "ok"

    async def stop(self) -> None:
        """Every step is isolated and time-limited: one stuck part (a hung
        ffmpeg, a slow DB) must never stop the rest from shutting down, or a
        restart of this camera from completing."""
        self._stopping = True

        async def step(name, coro, timeout):
            try:
                await asyncio.wait_for(coro, timeout=timeout)
            except Exception as exc:
                logger.warning("Camera %s: stopping %s failed (%s)", self.camera_id, name, exc or type(exc).__name__)

        if self._stream_viewer:
            await step("live stream", self._stream_viewer.stop(), 10)
        if self._session_grace is not None:
            self._session_grace.cancel()
            self._session_grace = None
        if self._motion_recorder:
            await step("motion recorder", self._motion_recorder.stop(), 20)
        # An event still open (or in its post-roll) gets its summary now. The
        # clip was just saved by the recorder's stop above.
        session, self._session = self._session, None
        if session is not None:
            # No vision-model call on the way down: it can take tens of seconds
            # and a container stop allows ~10s in total.
            session.skip_description = True
            try:
                await asyncio.wait_for(self._finalize_session(session, clip_timeout=2), timeout=15)
            except asyncio.TimeoutError:
                logger.warning("Timed out finalising motion event for camera %s on stop", self.camera_id)
        if self._announce_tasks:
            await asyncio.wait(list(self._announce_tasks), timeout=10)
        if self._continuous_recorder:
            await step("continuous recorder", self._continuous_recorder.stop(), 20)
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=5)

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
        zones = self.motion_zones
        # One visit across cameras: join (or open) the incident this belongs to.
        mode = None
        try:
            from app.services.security_mode import current_mode

            mode = (await current_mode())["mode"]
        except Exception:
            pass
        incident = member = None
        incident_alert = True
        try:
            incident, member, incident_alert = await incident_manager.join(self.camera_id, self.name, self.area, mode)
        except Exception:
            logger.exception("Could not assign camera %s's event to a visit", self.camera_id)
        holder: list = []

        async def emit(cam, kind, message, jpeg, payload=None):
            return await self._emit_for(holder[0] if holder else None, cam, kind, message, jpeg, payload)

        self._session = MotionEventSession(
            self.camera_id,
            self.name,
            settings,
            motion_active=lambda: bool(viewer and viewer.motion_active),
            hold_motion=lambda seconds: viewer.hold_motion(seconds) if viewer else None,
            motion_level=lambda: (viewer.last_sample.change_pct if viewer and viewer.last_sample else 0.0),
            get_frame=frame_bus.get_fresh,
            detect=ai_pipeline.detect_frame,
            emit=emit,
            describe=_describe,
            tz=tz,
            in_zone=lambda x, y: point_in_watch_area(zones, x, y),
            parked=list(self._parked),
            on_revive=self._revive,
            motion_bbox=lambda: (viewer.last_sample.bbox if viewer and viewer.last_sample else None),
            presence_hold=lambda seconds: viewer.set_presence_hold(seconds) if viewer else None,
            pace=scheduler.suggested_interval,
            rules=[z for z in (zones or []) if z.get("kind") in ("zone", "line")],
            recognise=(lambda frame, boxes: face_service.recognise(settings, frame, boxes))
            if faces_enabled(settings) else None,
            on_identity=lambda track: self._on_identity(holder[0] if holder else None, track),
        )
        self._session.incident, self._session.member = incident, member
        self._session.incident_alert = incident_alert
        holder.append(self._session)
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

    async def _handle_tamper(self, change: str, kind: str) -> None:
        from app.services.tamper import MESSAGES

        frame = frame_bus.get_fresh(self.camera_id)
        jpeg = await asyncio.to_thread(_encode_jpeg, frame[0]) if frame is not None else None
        if change == "started":
            # Always urgent: someone covering or turning a camera is exactly
            # what a security system must not stay quiet about.
            await self._emit_with_snapshot(self.camera_id, "tamper", MESSAGES[kind].format(name=self.name), jpeg,
                                           {"tamper": kind}, notify=True)
        else:
            await self._emit_with_snapshot(self.camera_id, "system", f"Camera '{self.name}' picture is back to normal",
                                           jpeg, {"tamper_cleared": kind}, notify=False)

    async def _revive(self, hold_seconds: float) -> None:
        """AI still sees a person after pixel motion stopped (they're standing
        still): carry on with the same event instead of letting the post-roll
        close it - one visit stays one clip and one summary."""
        if self._session is None or self._session_grace is None:
            return
        self._session_grace.cancel()
        self._session_grace = None
        motion_state_registry.set_motion(self.camera_id, True)
        if self._motion_recorder:
            await self._motion_recorder.on_motion_start()
        if self._stream_viewer:
            self._stream_viewer.revive_motion(hold_seconds)
        self._session.mark_motion_resume()

    async def _end_session_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        session, self._session, self._session_grace = self._session, None, None
        if session is not None:
            self._track(asyncio.create_task(self._finalize_session(session)))

    def _track(self, task: asyncio.Task) -> None:
        self._announce_tasks.add(task)
        task.add_done_callback(self._announce_tasks.discard)

    async def _emit_with_snapshot(
        self, camera_id: int, event_type: str, message: str, jpeg: bytes | None, payload: dict | None = None,
        incident_id: int | None = None, notify: bool = True,
    ) -> int | None:
        snapshot_path = await asyncio.to_thread(_write_event_snapshot, camera_id, jpeg) if jpeg else None
        event = await emit_event(
            camera_id, event_type, message, snapshot_path=snapshot_path, payload=payload,
            incident_id=incident_id, notify=notify,
        )
        return event.id

    async def _on_identity(self, session, track) -> None:
        """A person on this camera was recognised: tell the visit, and send an
        arrival alert if that person has one (with its own cooldown)."""
        ident = track.identity or {}
        inc = getattr(session, "incident", None)
        new_to_visit = True
        if inc is not None and ident.get("name"):
            new_to_visit = incident_manager.add_identity(inc, ident["name"])
        person = face_service.person(ident.get("id")) if ident.get("id") else None
        if person is None or not person.arrival_alert or not new_to_visit:
            return
        now = time.time()
        last = _last_arrival.get(person.id, 0.0)
        if now - last < (person.arrival_cooldown_s or 0):
            return
        _last_arrival[person.id] = now
        who = f"{person.name} ({person.note})" if person.note else person.name
        frame = frame_bus.get_fresh(self.camera_id)
        jpeg = None
        if frame is not None:
            from app.services.motion_event import _jpeg, draw_boxes

            jpeg = await asyncio.to_thread(lambda: _jpeg(draw_boxes(frame[0], [(person.name, ident.get("score", 0), track.box)])))
        payload = {"objects": ["person"], "identities": [f"person:{person.id}"], "names": [person.name],
                   "categories": [person.category]}
        await self._emit_for(session, self.camera_id, "arrival", f"{who} arrived at '{self.name}'", jpeg, payload)

    async def _emit_for(self, session, camera_id, event_type, message, jpeg, payload=None) -> int | None:
        """Session events, tied to their visit. A camera joining a visit that
        another camera already alerted about stays quiet unless the visit
        escalated (see incidents.py)."""
        inc = getattr(session, "incident", None)
        notify = True
        if inc is not None:
            payload = dict(payload or {}, incident_id=inc.id)
            if event_type == "motion" and not getattr(session, "incident_alert", True):
                notify = False
        event_id = await self._emit_with_snapshot(
            camera_id, event_type, message, jpeg, payload, incident_id=inc.id if inc else None, notify=notify,
        )
        if inc is not None and event_type == "motion" and notify:
            incident_manager.mark_alerted(inc)
        return event_id

    async def _on_clip_saved(self, recording, clip_start: float, clip_end: float) -> None:
        async with self._clips_changed:
            self._clips.append((recording, clip_start, clip_end))
            del self._clips[:-10]
            self._clips_changed.notify_all()

    async def _clip_for(
        self, session: MotionEventSession, timeout: float
    ) -> tuple[object | None, tuple[float, float] | None, int]:
        """The motion clip(s) covering this event - saved a moment after the
        post-roll, once the buffer segment containing the end has closed."""
        end = session.ended_wall or time.time()
        deadline = time.time() + timeout

        def overlapping():
            # At least 1s of real overlap: the previous event's clip can end up
            # to one buffer segment after this event started, and must not be
            # mistaken for this one's.
            return [c for c in self._clips if min(c[2], end) - max(c[1], session.started_wall) >= 1.0]

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
            return None, None, 0
        # A long wait crosses the 10-minute clip limit: several parts, one event.
        return clips[0][0], (min(c[1] for c in clips), max(c[2] for c in clips)), len(clips)

    async def _finalize_session(self, session: MotionEventSession, clip_timeout: float = 45) -> None:
        try:
            await session.stop_monitoring()
            if session.ai_on and session.ai_worked:
                # What's standing still now is the starting point for the next event.
                self._parked = session.parked_objects()
            recording, clip, parts = (None, None, 0)
            if self._motion_recorder is not None:
                recording, clip, parts = await self._clip_for(session, timeout=clip_timeout)

            event_id = session.alert_event_id
            s = session.settings
            inc, member = getattr(session, "incident", None), getattr(session, "member", None)
            summary = None
            if s is None or getattr(s, "event_summary_enabled", True) is not False:
                summary = await session.build_summary(clip, clip_parts=parts)
            own_summary = True
            names = [i["name"] for i in session.identities()]
            if inc is not None and member is not None:
                waited = [
                    (f"{t.label} #{t.id}", t.max_still, (t.zones[-1] if t.zones else member.area))
                    for t in session.tracker.confirmed() if t.group == "person"
                ]
                own_summary = await incident_manager.leave(
                    inc, member, session.tracker.totals(), waited, clip,
                    summary.snapshot if summary else None, names,
                )
            if summary is not None:
                payload = session.summary_payload()
                if inc is not None:
                    payload["incident_id"] = inc.id
                # Part of a multi-camera visit: stored, but the visit summary
                # (sent when the visit ends) replaces the per-camera one.
                event_id = await self._emit_with_snapshot(
                    self.camera_id, "motion_summary", summary.message, summary.snapshot, payload,
                    incident_id=inc.id if inc else None, notify=own_summary,
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
                track_uid = f"{self.camera_id}:{int(session.started_wall * 1000)}:{t.label}{t.id}"
                if t.best_face is not None and t.best_face.embedding is not None:
                    try:
                        # Every face seen is kept for "Who is this?" (unknown
                        # samples expire after a few days); recognised ones too,
                        # but only the user can add a sighting to a person.
                        await face_service.save_sample(t.best_face, None, self.camera_id, t.best_face_score, track_uid)
                    except Exception:
                        logger.exception("Could not save face sample")
                extra = {
                    "track_uid": track_uid,
                    "person_id": (t.identity or {}).get("id"),
                    "face_score": (t.identity or {}).get("score"),
                    "first_seen": datetime.fromtimestamp(t.since_wall or session.started_wall, timezone.utc),
                    "last_seen": datetime.fromtimestamp(t.last_wall or session.ended_wall or time.time(), timezone.utc),
                    "stationary_seconds": round(t.max_still, 1),
                    "zones": t.zones or None,
                    "incident_id": inc.id if inc is not None else None,
                    "attributes": await asyncio.to_thread(_appearance, t.best_frame, t.group, t.best_box) or None,
                }
                await ai_pipeline.store_detections(
                    self.camera_id,
                    [DetectedObject(t.label, t.best_conf, *t.best_box)],
                    jpeg,
                    event_id=event_id,
                    recording_id=recording_id,
                    seen_at=datetime.fromtimestamp(t.best_at, timezone.utc),
                    extra=extra,
                )
        except Exception:
            logger.exception("Could not finalise motion event for camera %s", self.camera_id)
