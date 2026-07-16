import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.db.session import AsyncSessionLocal
from app.models.ai_settings import AISettings
from app.models.detection import Detection
from app.services.ai.backends import InferenceBackend, build_backend
from app.services.ai.types import DetectedObject
from app.services.frame_bus import frame_bus

logger = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    objects: list[DetectedObject] = field(default_factory=list)
    # True when objects-only mode is on and nothing of interest was in frame -
    # the caller should stay silent instead of alerting.
    suppressed: bool = False
    message: str = ""
    description: str = ""  # Tier 3 sentence, when a VLM is configured
    detection_ids: list[int] = field(default_factory=list)


def _summarise(objects: list[DetectedObject], camera_name: str) -> str:
    """'Person detected on Front Door' beats 'Motion detected on Front Door',
    and 'Person + car' beats either. Counts, because '3 people' is a
    materially different event from '1 person'."""
    counts: dict[str, int] = {}
    for obj in objects:
        counts[obj.label] = counts.get(obj.label, 0) + 1
    parts = [(f"{n} {label}s" if n > 1 else label.capitalize()) for label, n in sorted(counts.items())]
    return f"{' + '.join(parts)} detected on '{camera_name}'"


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

    async def analyze_motion(self, camera_id: int, camera_name: str) -> AnalysisResult | None:
        """Returns None when AI is off or unusable - caller then behaves exactly
        as it did before AI existed."""
        settings = await self._load_settings()
        if settings is None or not settings.enabled or not settings.detection_enabled:
            return None

        jpeg = frame_bus.get_latest(camera_id)
        if not jpeg:
            return None  # nothing decoded yet (camera just came up)

        try:
            backend = await self._backend_for(settings)
            objects = await backend.detect(jpeg, settings.detection_confidence / 100.0)
        except Exception as exc:
            # Warn, don't raise: a broken AI config must not silence motion.
            logger.warning("AI: detection failed on camera %s (%s) - falling back to plain motion", camera_id, exc)
            return None

        wanted = self._wanted(settings)
        if wanted:
            objects = [o for o in objects if o.label.lower() in wanted]

        if not objects and settings.alert_on_objects_only:
            # The whole point of Tier 1: motion with no object of interest is
            # a tree/shadow/rain/insect, so say nothing.
            return AnalysisResult(objects=[], suppressed=True)
        if not objects:
            return AnalysisResult(objects=[], message="")

        # Tier 3: one sentence describing the scene. Only reached for frames
        # that already passed the object filter, which is what keeps this
        # affordable - a handful of calls a day instead of one per twitch of a
        # tree. Never fatal: a description is a nice-to-have on top of a
        # detection that already stands on its own.
        description = ""
        if settings.vlm_enabled:
            try:
                from app.services.ai.vlm import describe_frame

                description = await describe_frame(
                    settings, jpeg, [o.label for o in objects], camera_name
                )
            except Exception as exc:
                logger.warning("AI: VLM description failed for camera %s (%s)", camera_id, exc)

        detection_ids = await self._store(camera_id, objects, jpeg, description)

        message = description or _summarise(objects, camera_name)
        return AnalysisResult(objects=objects, message=message, description=description, detection_ids=detection_ids)

    async def _store(
        self, camera_id: int, objects: list[DetectedObject], jpeg: bytes, description: str = ""
    ) -> list[int]:
        snapshot_path = await asyncio.to_thread(self._write_snapshot, camera_id, jpeg)
        now = datetime.now(timezone.utc)
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
                        created_at=now,
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
