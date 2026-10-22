"""People & faces (opt-in face recognition).

Everything here requires the privacy acknowledgement (Settings -> AI) and an
admin. Deleting a person deletes all their samples; "forget all faces"
deletes every face sample and person.
"""

import asyncio
import os
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_admin, require_admin_flexible
from app.db.session import AsyncSessionLocal, get_db
from app.models.ai_settings import AISettings
from app.models.detection import Detection
from app.models.person import FaceSample, Person
from app.models.user import User
from app.schemas.common import UtcDatetime, serialize_utc
from app.services.faces import (
    GROUP_SLACK,
    MARGIN,
    active_model,
    face_service,
    group_faces,
    recognition_strength,
    sample_vector,
    threshold_for,
)

router = APIRouter(prefix="/api/faces", tags=["faces"])

# Trust levels (UI: Family / Familiar / Stranger / Suspicious).
CATEGORIES = ("household", "familiar", "stranger", "watchlist")
LEGACY_CATEGORIES = {"staff": "familiar", "visitor": "familiar"}
NOTIFY = ("always", "away", "never")
# Strangers and suspicious people may stay unnamed: "Stranger #3".
AUTO_NAME = {"stranger": "Stranger", "watchlist": "Suspicious person"}
MAX_PHOTO_BYTES = 15 * 1024 * 1024


class PersonIn(BaseModel):
    name: str = Field("", max_length=128)
    note: str | None = Field(None, max_length=256)  # relation: "milkman", "daughter"...
    category: str = "familiar"
    notify: str | None = None  # always | away | never; None = derive from arrival_alert (older clients)
    arrival_alert: bool = True
    arrival_cooldown_s: int = Field(1800, ge=0, le=86400)


class PersonOut(PersonIn):
    id: int
    samples: int = 0
    strength: str = "weak"  # weak | good | strong
    strength_hint: str = ""
    cover_sample_id: int | None = None
    learned: int = 0  # camera sightings added by itself (confident recognitions)
    cover_camera_id: int | None = None  # where / when the cover photo was taken (None for uploads)
    cover_seen_at: UtcDatetime | None = None
    last_seen: UtcDatetime | None = None
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


async def _clean(db: AsyncSession, payload: PersonIn, person_id: int | None = None) -> dict:
    """Validates a person and fills in what the table leaves implicit."""
    data = payload.model_dump()
    data["category"] = LEGACY_CATEGORIES.get(data["category"], data["category"])
    if data["category"] not in CATEGORIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"category must be one of {CATEGORIES}")
    notify = data.get("notify") or ("always" if data["arrival_alert"] else "never")
    if notify not in NOTIFY:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"notify must be one of {NOTIFY}")
    if data["category"] == "watchlist":
        notify = "always"  # suspicious people are always urgent
    data["notify"], data["arrival_alert"] = notify, notify != "never"
    data["note"] = (data.get("note") or "").strip() or None
    name = (data.get("name") or "").strip()
    if not name:
        prefix = AUTO_NAME.get(data["category"])
        if prefix is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Give family and familiar people a name")
        taken = set((await db.execute(select(Person.name).where(Person.id != (person_id or 0)))).scalars().all())
        n = 1
        while f"{prefix} #{n}" in taken:
            n += 1
        name = f"{prefix} #{n}"
    data["name"] = name
    return data


def _seen(sample) -> datetime:
    """When a captured face was on camera. Older samples have no seen_at: the
    event is saved after its post-roll, ~15s after the last sighting."""
    return sample.seen_at or (sample.created_at - timedelta(seconds=15))


async def _people_out(db: AsyncSession, people: list[Person]) -> list[PersonOut]:
    ids = [p.id for p in people]
    rows = (await db.execute(
        select(FaceSample.id, FaceSample.person_id, FaceSample.quality, FaceSample.created_at, FaceSample.crop_path,
               FaceSample.camera_id, FaceSample.seen_at, FaceSample.source)
        .where(FaceSample.person_id.in_(ids)).order_by(FaceSample.created_at)
    )).all() if ids else []
    by_person: dict[int, list] = {}
    for r in rows:
        by_person.setdefault(r.person_id, []).append(r)
    out = []
    for p in people:
        samples = by_person.get(p.id, [])
        strength, hint = recognition_strength([r.quality for r in samples], [r.source for r in samples])
        with_crop = [r for r in samples if r.crop_path]
        cover = max(with_crop, key=lambda r: r.quality) if with_crop else None
        captured = [r.seen_at or r.created_at for r in samples if r.camera_id is not None]
        out.append(PersonOut(
            id=p.id, name=p.name, note=p.note, category=LEGACY_CATEGORIES.get(p.category, p.category),
            notify=p.notify or ("always" if p.arrival_alert else "never"), arrival_alert=p.arrival_alert,
            arrival_cooldown_s=p.arrival_cooldown_s, samples=len(samples), strength=strength, strength_hint=hint,
            cover_sample_id=cover.id if cover else None, learned=sum(1 for r in samples if r.source == "auto"),
            cover_camera_id=cover.camera_id if cover else None,
            cover_seen_at=_seen(cover) if cover and cover.camera_id is not None else None,
            last_seen=max(captured) if captured else None, created_at=p.created_at,
        ))
    return out


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


