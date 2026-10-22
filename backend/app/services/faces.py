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
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import cv2
import numpy as np
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.person import FaceSample, Person
from app.services.ai.types import MODEL_DIR
from app.vision import models as model_registry
from app.vision.face import MIN_FACE_PX, Face, cosine

logger = logging.getLogger(__name__)

MIN_QUALITY = 0.35
LEARN_MIN_QUALITY = 0.55  # learning by itself: only sharp, frontal faces
LEARN_PER_DAY = 5
LEARN_MAX = 40  # learned sightings kept per person (the oldest are replaced)
# Faces are searched down to SEARCH_MIN_FACE px so the ones just too small to
# use can be counted (and explained on the People page) instead of vanishing.
SEARCH_MIN_FACE = 16
MARGIN = 0.06
CLEAR_EXTRA = 0.08  # this far above the threshold, one sighting is enough

# Per recogniser: the Settings slider (1-99) maps to a cosine threshold of
# base..base+span, and "possibly X" starts at `probable`. SFace's default
# (60 -> 0.45) is stricter than its LFW figure (0.363) on purpose: CCTV faces
# are smaller, blurrier and more often at an angle than benchmark photos.
# AdaFace scores different people lower (near 0), so its bar sits lower
# (60 -> 0.38, the usual AdaFace operating point being ~0.4).
MODEL_PARAMS = {
    "sface": {"base": 0.30, "span": 0.25, "probable": 0.36},
    "adaface": {"base": 0.20, "span": 0.30, "probable": 0.26},
}
PROBABLE_FLOOR = MODEL_PARAMS["sface"]["probable"]


def active_model(settings) -> str:
    """The recogniser in use: AdaFace only when chosen *and* downloaded."""
    chosen = getattr(settings, "face_model", "sface") or "sface"
    if chosen == "adaface" and model_registry.present("adaface", MODEL_DIR):
        return "adaface"
    return "sface"


def threshold_for(settings, model: str | None = None) -> float:
    p = MODEL_PARAMS[model or active_model(settings)]
    v = getattr(settings, "face_threshold", 60) or 60
    return p["base"] + (max(1, min(99, v)) / 100.0) * p["span"]


