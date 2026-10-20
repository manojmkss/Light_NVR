"""Search what the cameras saw, in plain words.

    "person in red yesterday"      "blue car driveway"
    "Ramesh at the gate"           "package today"      "dog night"

Words are matched against: object kinds, clothing/vehicle colours, names of
recognised people, camera names, areas and named zones, and simple times
(today, yesterday, this week, night, morning...). Everything is local SQL +
a small in-memory filter - fast at home-NVR volumes.
"""

import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.camera import Camera
from app.models.detection import Detection
from app.models.person import Person
from app.models.user import User
from app.vision.attributes import COLOURS

router = APIRouter(prefix="/api/search", tags=["search"])

LABEL_WORDS = {
    "person": "person", "people": "person", "man": "person", "woman": "person", "someone": "person",
    "visitor": "person", "car": "car", "cars": "car", "truck": "truck", "lorry": "truck", "van": "truck",
    "bus": "bus", "bike": "bicycle", "bicycle": "bicycle", "cycle": "bicycle", "motorcycle": "motorcycle",
    "scooter": "motorcycle", "motorbike": "motorcycle", "dog": "dog", "cat": "cat", "bird": "bird",
    "package": "package", "parcel": "package", "bag": "package", "backpack": "backpack", "suitcase": "suitcase",
}
PACKAGE = {"backpack", "handbag", "suitcase"}
UPPER_WORDS = {"shirt", "top", "jacket", "tshirt", "t-shirt", "coat", "hoodie", "kurta", "saree", "sweater"}
LOWER_WORDS = {"pants", "trousers", "jeans", "shorts", "skirt", "lower"}
GREY = {"gray": "grey"}


def parse(q: str, names: dict[str, int], cameras: dict[str, int], now: datetime) -> dict:
    words = [w for w in re.split(r"[^a-z0-9\-]+", q.lower()) if w]
    f: dict = {"labels": set(), "colours": [], "people": set(), "cameras": set(), "places": set(),
               "since": None, "until": None, "hours": None, "rest": []}
    i = 0
    while i < len(words):
        w = GREY.get(words[i], words[i])
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if w in COLOURS:
            part = "upper" if nxt in UPPER_WORDS else "lower" if nxt in LOWER_WORDS else None
            f["colours"].append((w, part))
            if part:
                i += 1
        elif w in LABEL_WORDS:
            f["labels"].add(LABEL_WORDS[w])
        elif w in names:
            f["people"].add(names[w])
            f["labels"].add("person")
        elif w in cameras:
            f["cameras"].add(cameras[w])
        elif w == "today":
            f["since"] = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif w == "yesterday":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
            f["since"], f["until"] = start, start + timedelta(days=1)
        elif w in ("week", "7days"):
            f["since"] = now - timedelta(days=7)
        elif w in ("night", "tonight"):
            f["hours"] = (21, 6)
        elif w == "morning":
            f["hours"] = (5, 12)
        elif w == "afternoon":
            f["hours"] = (12, 17)
        elif w == "evening":
            f["hours"] = (17, 21)
        elif w not in {"in", "at", "the", "a", "an", "on", "with", "near", "by", "of", "and", "last", "this"}:
            f["rest"].append(w)
        i += 1
    return f


def _hour_ok(hour: int, hours) -> bool:
    if hours is None:
        return True
    a, b = hours
    return a <= hour < b if a < b else (hour >= a or hour < b)


@router.get("")
async def search(q: str = Query("", max_length=200), limit: int = Query(60, le=200),
                 db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    from app.services.ai.digest import _display_tz

    tz = await _display_tz()
    now = datetime.now(tz)
    people = (await db.execute(select(Person))).scalars().all()
    cams = (await db.execute(select(Camera))).scalars().all()
    names = {p.name.lower(): p.id for p in people}
    names.update({part.lower(): p.id for p in people for part in p.name.split() if len(part) > 2})
    cam_words: dict[str, int] = {}
    for c in cams:
        for part in re.split(r"\W+", c.name.lower()):
            if len(part) > 2:
                cam_words[part] = c.id
    f = parse(q, names, cam_words, now)

    stmt = select(Detection).order_by(Detection.created_at.desc()).limit(3000)
    if f["since"]:
        stmt = stmt.where(Detection.created_at >= f["since"].astimezone(timezone.utc).replace(tzinfo=None))
    if f["until"]:
        stmt = stmt.where(Detection.created_at < f["until"].astimezone(timezone.utc).replace(tzinfo=None))
    if f["cameras"]:
        stmt = stmt.where(Detection.camera_id.in_(f["cameras"]))
    rows = (await db.execute(stmt)).scalars().all()

    cam_name = {c.id: c.name for c in cams}
    cam_area = {c.id: (c.area or "") for c in cams}
    person_name = {p.id: p.name for p in people}
    out = []
    for d in rows:
        label = "package" if d.label in PACKAGE else d.label
        if f["labels"] and label not in f["labels"] and d.label not in f["labels"]:
            continue
        if f["people"] and d.person_id not in f["people"]:
            continue
        attrs = d.attributes or {}
        ok = True
        for colour, part in f["colours"]:
            have = [attrs.get(part)] if part else [attrs.get("upper"), attrs.get("lower"), attrs.get("colour")]
            if colour not in have:
                ok = False
        if not ok:
            continue
        created = d.created_at.replace(tzinfo=timezone.utc).astimezone(tz)
        if not _hour_ok(created.hour, f["hours"]):
            continue
        if f["rest"]:
            # Leftover words must match an area or a named zone ("driveway").
            places = " ".join([cam_area.get(d.camera_id, "")] + list(d.zones or [])).lower()
            if not all(w in places for w in f["rest"]):
                continue
        out.append({
            "id": d.id, "camera_id": d.camera_id, "camera": cam_name.get(d.camera_id), "label": d.label,
            "confidence": d.confidence, "created_at": d.created_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
            "attributes": attrs, "zones": d.zones, "person": person_name.get(d.person_id),
            "event_id": d.event_id, "recording_id": d.recording_id, "incident_id": d.incident_id,
            "has_snapshot": bool(d.snapshot_path),
        })
        if len(out) >= limit:
            break
    understood = {
        "objects": sorted(f["labels"]), "colours": [c if not p else f"{c} {p}" for c, p in f["colours"]],
        "people": [person_name.get(i) for i in f["people"]], "cameras": [cam_name.get(i) for i in f["cameras"]],
        "since": f["since"].isoformat() if f["since"] else None, "hours": f["hours"], "places": f["rest"],
    }
    return {"query": q, "understood": understood, "results": out}