# ── recogniser: SFace (built in) or AdaFace (optional, compared first) ──────


class ModelIn(BaseModel):
    model: str


def _model_status(s: AISettings | None) -> dict:
    from app.services.ai.types import MODEL_DIR
    from app.vision import models as reg

    info = reg.MANIFEST["adaface"]
    return {
        "active": active_model(s),
        "chosen": (getattr(s, "face_model", None) or "sface") if s else "sface",
        "adaface": {"present": reg.present("adaface", MODEL_DIR), "size_mb": info["size_mb"],
                    "license": info["license"]},
        "preparing": face_service.backfill_state,
    }


@router.get("/model")
async def face_model(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return _model_status(await db.get(AISettings, 1))


@router.post("/model/adaface/download")
async def download_adaface(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    """Explicit, admin-triggered download of AdaFace (~92 MB, checksum
    verified). It doesn't replace anything: every saved face also gets an
    AdaFace embedding in the background so the two can be compared on the
    user's own faces before switching."""
    s = await _require_faces(db)
    from app.services.ai.types import MODEL_DIR
    from app.vision import models as reg

    try:
        for name in ("yunet", "sface", "adaface"):  # alignment needs YuNet + SFace locally
            await asyncio.to_thread(reg.download, name, MODEL_DIR, 600.0)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Download failed: {exc}") from exc
    face_service._engine = None
    asyncio.create_task(face_service.backfill_ada())
    return _model_status(s)


@router.put("/model")
async def set_face_model(payload: ModelIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)):
    if payload.model not in ("sface", "adaface"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="model must be sface or adaface")
    s = await _require_faces(db)
    if payload.model == "adaface" and active_model(SimpleNamespace(face_model="adaface")) != "adaface":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Download AdaFace first")
    s.face_model = payload.model
    await db.commit()
    await face_service.reload()
    from app.services.events import emit_event

    await emit_event(None, "audit", f"Face recogniser set to {payload.model} by {user.username}", notify=False)
    return _model_status(s)


def compare_models(samples: list, settings) -> dict:
    """How each recogniser does on the user's own confirmed faces.

    Every confirmed *camera* sighting is taken out in turn and matched against
    all the other confirmed photos (minus its own walk-past, which would be a
    trivial match) - exactly the decision made live: recognised correctly,
    recognised as the wrong person, or missed. Pure function (tested)."""
    out = {}
    for model in ("sface", "adaface"):
        thr = threshold_for(settings, model)
        labelled = [(x, sample_vector(x, model)) for x in samples if x.person_id is not None]
        labelled = [(x, v) for x, v in labelled if v is not None]
        correct = wrong = missed = 0
        genuine, impostor = [], []
        for x, v in labelled:
            if x.source == "upload":
                continue
            best: dict[int, float] = {}
            for y, w in labelled:
                if y.id == x.id or (x.track_uid and y.track_uid == x.track_uid):
                    continue
                best[y.person_id] = max(best.get(y.person_id, -1.0), float(np.dot(v, w)))
            if not best:
                continue
            ranked = sorted(best.items(), key=lambda kv: -kv[1])
            pid, top = ranked[0]
            second = ranked[1][1] if len(ranked) > 1 else -1.0
            if x.person_id in best:
                genuine.append(best[x.person_id])
            impostor += [sc for p, sc in best.items() if p != x.person_id]
            if top >= thr and top - second >= MARGIN:
                if pid == x.person_id:
                    correct += 1
                else:
                    wrong += 1
            else:
                missed += 1
        tested = correct + wrong + missed
        out[model] = {
            "tested": tested, "correct": correct, "wrong": wrong, "missed": missed, "threshold": round(thr, 3),
            "same_person_avg": round(float(np.mean(genuine)), 3) if genuine else None,
            "other_people_max": round(float(np.max(impostor)), 3) if impostor else None,
        }
    s, a = out["sface"], out["adaface"]
    if a["tested"] < 5 or s["tested"] < 5:
        verdict = "not_enough"
    elif a["wrong"] <= s["wrong"] and a["correct"] >= s["correct"] + max(2, round(0.1 * a["tested"])):
        verdict = "adaface"
    else:
        verdict = "sface"
    return {"models": out, "verdict": verdict}


@router.get("/model/compare")
async def compare(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    s = await db.get(AISettings, 1)
    rows = (await db.execute(select(FaceSample).where(FaceSample.person_id.is_not(None)))).scalars().all()
    return compare_models(list(rows), s or SimpleNamespace(face_threshold=60))


@router.get("/readiness")
async def readiness(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    """Everything that must be on for a face to be captured, as a checklist:
    a face is only looked for when a camera's motion detection starts an
    event, AI object detection confirms a person in it, and face recognition
    (with its privacy acknowledgement and models) is on."""
    from app.models.camera import Camera
    from app.services.ai.pipeline import ai_pipeline
    from app.services.ai.types import MODEL_DIR
    from app.vision import models as reg

    s = await db.get(AISettings, 1)
    cams = (await db.execute(select(Camera).order_by(Camera.id))).scalars().all()
    models_ready = all(reg.present(n, MODEL_DIR) for n in ("yunet", "sface")) or bool(
        s and s.backend == "remote" and s.remote_url
    )
    checks = [
        {"key": "ai", "ok": bool(s and s.enabled), "label": "AI is turned on",
         "fix": "Settings → AI → turn on AI"},
        {"key": "detection", "ok": bool(s and s.enabled and s.detection_enabled), "label": "Object detection is on",
         "fix": "Settings → AI → Object detection"},
        {"key": "ai_working", "ok": not getattr(ai_pipeline, "_outage_reported", False),
         "label": "AI detection is working", "fix": "Settings → AI → Test / check the AI worker is running"},
        {"key": "privacy", "ok": bool(s and s.privacy_ack), "label": "Face recognition privacy acknowledged",
         "fix": "Settings → AI → Recognition → acknowledge"},
        {"key": "faces", "ok": bool(s and s.face_enabled), "label": "Face recognition is on",
         "fix": "Settings → AI → Recognition → turn on face recognition"},
        {"key": "models", "ok": models_ready, "label": "Face models downloaded",
         "fix": "People → Download face models"},
    ]
    stats = face_service.capture_stats()
    cameras = [{
        "id": c.id, "name": c.name, "recording_mode": c.recording_mode,
        "motion": bool(c.motion_enabled or c.recording_mode == "motion"),
        "checked_today": stats.get(c.id, {}).get("checked", 0),
    } for c in cams]
    return {"ready": all(c["ok"] for c in checks), "checks": checks, "cameras": cameras}


@router.get("/capture-stats")
async def capture_stats(_: User = Depends(require_admin)):
    """Today's face-capture outcomes per camera: how often a person was
    checked and why no usable face came of it (not facing the camera, too
    small, too blurry/turned). Explains "why isn't it capturing faces?" on
    the People page."""
    return {str(k): v for k, v in face_service.capture_stats().items()}


@router.get("/people", response_model=list[PersonOut])
async def list_people(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    people = (await db.execute(select(Person).order_by(Person.name))).scalars().all()
    return await _people_out(db, list(people))


@router.post("/people", response_model=PersonOut, status_code=status.HTTP_201_CREATED)
async def create_person(payload: PersonIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    await _require_faces(db)
    row = Person(**await _clean(db, payload))
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await face_service.reload()
    return (await _people_out(db, [row]))[0]


@router.put("/people/{person_id}", response_model=PersonOut)
async def update_person(person_id: int, payload: PersonIn, db: AsyncSession = Depends(get_db),
                        _: User = Depends(require_admin)):
    row = await db.get(Person, person_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Person not found")
    for k, v in (await _clean(db, payload, person_id)).items():
        setattr(row, k, v)
    await db.commit()
    await face_service.reload()
    return (await _people_out(db, [row]))[0]


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
async def forget_all(include_family: bool = False, db: AsyncSession = Depends(get_db),
                     user: User = Depends(require_admin)):
    """Deletes every person and face sample - except Family (and their
    samples) unless `include_family`: wiping the household by accident means
    enrolling everyone again."""
    keep = [] if include_family else list((await db.execute(
        select(Person.id).where(Person.category == "household")
    )).scalars().all())
    n = await _purge_samples(db, FaceSample.person_id.is_(None) | FaceSample.person_id.not_in(keep)
                             if keep else FaceSample.id.is_not(None))
    gone = update(Detection).values(person_id=None, face_score=None)
    await db.execute(gone.where(Detection.person_id.not_in(keep)) if keep else gone)
    await db.execute(delete(Person).where(Person.id.not_in(keep)) if keep else delete(Person))
    await db.commit()
    await face_service.reload()
    from app.services.events import emit_event

    kept = f", kept {len(keep)} Family member(s)" if keep else ""
    await emit_event(None, "audit", f"Face data deleted by {user.username}: {n} samples{kept}", notify=False)
    return {"deleted_samples": n, "kept_family": len(keep)}


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
            vec = sample_vector(r, face_service._model)
            if vec is None:
                out.append(SampleOut(id=r.id, person_id=r.person_id, camera_id=r.camera_id, quality=r.quality,
                                     score=r.score, source=r.source, created_at=r.created_at))
                continue
            m = face_service.match(vec, thr)
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


class FaceGroupOut(BaseModel):
    sample_ids: list[int]
    cover_sample_id: int
    sightings: int
    first_seen: UtcDatetime
    last_seen: UtcDatetime
    seen_at: list[UtcDatetime]  # for "mostly 7-8 am" in the viewer's timezone
    camera_ids: list[int]
    faces: list[dict] = []  # [{"id", "camera_id", "seen_at"}] - click to play that moment
    suggestion: dict | None = None
    # The enrolled person it's most like, however weakly, and the score needed
    # to be recognised - so a near miss is visible and one click fixes it.
    closest: dict | None = None
    threshold: float = 0.0


@router.get("/unknown-groups", response_model=list[FaceGroupOut])
async def unknown_groups(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    """'Who is this?' - unnamed faces, with the same person's sightings grouped."""
    s = await db.get(AISettings, 1)
    rows = list((await db.execute(
        select(FaceSample).where(FaceSample.person_id.is_(None)).order_by(FaceSample.created_at.desc()).limit(400)
    )).scalars().all())
    rows.reverse()
    by_id = {r.id: r for r in rows}
    thr = threshold_for(s)
    await face_service._ensure_loaded()
    out = []
    model = face_service._model
    floor = 0.30 if model == "sface" else 0.20
    usable = [SimpleNamespace(id=r.id, track_uid=r.track_uid, embedding=r.embedding_ada if model == "adaface" else r.embedding)
              for r in rows]
    for g in group_faces([u for u in usable if u.embedding], max(floor, thr - GROUP_SLACK)):
        members = [by_id[i] for i in g.ids]
        with_crop = [m for m in members if m.crop_path] or members
        times = sorted(_seen(m) for m in members)
        match = face_service.match(g.centroid, thr)
        sug = ({"person_id": match.person_id, "name": match.name, "score": round(match.score, 3), "band": match.band}
               if match.person_id is not None else None)
        near_pid, near = face_service.closest(g.centroid)
        near_person = face_service.person(near_pid) if near_pid is not None else None
        closest = ({"person_id": near_pid, "name": near_person.name, "score": round(near, 3)}
                   if near_person is not None else None)
        out.append(FaceGroupOut(
            sample_ids=g.ids, cover_sample_id=max(with_crop, key=lambda r: r.quality).id,
            sightings=len({m.track_uid or f"s{m.id}" for m in members}), first_seen=times[0], last_seen=times[-1],
            seen_at=times[-50:], camera_ids=sorted({m.camera_id for m in members if m.camera_id is not None}),
            faces=[{"id": m.id, "camera_id": m.camera_id, "seen_at": serialize_utc(_seen(m))}
                   for m in sorted(members, key=lambda r: -r.quality)[:8]],
            suggestion=sug, closest=closest, threshold=round(thr, 3),
        ))
    out.sort(key=lambda g: g.last_seen, reverse=True)
    return out


class GroupAssignIn(BaseModel):
    sample_ids: list[int] = Field(min_length=1, max_length=400)
    person_id: int | None = None  # an existing person...
    person: PersonIn | None = None  # ...or a new one created from these faces


@router.post("/groups/assign", response_model=PersonOut)
async def assign_group(payload: GroupAssignIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    """Name a group of faces in one go - for an existing or a new person."""
    await _require_faces(db)
    if payload.person_id is not None:
        row = await db.get(Person, payload.person_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Person not found")
    elif payload.person is not None:
        row = Person(**await _clean(db, payload.person))
        db.add(row)
        await db.flush()
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose a person or describe a new one")
    await db.execute(update(FaceSample).where(FaceSample.id.in_(payload.sample_ids), FaceSample.person_id.is_(None))
                     .values(person_id=row.id))
    await db.commit()
    await db.refresh(row)
    await face_service.reload()
    return (await _people_out(db, [row]))[0]


class SampleIdsIn(BaseModel):
    sample_ids: list[int] = Field(min_length=1, max_length=400)


@router.post("/groups/discard")
async def discard_group(payload: SampleIdsIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    """'Ignore' - delete these unnamed captures now instead of waiting for retention."""
    n = await _purge_samples(db, FaceSample.id.in_(payload.sample_ids) & FaceSample.person_id.is_(None))
    await db.commit()
    return {"deleted": n}


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
