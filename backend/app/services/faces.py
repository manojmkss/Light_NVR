"""Face recognition for enrolled people - local only, opt-in.

Pipeline (per person track, during a motion event):
  person box -> face found in its upper half (YuNet) -> quality gate ->
  128-d embedding (SFace) -> compared with every enrolled person's samples.

Decisions are deliberately conservative - a wrong "it's Ramesh" is worse than
"unknown person":
- a match needs cosine >= threshold AND a margin over the next-best person;
- a "probable" band below the threshold is shown as "possibly X" and never
  counts as recognised;
- a track is recognised after 2 agreeing sightings, or 1 clearly above the
  threshold;
- the gallery only grows when the user confirms ("Add to Ramesh?"), never on
  its own (that's how galleries drift and get poisoned).

Faces run in the AI worker when one is configured, else in this process.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import cv2
import numpy as np
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.person import FaceSample, Person
from app.services.ai.types import MODEL_DIR
from app.vision import models as model_registry
from app.vision.face import Face, cosine

logger = logging.getLogger(__name__)

MIN_QUALITY = 0.35
MARGIN = 0.06
CLEAR_EXTRA = 0.08  # this far above the threshold, one sighting is enough
PROBABLE_FLOOR = 0.36


def threshold_for(settings) -> float:
    """Settings slider 1-99 -> cosine threshold 0.30-0.55 (default 60 ~ 0.45).
    Stricter than SFace's LFW figure (0.363) on purpose: CCTV faces are
    smaller, blurrier and more often at an angle than benchmark photos."""
    v = getattr(settings, "face_threshold", 60) or 60
    return 0.30 + (max(1, min(99, v)) / 100.0) * 0.25


@dataclass
class Match:
    person_id: int | None
    name: str | None
    note: str | None
    category: str | None
    score: float
    band: str  # known | probable | unknown


def faces_enabled(settings) -> bool:
    return bool(settings and settings.enabled and getattr(settings, "face_enabled", False)
                and getattr(settings, "privacy_ack", False))


class FaceService:
    def __init__(self) -> None:
        self._engine = None
        self._gallery: list[tuple[int, np.ndarray]] = []
        self._people: dict[int, Person] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    # ── gallery ──────────────────────────────────────────────────────────────

    async def reload(self) -> None:
        async with AsyncSessionLocal() as db:
            people = (await db.execute(select(Person))).scalars().all()
            samples = (await db.execute(select(FaceSample).where(FaceSample.person_id.is_not(None)))).scalars().all()
            for p in people:
                db.expunge(p)
        self._people = {p.id: p for p in people}
        self._gallery = [(s.person_id, np.frombuffer(s.embedding, dtype=np.float32)) for s in samples]
        self._loaded = True

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            await self.reload()

    def match(self, embedding: np.ndarray, threshold: float) -> Match:
        best: dict[int, float] = {}
        for pid, emb in self._gallery:
            best[pid] = max(best.get(pid, -1.0), cosine(embedding, emb))
        if not best:
            return Match(None, None, None, None, 0.0, "unknown")
        ranked = sorted(best.items(), key=lambda kv: -kv[1])
        pid, top = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else -1.0
        person = self._people.get(pid)
        if top >= threshold and top - second >= MARGIN and person is not None:
            return Match(pid, person.name, person.note, person.category, top, "known")
        if top >= PROBABLE_FLOOR and person is not None:
            return Match(pid, person.name, person.note, person.category, top, "probable")
        return Match(None, None, None, None, top, "unknown")

    def person(self, person_id: int) -> Person | None:
        return self._people.get(person_id)

    # ── finding faces ────────────────────────────────────────────────────────

    def _local_engine(self):
        if self._engine is None:
            from app.vision.face import FaceEngine

            if not (model_registry.present("yunet", MODEL_DIR) and model_registry.present("sface", MODEL_DIR)):
                raise FileNotFoundError("face models not downloaded")
            self._engine = FaceEngine(model_registry.path_for("yunet", MODEL_DIR),
                                      model_registry.path_for("sface", MODEL_DIR))
        return self._engine

    async def find(self, settings, frame: np.ndarray, boxes: list | None, keep_crops: bool = True) -> list[Face]:
        if settings is not None and settings.backend == "remote" and settings.remote_url:
            try:
                return await self._find_remote(settings, frame, boxes)
            except Exception as exc:
                logger.warning("Face search on the AI worker failed (%s) - trying locally", exc)
        engine = await asyncio.to_thread(self._local_engine)
        return await asyncio.to_thread(engine.find, frame, boxes, 40, keep_crops)

    async def _find_remote(self, settings, frame: np.ndarray, boxes: list | None) -> list[Face]:
        import httpx

        ok, jpeg = await asyncio.to_thread(cv2.imencode, ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        headers = {"X-API-Key": settings.remote_api_key} if settings.remote_api_key else {}
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            resp = await client.post(
                f"{settings.remote_url.rstrip('/')}/v2/faces",
                files={"image": ("frame.jpg", jpeg.tobytes(), "image/jpeg")},
                data={"boxes": json.dumps(boxes) if boxes else ""},
            )
            resp.raise_for_status()
        out = []
        H, W = frame.shape[:2]
        for f in resp.json().get("faces", []):
            face = Face(f["x"], f["y"], f["w"], f["h"], f["score"], f["quality"],
                        np.asarray(f["embedding"], dtype=np.float32))
            x1, y1 = int(face.x * W), int(face.y * H)
            x2, y2 = int((face.x + face.w) * W), int((face.y + face.h) * H)
            crop = frame[max(0, y1):y2, max(0, x1):x2]
            face.crop = cv2.resize(crop, (112, 112)) if crop.size else None
            out.append(face)
        return out

    async def recognise(self, settings, frame: np.ndarray, person_boxes: list) -> list[tuple[int, Face, Match]]:
        """For each person box (index), its best usable face and who it is."""
        await self._ensure_loaded()
        faces = await self.find(settings, frame, person_boxes)
        thr = threshold_for(settings)
        out = []
        for i, (bx, by, bw, bh) in enumerate(person_boxes):
            inside = [f for f in faces if bx - bw * 0.2 <= f.x + f.w / 2 <= bx + bw * 1.2
                      and by - bh * 0.1 <= f.y + f.h / 2 <= by + bh * 0.6]
            if not inside:
                continue
            f = max(inside, key=lambda x: x.quality)
            if f.quality < MIN_QUALITY:
                continue
            out.append((i, f, self.match(f.embedding, thr)))
        return out

    # ── storage ──────────────────────────────────────────────────────────────

    @staticmethod
    def _write_crop(crop: np.ndarray | None) -> str | None:
        if crop is None:
            return None
        from app.services.storage_manager import storage_manager

        directory = os.path.join(storage_manager.cache_dir(), "faces")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}.jpg")
        cv2.imwrite(path, crop)
        return path

    async def save_sample(self, face: Face, person_id: int | None, camera_id: int | None, score: float | None,
                          track_uid: str | None, source: str = "capture") -> int:
        path = await asyncio.to_thread(self._write_crop, face.crop)
        async with AsyncSessionLocal() as db:
            row = FaceSample(person_id=person_id, camera_id=camera_id,
                             embedding=np.asarray(face.embedding, dtype=np.float32).tobytes(), crop_path=path,
                             quality=face.quality, score=score, track_uid=track_uid, source=source)
            db.add(row)
            await db.commit()
            await db.refresh(row)
        if person_id is not None:
            await self.reload()
        return row.id


face_service = FaceService()


def _is_private_url(url: str) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return (host in ("localhost", "127.0.0.1", "ai-worker") or host.endswith(".local") or host.startswith(("10.", "192.168."))
            or any(host.startswith(f"172.{n}.") for n in range(16, 32)))


def vlm_is_cloud(settings) -> bool:
    provider = getattr(settings, "vlm_provider", "")
    if provider == "anthropic":
        return True
    url = getattr(settings, "vlm_url", "") or ""
    return provider == "openai_compatible" and bool(url) and not _is_private_url(url)


async def privacy_safe_jpeg(settings, frame: np.ndarray, quality: int = 90) -> bytes | None:
    """The picture to send to a vision model. With face recognition on and a
    cloud provider, faces are blurred first (or nothing is sent if they can't
    be found reliably) - recognised people's faces never leave the house."""
    if faces_enabled(settings) and vlm_is_cloud(settings):
        try:
            faces = await face_service.find(settings, frame, None, keep_crops=False)
        except Exception:
            return None
        frame = frame.copy()
        H, W = frame.shape[:2]
        for f in faces:
            x1, y1 = int(max(0, (f.x - f.w * 0.2) * W)), int(max(0, (f.y - f.h * 0.2) * H))
            x2, y2 = int(min(W, (f.x + f.w * 1.2) * W)), int(min(H, (f.y + f.h * 1.2) * H))
            if x2 > x1 and y2 > y1:
                frame[y1:y2, x1:x2] = cv2.GaussianBlur(frame[y1:y2, x1:x2], (0, 0), 25)
    ok, buf = await asyncio.to_thread(cv2.imencode, ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buf.tobytes() if ok else None
