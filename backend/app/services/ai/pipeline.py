import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from app.db.session import AsyncSessionLocal
from app.models.ai_settings import AISettings
from app.models.detection import Detection
from app.services.ai.backends import InferenceBackend, build_backend
from app.services.ai.scheduler import P_MOVING, SKIPPED, scheduler
from app.services.ai.types import DetectedObject

# Weakest detection passed to the tracker (see detect_frame).
LOW_CONFIDENCE = 0.15

logger = logging.getLogger(__name__)


class AIPipeline:
    """The cascade: motion (free, already running) gates object detection,
    which in later tiers gates the more expensive stages.

    Everything here is best-effort. Any failure - model missing, GPU box off,
    bad frame - must degrade to plain motion behaviour rather than break
    detection, alerting, or recording. An NVR that stops recording because an
    AI model failed to load is far worse than one with no AI at all.
    """

    def __init__(self) -> None:
        self._backend: InferenceBackend | None = None
        self._config_key: tuple | None = None
        self._lock = asyncio.Lock()
        self._fail_since: float | None = None
        self._outage_reported = False

    @staticmethod
    def _key(s: AISettings) -> tuple:
        # Only the fields that change *how inference runs*. Rebuilding on every
        # unrelated settings edit would drop the loaded ONNX session and pay
        # the model-load cost again on the next motion event.
        return (s.backend, s.remote_url, s.remote_api_key, s.detection_model)

    async def _load_settings(self) -> AISettings | None:
        async with AsyncSessionLocal() as db:
            settings = await db.get(AISettings, 1)
            if settings is None:
                return None
            db.expunge(settings)  # detached read-only copy; no session held open
            return settings

    async def _backend_for(self, settings: AISettings) -> InferenceBackend:
        key = self._key(settings)
        async with self._lock:
            if self._backend is None or self._config_key != key:
                if self._backend is not None:
                    await self._backend.close()
                self._backend = build_backend(settings)
                self._config_key = key
                logger.info("AI: using %s backend (model=%s)", settings.backend, settings.detection_model)
            return self._backend

    def _wanted(self, settings: AISettings) -> set[str]:
        return {c.strip().lower() for c in settings.detection_classes.split(",") if c.strip()}

    async def settings(self) -> AISettings | None:
        """Detached copy of the AI settings, or None if the row is missing."""
        return await self._load_settings()

    async def detect_frame(
        self, settings: AISettings, frame, camera_id: int = 0, priority: int = P_MOVING
    ) -> list[DetectedObject] | None | object:
        """Objects of interest in one decoded frame (class filter applied).

        Returns boxes down to LOW_CONFIDENCE, not just the user's threshold:
        the tracker uses the weak ones only to keep following an object it
        already knows (partly hidden, turning away) - never to start one.
        None when detection is off or failed (the caller carries on as plain
        motion); scheduler.SKIPPED when this tick's request was superseded by
        a newer frame or ran out of time."""
        if not settings.enabled or not settings.detection_enabled:
            return None
        try:
            backend = await self._backend_for(settings)
        except Exception as exc:
            logger.warning("AI: backend unavailable (%s) - treating as plain motion", exc)
            return None
        scheduler.set_slots(getattr(backend, "slots", 1))
        floor = min(LOW_CONFIDENCE, settings.detection_confidence / 100.0)

        async def run():
            return await backend.detect_frame(frame, floor)

        objects = await scheduler.submit(camera_id, priority, run)
        if objects is SKIPPED:
            return objects
        await self._track_health(objects is not None, settings)
        if objects is None:
            return None
        wanted = self._wanted(settings)
        return [o for o in objects if not wanted or o.label.lower() in wanted]

    OUTAGE_AFTER = 120.0  # failing this long, continuously, = tell the user

    async def _track_health(self, ok: bool, settings) -> None:
        """One event when AI detection stops working (alerts then fall back to
        plain motion, and rules treat events as possibly a person), and one
        when it's back - not one per failed check."""
        now = time.monotonic()
        if ok:
            self._fail_since = None
            if self._outage_reported:
                self._outage_reported = False
                await self._notify_outage("AI object detection is working again.")
            return
        if self._fail_since is None:
            self._fail_since = now
        elif not self._outage_reported and now - self._fail_since > self.OUTAGE_AFTER:
            self._outage_reported = True
            where = (f"the AI service at {settings.remote_url}" if settings.backend == "remote"
                     else "the AI model on this NVR")
            await self._notify_outage(
                f"AI object detection has not been working for {int(self.OUTAGE_AFTER // 60)} minutes ({where}). "
                "Motion is still recorded and alerted, but not checked for people/vehicles - "
                "check Settings -> AI -> 'Check it works'."
            )

    @staticmethod
    async def _notify_outage(message: str) -> None:
        try:
            from app.services.events import emit_event

            await emit_event(None, "system", message)
        except Exception:
            logger.exception("Could not record AI health change")

    async def store_detections(
        self,
        camera_id: int,
        objects: list[DetectedObject],
        jpeg: bytes,
        description: str = "",
        event_id: int | None = None,
        recording_id: int | None = None,
        seen_at: datetime | None = None,
        extra: dict | None = None,
    ) -> list[int]:
        """`extra`: further Detection columns shared by these rows (track_uid,
        first_seen, last_seen, stationary_seconds, zones)."""
        return await self._store(camera_id, objects, jpeg, description, event_id, recording_id, seen_at, extra)

    def write_snapshot(self, camera_id: int, jpeg: bytes) -> str | None:
        return self._write_snapshot(camera_id, jpeg)

    async def _store(
        self,
        camera_id: int,
        objects: list[DetectedObject],
        jpeg: bytes,
        description: str = "",
        event_id: int | None = None,
        recording_id: int | None = None,
        seen_at: datetime | None = None,
        extra: dict | None = None,
    ) -> list[int]:
        snapshot_path = await asyncio.to_thread(self._write_snapshot, camera_id, jpeg)
        now = seen_at or datetime.now(timezone.utc)
        try:
            async with AsyncSessionLocal() as db:
                rows = [
                    Detection(
                        camera_id=camera_id,
                        label=obj.label,
                        confidence=int(round(obj.confidence * 100)),
                        bbox_x=obj.x,
                        bbox_y=obj.y,
                        bbox_w=obj.w,
                        bbox_h=obj.h,
                        # The sentence describes the frame, so every object in
                        # it shares the same one.
                        description=description or None,
                        snapshot_path=snapshot_path,
                        event_id=event_id,
                        recording_id=recording_id,
                        created_at=now,
                        **(extra or {}),
                    )
                    for obj in objects
                ]
                db.add_all(rows)
                await db.commit()
                return [r.id for r in rows]
        except Exception:
            logger.exception("AI: could not store detections for camera %s", camera_id)
            return []

    @staticmethod
    def _write_snapshot(camera_id: int, jpeg: bytes) -> str | None:
        """One JPEG per motion event (not per object) - all objects in a frame
        share it. Lives in the cache tier so the existing storage plumbing owns
        the disk, and is pruned alongside its detection rows by retention."""
        try:
            from app.services.storage_manager import storage_manager

            directory = os.path.join(storage_manager.cache_dir(), "detections", str(camera_id))
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}.jpg")
            with open(path, "wb") as f:
                f.write(jpeg)
            return path
        except Exception:
            logger.exception("AI: could not write detection snapshot")
            return None

    async def health(self) -> tuple[bool, str, str]:
        settings = await self._load_settings()
        if settings is None:
            return False, "AI settings row missing", "none"
        backend = await self._backend_for(settings)
        ok, detail = await backend.health()
        return ok, detail, settings.backend


ai_pipeline = AIPipeline()