def sample_vector(sample, model: str) -> np.ndarray | None:
    raw = sample.embedding_ada if model == "adaface" else sample.embedding
    return np.frombuffer(raw, dtype=np.float32) if raw else None


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
        self._ada = None
        self._model = "sface"
        self.backfill_state = {"running": False, "done": 0, "total": 0}
        self._gallery: list[tuple[int, np.ndarray]] = []
        self._people: dict[int, Person] = {}
        self._loaded = False
        self._lock = asyncio.Lock()
        self._stats: dict[int, dict] = {}
        self._stats_loaded = False
        self._stats_saved = 0.0

    # ── gallery ──────────────────────────────────────────────────────────────

    async def reload(self) -> None:
        from app.models.ai_settings import AISettings

        async with AsyncSessionLocal() as db:
            settings = await db.get(AISettings, 1)
            people = (await db.execute(select(Person))).scalars().all()
            samples = (await db.execute(select(FaceSample).where(FaceSample.person_id.is_not(None)))).scalars().all()
            for p in people:
                db.expunge(p)
        self._model = active_model(settings)
        self._people = {p.id: p for p in people}
        vectors = [(s.person_id, sample_vector(s, self._model)) for s in samples]
        self._gallery = [(pid, v) for pid, v in vectors if v is not None]
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
        if top >= MODEL_PARAMS[self._model]["probable"] and person is not None:
            return Match(pid, person.name, person.note, person.category, top, "probable")
        return Match(None, None, None, None, top, "unknown")

    def closest(self, embedding: np.ndarray) -> tuple[int | None, float]:
        """The enrolled person this face is most similar to, however weakly -
        shown on "Who is this?" so a near miss ("41%, 45% needed") is visible
        and one click teaches the NVR what that person looks like on camera."""
        best_pid, best = None, -1.0
        for pid, emb in self._gallery:
            score = cosine(embedding, emb)
            if score > best:
                best_pid, best = pid, score
        return best_pid, best

    def person(self, person_id: int) -> Person | None:
        return self._people.get(person_id)

    # ── learning by itself ───────────────────────────────────────────────────

    async def learnable(self, settings, identity: dict | None, face: Face) -> int | None:
        """The person to file this camera sighting under automatically, or None
        to leave it for "Who is this?".

        Phone photos teach the recogniser what someone looks like up close;
        only camera sightings teach it how they look from *this* camera (from
        above, smaller, in this light). So confident recognitions are added
        to the person's photos by themselves and recognition gets better day
        by day. Guarded against learning the wrong face: the event already
        recognised them (2 agreeing sightings or 1 clear one), this face on
        its own is a clear match with a margin over everyone else, it's a
        sharp, frontal face, only Family and Familiar people, at most
        LEARN_PER_DAY a day and LEARN_MAX in all (oldest replaced)."""
        if not identity or not identity.get("id") or face is None or face.embedding is None:
            return None
        pid = identity["id"]
        person = self._people.get(pid)
        if person is None or person.category not in ("household", "familiar"):
            return None
        if face.quality < LEARN_MIN_QUALITY:
            return None
        vec = await self.vector(face)
        if vec is None:
            return None
        m = self.match(vec, threshold_for(settings, self._model))
        if m.band != "known" or m.person_id != pid:
            return None
        from sqlalchemy import func

        day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        async with AsyncSessionLocal() as db:
            today = (await db.execute(
                select(func.count()).select_from(FaceSample)
                .where(FaceSample.person_id == pid, FaceSample.source == "auto", FaceSample.created_at >= day_start)
            )).scalar() or 0
            if today >= LEARN_PER_DAY:
                return None
            learned = (await db.execute(
                select(FaceSample).where(FaceSample.person_id == pid, FaceSample.source == "auto")
                .order_by(FaceSample.created_at)
            )).scalars().all()
            for old in learned[: max(0, len(learned) - LEARN_MAX + 1)]:
                if old.crop_path and os.path.exists(old.crop_path):
                    try:
                        os.remove(old.crop_path)
                    except OSError:
                        pass
                await db.delete(old)
            await db.commit()
        return pid

    # ── finding faces ────────────────────────────────────────────────────────

    def _local_engine(self):
        if self._engine is None:
            from app.vision.face import FaceEngine

            if not (model_registry.present("yunet", MODEL_DIR) and model_registry.present("sface", MODEL_DIR)):
                raise FileNotFoundError("face models not downloaded")
            self._engine = FaceEngine(model_registry.path_for("yunet", MODEL_DIR),
                                      model_registry.path_for("sface", MODEL_DIR))
        return self._engine

    # ── AdaFace (optional second recogniser) ─────────────────────────────────

    def _ada_engine(self):
        if self._ada is None:
            from app.vision.face import AdaFace

            if not model_registry.present("adaface", MODEL_DIR):
                return None
            self._ada = AdaFace(model_registry.path_for("adaface", MODEL_DIR), threads=2)
        return self._ada

    def _ada_vector_sync(self, crop: np.ndarray | None, aligned: bool) -> np.ndarray | None:
        from app.vision.face import realign

        ada = self._ada_engine()
        if ada is None or crop is None:
            return None
        if not aligned:
            try:
                crop = realign(self._local_engine(), crop)
            except FileNotFoundError:
                return None
            if crop is None:
                return None
        return ada.embed(crop)

    async def ada_vector(self, face: Face) -> np.ndarray | None:
        if face.embedding_ada is None:
            face.embedding_ada = await asyncio.to_thread(self._ada_vector_sync, face.crop, face.aligned)
        return face.embedding_ada

    async def vector(self, face: Face) -> np.ndarray | None:
        """The face's embedding for the recogniser in use."""
        if self._model == "adaface":
            return await self.ada_vector(face)
        return np.asarray(face.embedding, dtype=np.float32) if face.embedding is not None else None

    async def backfill_ada(self) -> None:
        """Give every saved face an AdaFace embedding too (from its saved
        picture, re-aligned), so the two recognisers can be compared on the
        user's own faces. Runs in the background after the download."""
        if self.backfill_state["running"] or self._ada_engine() is None:
            return
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(FaceSample.id, FaceSample.crop_path).where(FaceSample.embedding_ada.is_(None))
            )).all()
        self.backfill_state = {"running": True, "done": 0, "total": len(rows)}
        try:
            for sid, path in rows:
                crop = await asyncio.to_thread(cv2.imread, path) if path and os.path.exists(path) else None
                vec = await asyncio.to_thread(self._ada_vector_sync, crop, False) if crop is not None else None
                if vec is not None:
                    async with AsyncSessionLocal() as db:
                        row = await db.get(FaceSample, sid)
                        if row is not None:
                            row.embedding_ada = vec.astype(np.float32).tobytes()
                            await db.commit()
                self.backfill_state["done"] += 1
        except Exception:
            logger.exception("Preparing AdaFace embeddings failed")
        finally:
            self.backfill_state["running"] = False
        await self.reload()

    async def find(self, settings, frame: np.ndarray, boxes: list | None, keep_crops: bool = True,
                   min_face: int = MIN_FACE_PX) -> list[Face]:
        if settings is not None and settings.backend == "remote" and settings.remote_url:
            try:
                return await self._find_remote(settings, frame, boxes, min_face)
            except Exception as exc:
                logger.warning("Face search on the AI worker failed (%s) - trying locally", exc)
        engine = await asyncio.to_thread(self._local_engine)
        return await asyncio.to_thread(engine.find, frame, boxes, min_face, keep_crops)

    async def _find_remote(self, settings, frame: np.ndarray, boxes: list | None,
                           min_face: int = MIN_FACE_PX) -> list[Face]:
        import httpx

        ok, jpeg = await asyncio.to_thread(cv2.imencode, ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        headers = {"X-API-Key": settings.remote_api_key} if settings.remote_api_key else {}
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            resp = await client.post(
                f"{settings.remote_url.rstrip('/')}/v2/faces",
                files={"image": ("frame.jpg", jpeg.tobytes(), "image/jpeg")},
                data={"boxes": json.dumps(boxes) if boxes else "", "min_face": str(min_face), "crops": "true"},
            )
            resp.raise_for_status()
        out = []
        H, W = frame.shape[:2]
        for f in resp.json().get("faces", []):
            face = Face(f["x"], f["y"], f["w"], f["h"], f["score"], f["quality"],
                        np.asarray(f["embedding"], dtype=np.float32))
            if f.get("crop"):
                # The worker's aligned face (newer workers).
                import base64

                buf = np.frombuffer(base64.b64decode(f["crop"]), np.uint8)
                face.crop = cv2.imdecode(buf, cv2.IMREAD_COLOR)
                face.aligned = face.crop is not None
            if face.crop is None:
                x1, y1 = int(face.x * W), int(face.y * H)
                x2, y2 = int((face.x + face.w) * W), int((face.y + face.h) * H)
                crop = frame[max(0, y1):y2, max(0, x1):x2]
                face.crop = cv2.resize(crop, (112, 112)) if crop.size else None
            out.append(face)
        return out

    async def recognise(self, settings, frame: np.ndarray, person_boxes: list,
                        camera_id: int | None = None) -> list[tuple[int, Face, Match]]:
        """For each person box (index), its best usable face and who it is.
        Every look is counted per camera with the reason when no face was
        usable (see capture_stats)."""
        await self._ensure_loaded()
        faces = await self.find(settings, frame, person_boxes, min_face=SEARCH_MIN_FACE)
        thr = threshold_for(settings, self._model)
        H, W = frame.shape[:2]
        out = []
        for i, (bx, by, bw, bh) in enumerate(person_boxes):
            inside = [f for f in faces if bx - bw * 0.2 <= f.x + f.w / 2 <= bx + bw * 1.2
                      and by - bh * 0.1 <= f.y + f.h / 2 <= by + bh * 0.6]
            if not inside:
                self._count(camera_id, "no_face", W)
                continue
            f = max(inside, key=lambda x: x.quality)
            if min(f.w * W, f.h * H) < MIN_FACE_PX:
                self._count(camera_id, "too_small", W, min(f.w * W, f.h * H))
                continue
            if f.quality < MIN_QUALITY:
                self._count(camera_id, "low_quality", W, min(f.w * W, f.h * H))
                continue
            vec = await self.vector(f)
            if vec is None:
                self._count(camera_id, "low_quality", W, min(f.w * W, f.h * H))
                continue
            self._count(camera_id, "usable", W, min(f.w * W, f.h * H))
            out.append((i, f, self.match(vec, thr)))
        return out

    # ── capture statistics (People page: "why no faces?") ───────────────────

    def _stats_path(self) -> str:
        from app.services.storage_manager import storage_manager

        return os.path.join(storage_manager.cache_dir(), "face_capture_stats.json")

    def _load_stats(self) -> None:
        """Today's counts survive a restart (an update mid-day would otherwise
        wipe the answer to "why wasn't X captured this morning?")."""
        if self._stats_loaded:
            return
        self._stats_loaded = True
        try:
            with open(self._stats_path(), encoding="utf-8") as f:
                self._stats = {int(k): v for k, v in json.load(f).items()}
        except (OSError, ValueError):
            pass

    def _save_stats(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._stats_saved < 20:
            return
        self._stats_saved = now
        try:
            tmp = self._stats_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._stats, f)
            os.replace(tmp, self._stats_path())
        except OSError:
            logger.debug("Could not save face capture stats", exc_info=True)

    def _count(self, camera_id: int | None, outcome: str, frame_width: int, face_px: float | None = None) -> None:
        if camera_id is None:
            return
        self._load_stats()
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        st = self._stats.get(camera_id)
        if st is None or st["day"] != day:
            st = self._stats[camera_id] = {"day": day, "no_face": 0, "too_small": 0, "low_quality": 0,
                                           "usable": 0, "face_px": [], "frame_width": frame_width}
        st[outcome] += 1
        st["frame_width"] = frame_width
        if face_px is not None:
            st["face_px"] = (st["face_px"] + [round(face_px)])[-200:]
        self._save_stats()

    def capture_stats(self) -> dict[int, dict]:
        self._load_stats()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out = {}
        for cam, st in self._stats.items():
            if st.get("day") != today:
                continue
            px = sorted(st["face_px"])
            out[cam] = {k: st[k] for k in ("day", "no_face", "too_small", "low_quality", "usable", "frame_width")}
            out[cam]["checked"] = st["no_face"] + st["too_small"] + st["low_quality"] + st["usable"]
            out[cam]["median_face_px"] = px[len(px) // 2] if px else None
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
                          track_uid: str | None, source: str = "capture", seen_at: float | None = None) -> int:
        path = await asyncio.to_thread(self._write_crop, face.crop)
        seen = datetime.fromtimestamp(seen_at, timezone.utc).replace(tzinfo=None) if seen_at else None
        ada = await self.ada_vector(face) if model_registry.present("adaface", MODEL_DIR) else None
        async with AsyncSessionLocal() as db:
            row = FaceSample(person_id=person_id, camera_id=camera_id,
                             embedding=np.asarray(face.embedding, dtype=np.float32).tobytes(), crop_path=path,
                             quality=face.quality, score=score, track_uid=track_uid, source=source, seen_at=seen,
                             embedding_ada=ada.astype(np.float32).tobytes() if ada is not None else None)
            db.add(row)
            await db.commit()
            await db.refresh(row)
        if person_id is not None:
            await self.reload()
        return row.id


face_service = FaceService()


# ── people page helpers ──────────────────────────────────────────────────────

# "Who is this?" groups sightings this far below the recognition threshold:
# grouping is only a suggestion the user confirms, and the same person seen
# from different angles across visits rarely scores as high as a match needs.
GROUP_SLACK = 0.06


def recognition_strength(qualities: list[float], sources: list[str] | None = None) -> tuple[str, str]:
    """How reliably someone will be recognised, in words, from their samples.

    Phone photos alone are weak: a face seen by a CCTV camera (from above,
    small, softer, different light) looks quite different to the recogniser,
    so it needs a few sightings from the cameras themselves."""
    n = len(qualities)
    good = sum(1 for q in qualities if q >= 0.6)
    cameras = sum(1 for s in (sources or []) if s != "upload") if sources is not None else n
    if n < 3:
        return "weak", f"add {3 - n} more photo(s) - front and slightly turned"
    if cameras == 0:
        return "weak", "only phone photos - confirm 2-3 of their sightings under Who is this?"
    if cameras < 3:
        return "good", f"confirm {3 - cameras} more camera sighting(s) under Who is this?"
    if n >= 6 and good >= 4:
        return "strong", ""
    return "good", "a few more clear sightings will make it stronger"


@dataclass
class FaceGroup:
    ids: list[int]
    centroid: np.ndarray
    tracks: set


def group_faces(samples: list, threshold: float) -> list[FaceGroup]:
    """Groups unnamed face captures that are (very likely) the same person, so
    "Who is this?" asks once per person instead of once per sighting.

    `samples`: objects with .id, .track_uid and .embedding (float32 bytes),
    oldest first. Captures from the same track always group together;
    otherwise a capture joins the group whose average face it matches at the
    recognition threshold - the same bar as recognising an enrolled person.
    """
    groups: list[FaceGroup] = []
    for s in samples:
        emb = np.frombuffer(s.embedding, dtype=np.float32)
        target = None
        if s.track_uid:
            target = next((g for g in groups if s.track_uid in g.tracks), None)
        if target is None:
            best, best_score = None, threshold
            for g in groups:
                score = cosine(emb, g.centroid)
                if score >= best_score:
                    best, best_score = g, score
            target = best
        if target is None:
            groups.append(FaceGroup([s.id], emb.copy(), {s.track_uid} if s.track_uid else set()))
            continue
        k = len(target.ids)
        mean = target.centroid * k + emb
        target.centroid = (mean / (np.linalg.norm(mean) or 1.0)).astype(np.float32)
        target.ids.append(s.id)
        if s.track_uid:
            target.tracks.add(s.track_uid)
    return groups


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
