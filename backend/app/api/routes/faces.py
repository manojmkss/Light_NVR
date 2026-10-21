"""People & faces (opt-in face recognition).

Everything here requires the privacy acknowledgement (Settings -> AI) and an
admin. Deleting a person deletes all their samples; "forget all faces"
deletes every face sample and person.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_admin, require_admin_flexible
from app.db.session import AsyncSessionLocal, get_db
from app.models.ai_settings import AISettings
from app.models.detection import Detection
from app.models.person import FaceSample, Person
from app.models.user import User
from app.schemas.common import UtcDatetime
from app.services.faces import face_service, threshold_for

router = APIRouter(prefix="/api/faces", tags=["faces"])

CATEGORIES = ("household", "staff", "visitor", "watchlist")
MAX_PHOTO_BYTES = 15 * 1024 * 1024


class PersonIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    note: str | None = Field(None, max_length=256)
    category: str = "visitor"
    arrival_alert: bool = True
    arrival_cooldown_s: int = Field(1800, ge=0, le=86400)


class PersonOut(PersonIn):
    id: int
    samples: int = 0
    created_at: UtcDatetime


class SampleOut(BaseModel):
    id: int
    person_id: int | None
    camera_id: int | None
    quality: float
    score: float | None
    source: str
    created_at: UtcDatetime
    suggestion: dict | None = None  # {"person_id", "name", "score"} for unassigned captures


async def _require_faces(db: AsyncSession) -> AISettings:
    s = await db.get(AISettings, 1)
    if s is None or not s.privacy_ack:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Face recognition needs the privacy acknowledgement in Settings -> AI first",
        )
    return s


def _check_category(c: str) -> None:
    if c not in CATEGORIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"category must be one of {CATEGORIES}")


@router.get("/models")
async def face_models(_: User = Depends(require_admin)):
    from app.services.ai.types import MODEL_DIR
    from app.vision import models as reg

    return {"models": [m for m in reg.status(MODEL_DIR) if m["name"] in ("yunet", "sface")]}


@router.post("/models/download")
async def download_face_models(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    """Explicit, admin-triggered download of the two face models (~37 MB,
    MIT / Apache-2.0), verified by checksum."""
    await _require_faces(db)
    from app.services.ai.types import MODEL_DIR
    from app.vision import models as reg

    try:
        for name in ("yunet", "sface"):
            await asyncio.to_thread(reg.download, name, MODEL_DIR)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Download failed: {exc}") from exc
    face_service._engine = None
    return {"models": [m for m in reg.status(MODEL_DIR) if m["name"] in ("yunet", "sface")]}


@router.get("/people", response_model=list[PersonOut])
async def list_people(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    people = (await db.execute(select(Person).order_by(Person.name))).scalars().all()
    counts = dict((await db.execute(
        select(FaceSample.person_id, func.count()).where(FaceSample.person_id.is_not(None)).group_by(FaceSample.person_id)
    )).all())
    return [PersonOut(id=p.id, name=p.name, note=p.note, category=p.category, arrival_alert=p.arrival_alert,
                      arrival_cooldown_s=p.arrival_cooldown_s, samples=counts.get(p.id, 0), created_at=p.created_at)
            for p in people]


@router.post("/people", response_model=PersonOut, status_code=status.HTTP_201_CREATED)
async def create_person(payload: PersonIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    await _require_faces(db)
    _check_category(payload.category)
    row = Person(**payload.model_dump())
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await face_service.reload()
    return PersonOut(**payload.model_dump(), id=row.id, samples=0, created_at=row.created_at)


@router.put("/people/{person_id}", response_model=PersonOut)
async def update_person(person_id: int, payload: PersonIn, db: AsyncSession = Depends(get_db),
                        _: User = Depends(require_admin)):
    _check_category(payload.category)
    row = await db.get(Person, person_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Person not found")
    for k, v in payload.model_dump().items():
        setattr(row, k, v)
    await db.commit()
    await face_service.reload()
    n = (await db.execute(select(func.count()).select_from(FaceSample).where(FaceSample.person_id == person_id))).scalar()
    return PersonOut(**payload.model_dump(), id=row.id, samples=n or 0, created_at=row.created_at)


async def _purge_samples(db: AsyncSession, where) -> int:
    rows = (await db.execute(select(FaceSample).where(where))).scalars().all()
    for r in rows:
        if r.crop_path and os.path.exists(r.crop_path):
            try:
                os.remove(r.crop_path)
            except OSError:
                pass
        await db.delete(r)
    return len(rows)


@router.delete("/people/{person_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_person(person_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)):
    """Deletes the person, every face sample and crop of them, and forgets
    them on past detections."""
    row = await db.get(Person, person_id)
    if row is None:
        return
    await _purge_samples(db, FaceSample.person_id == person_id)
    await db.execute(update(Detection).where(Detection.person_id == person_id).values(person_id=None, face_score=None))
    await db.delete(row)
    await db.commit()
    await face_service.reload()
    from app.services.events import emit_event

    await emit_event(None, "audit", f"Face data for '{row.name}' deleted by {user.username}", notify=False)


@router.post("/forget-all")
async def forget_all(db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)):
    n = await _purge_samples(db, FaceSample.id.is_not(None))
    await db.execute(update(Detection).values(person_id=None, face_score=None))
    await db.execute(delete(Person))
    await db.commit()
    await face_service.reload()
    from app.services.events import emit_event

    await emit_event(None, "audit", f"All face data ({n} samples) deleted by {user.username}", notify=False)
    return {"deleted_samples": n}


@router.get("/samples", response_model=list[SampleOut])
async def list_samples(person_id: int | None = None, unassigned: bool = False, limit: int = 60,
                       db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    s = await db.get(AISettings, 1)
    stmt = select(FaceSample).order_by(FaceSample.created_at.desc()).limit(min(limit, 200))
    if person_id is not None:
        stmt = stmt.where(FaceSample.person_id == person_id)
    elif unassigned:
        stmt = stmt.where(FaceSample.person_id.is_(None))
    rows = (await db.execute(stmt)).scalars().all()
    await face_service._ensure_loaded()
    thr = threshold_for(s)
    out = []
    for r in rows:
        sug = None
        if r.person_id is None:
            m = face_service.match(np.frombuffer(r.embedding, dtype=np.float32), thr)
            if m.person_id is not None:
                sug = {"person_id": m.person_id, "name": m.name, "score": round(m.score, 3), "band": m.band}
        out.append(SampleOut(id=r.id, person_id=r.person_id, camera_id=r.camera_id, quality=r.quality, score=r.score,
                             source=r.source, created_at=r.created_at, suggestion=sug))
    return out


@router.get("/samples/{sample_id}/crop.jpg")
async def sample_crop(sample_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin_flexible)):
    # Admin-only like the rest of face data: viewers never see face crops.
    r = await db.get(FaceSample, sample_id)
    if r is None or not r.crop_path or not os.path.exists(r.crop_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No crop")
    return FileResponse(r.crop_path, media_type="image/jpeg")


class AssignIn(BaseModel):
    person_id: int | None  # None = un-assign


@router.post("/samples/{sample_id}/assign")
async def assign_sample(sample_id: int, payload: AssignIn, db: AsyncSession = Depends(get_db),
                        _: User = Depends(require_admin)):
    """'Who is this?' - add a captured face to a person's gallery (or remove it)."""
    await _require_faces(db)
    r = await db.get(FaceSample, sample_id)
    if r is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sample not found")
    if payload.person_id is not None and await db.get(Person, payload.person_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Person not found")
    r.person_id = payload.person_id
    await db.commit()
    await face_service.reload()
    return {"ok": True}


@router.delete("/samples/{sample_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sample(sample_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    await _purge_samples(db, FaceSample.id == sample_id)
    await db.commit()
    await face_service.reload()


@router.post("/people/{person_id}/photos")
async def upload_photos(person_id: int, files: list[UploadFile] = File(...), db: AsyncSession = Depends(get_db),
                        _: User = Depends(require_admin)):
    """Enrol from photos: each must contain exactly one clear face."""
    s = await _require_faces(db)
    if await db.get(Person, person_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Person not found")
    results = []
    for f in files[:10]:
        raw = await f.read(MAX_PHOTO_BYTES + 1)
        if len(raw) > MAX_PHOTO_BYTES:
            results.append({"file": f.filename, "ok": False, "reason": "file larger than 15 MB"})
            continue
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR) if raw else None
        if img is None:
            results.append({"file": f.filename, "ok": False, "reason": "not an image"})
            continue
        try:
            faces = await face_service.find(s, img, None)
        except FileNotFoundError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Face models not downloaded yet - use 'Download face models' first")
        if len(faces) != 1:
            results.append({"file": f.filename, "ok": False,
                            "reason": "no face found" if not faces else f"{len(faces)} faces - use a photo of one person"})
            continue
        face = faces[0]
        if face.quality < 0.5:
            results.append({"file": f.filename, "ok": False, "reason": f"face too small/blurred/turned (quality {face.quality:.2f})"})
            continue
        sid = await face_service.save_sample(face, person_id, None, None, None, source="upload")
        results.append({"file": f.filename, "ok": True, "sample_id": sid, "quality": round(face.quality, 2)})
    return {"results": results}


async def prune_unknown_faces() -> int:
    """Retention: unnamed captures older than face_unknown_retention_days."""
    async with AsyncSessionLocal() as db:
        s = await db.get(AISettings, 1)
        days = (s.face_unknown_retention_days if s and s.face_unknown_retention_days else 7)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        n = await _purge_samples(db, (FaceSample.person_id.is_(None)) & (FaceSample.created_at < cutoff))
        await db.commit()
    return n
