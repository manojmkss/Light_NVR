import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_current_user_flexible, require_admin
from app.db.session import get_db
from app.models.event import Event
from app.models.incident import AreaLink, Incident
from app.models.security import AlertRule, ModeSchedule
from app.models.user import User
from app.schemas.common import UtcDatetime

router = APIRouter(prefix="/api/security", tags=["security"])
public_router = APIRouter(prefix="/api/public", tags=["public"])

Mode = Literal["home", "away", "night", "disarmed"]
Action = Literal["notify", "notify_high", "silent"]


class ModeOut(BaseModel):
    mode: str
    source: str
    manual_mode: str
    hold: bool
    set_by: str | None = None
    set_at: UtcDatetime | None = None
    next_change_at: UtcDatetime | None = None


class ModeIn(BaseModel):
    mode: Mode
    hold: bool = False  # keep until changed by hand (ignore the schedule)
    source: Literal["manual", "api", "ha"] = "manual"


class ScheduleIn(BaseModel):
    days_mask: int = Field(127, ge=1, le=127)
    at_hhmm: str = Field("23:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    mode: Mode
    enabled: bool = True


class ScheduleOut(ScheduleIn):
    id: int

    class Config:
        from_attributes = True


class RuleIn(BaseModel):
    name: str = "Rule"
    position: int = 0
    enabled: bool = True
    modes: list[Mode] | None = None
    camera_ids: list[int] | None = None
    kinds: list[str] | None = None
    objects: list[str] | None = None
    zones: list[str] | None = None
    identities: list[str] | None = None
    action: Action = "notify"
    cooldown_s: int | None = Field(None, ge=0, le=86400)
    webhook_url: str | None = Field(None, max_length=512)


class RuleOut(RuleIn):
    id: int

    class Config:
        from_attributes = True


def _check_webhook(url: str | None) -> None:
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook URL must start with http(s)://")


@router.get("/mode", response_model=ModeOut)
async def get_mode(_: User = Depends(get_current_user)):
    from app.services.security_mode import current_mode

    return await current_mode()


@router.put("/mode", response_model=ModeOut)
async def put_mode(payload: ModeIn, user: User = Depends(require_admin)):
    """Admins only: a viewer account (which may be limited to some cameras, or
    shared with a guest) must not be able to disarm alerting."""
    from app.services.security_mode import set_mode

    return await set_mode(payload.mode, payload.source, user.username, payload.hold)


@router.get("/schedules", response_model=list[ScheduleOut])
async def list_schedules(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return (await db.execute(select(ModeSchedule).order_by(ModeSchedule.at_hhmm))).scalars().all()


@router.post("/schedules", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
async def create_schedule(payload: ScheduleIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    row = ModeSchedule(**payload.model_dump())
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.put("/schedules/{schedule_id}", response_model=ScheduleOut)
async def update_schedule(
    schedule_id: int, payload: ScheduleIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)
):
    row = await db.get(ModeSchedule, schedule_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    for k, v in payload.model_dump().items():
        setattr(row, k, v)
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(schedule_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    row = await db.get(ModeSchedule, schedule_id)
    if row is not None:
        await db.delete(row)
        await db.commit()


@router.get("/rules", response_model=list[RuleOut])
async def list_rules(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return (await db.execute(select(AlertRule).order_by(AlertRule.position, AlertRule.id))).scalars().all()


@router.post("/rules", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(payload: RuleIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    _check_webhook(payload.webhook_url)
    row = AlertRule(**payload.model_dump())
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.put("/rules/{rule_id}", response_model=RuleOut)
async def update_rule(rule_id: int, payload: RuleIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    _check_webhook(payload.webhook_url)
    row = await db.get(AlertRule, rule_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")
    for k, v in payload.model_dump().items():
        setattr(row, k, v)
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    row = await db.get(AlertRule, rule_id)
    if row is not None:
        await db.delete(row)
        await db.commit()


@router.post("/rules/presets", response_model=list[RuleOut])
async def add_presets(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    """Adds the suggested Home/Away/Night rules, all switched OFF, so the user
    reviews and enables the ones they want."""
    from app.services.notify_policy import PRESETS

    existing = {r.name for r in (await db.execute(select(AlertRule))).scalars().all()}
    base = len(existing)
    rows = []
    for i, (name, modes, kinds, objects, action, identities) in enumerate(PRESETS):
        if name in existing:
            continue
        row = AlertRule(name=name, position=(base + i) * 10, enabled=False, modes=modes, kinds=kinds,
                        objects=objects, action=action, identities=identities)
        db.add(row)
        rows.append(row)
    await db.commit()
    return (await db.execute(select(AlertRule).order_by(AlertRule.position, AlertRule.id))).scalars().all()


@router.post("/rules/preview")
async def preview_rules(
    mode: Mode | None = None, limit: int = Query(20, le=100),
    db: AsyncSession = Depends(get_db), _: User = Depends(require_admin),
):
    """What the current rules would have done with the last N events - so
    nobody silences themselves by accident."""
    from app.services.notify_policy import decide_with
    from app.services.security_mode import current_mode

    rules = (await db.execute(select(AlertRule))).scalars().all()
    use_mode = mode or (await current_mode())["mode"]
    events = (await db.execute(select(Event).order_by(Event.created_at.desc()).limit(limit))).scalars().all()
    out = []
    for e in events:
        d = decide_with(rules, e.type, e.camera_id, use_mode, e.payload)
        out.append({"event_id": e.id, "type": e.type, "message": e.message.split("\n", 1)[0],
                    "action": d.action, "rule": d.rule_name})
    return {"mode": use_mode, "results": out}


@public_router.get("/events/{event_id}/snapshot.jpg")
async def signed_snapshot(event_id: int, exp: int, sig: str, db: AsyncSession = Depends(get_db)):
    """Snapshot for push notifications: valid only with an unexpired signature."""
    from app.services.signed_media import verify

    if not verify(event_id, exp, sig):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Link expired or invalid")
    event = await db.get(Event, event_id)
    if event is None or not event.snapshot_path or not os.path.exists(event.snapshot_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot not found")
    return FileResponse(event.snapshot_path, media_type="image/jpeg")


# ── areas & visits (incidents) ──────────────────────────────────────────────


class AreaLinkIn(BaseModel):
    area_a: str = Field(min_length=1, max_length=64)
    area_b: str = Field(min_length=1, max_length=64)
    min_seconds: int = Field(0, ge=0, le=3600)
    max_seconds: int = Field(60, ge=1, le=3600)


class AreaLinkOut(AreaLinkIn):
    id: int

    class Config:
        from_attributes = True


class IncidentOut(BaseModel):
    id: int
    started_at: UtcDatetime
    ended_at: UtcDatetime | None = None
    status: str
    camera_ids: list[int] | None = None
    areas_path: list[str] | None = None
    counts: dict | None = None
    identities: list[str] | None = None
    summary: str | None = None
    snapshot_path: str | None = None
    mode: str | None = None

    class Config:
        from_attributes = True


@router.get("/areas")
async def list_areas(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    """Area names in use by cameras, plus the walking-time links between them."""
    from app.models.camera import Camera

    cams = (await db.execute(select(Camera))).scalars().all()
    areas: dict[str, list[dict]] = {}
    for c in cams:
        areas.setdefault(c.area or "", []).append({"id": c.id, "name": c.name})
    links = (await db.execute(select(AreaLink))).scalars().all()
    return {"areas": [{"name": k, "cameras": v} for k, v in sorted(areas.items())],
            "links": [AreaLinkOut.model_validate(x).model_dump() for x in links]}


@router.post("/areas/links", response_model=AreaLinkOut, status_code=status.HTTP_201_CREATED)
async def create_area_link(payload: AreaLinkIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    if payload.max_seconds < payload.min_seconds:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="max must be at least min")
    row = AreaLink(**payload.model_dump())
    db.add(row)
    await db.commit()
    await db.refresh(row)
    from app.services.incident_store import reload_links

    await reload_links()
    return row


@router.delete("/areas/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_area_link(link_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    row = await db.get(AreaLink, link_id)
    if row is not None:
        await db.delete(row)
        await db.commit()
    from app.services.incident_store import reload_links

    await reload_links()


@router.get("/incidents", response_model=list[IncidentOut])
async def list_incidents(limit: int = Query(50, le=200), offset: int = 0, db: AsyncSession = Depends(get_db),
                         _: User = Depends(get_current_user)):
    stmt = select(Incident).order_by(Incident.started_at.desc()).offset(offset).limit(limit)
    return (await db.execute(stmt)).scalars().all()


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    from app.schemas.system import EventOut

    row = await db.get(Incident, incident_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Visit not found")
    events = (await db.execute(
        select(Event).where(Event.incident_id == incident_id).order_by(Event.created_at)
    )).scalars().all()
    return {"incident": IncidentOut.model_validate(row).model_dump(mode="json"),
            "events": [EventOut.model_validate(e).model_dump(mode="json") for e in events]}


@router.get("/incidents/{incident_id}/snapshot.jpg")
async def incident_snapshot(incident_id: int, db: AsyncSession = Depends(get_db),
                            _: User = Depends(get_current_user_flexible)):
    row = await db.get(Incident, incident_id)
    if row is None or not row.snapshot_path or not os.path.exists(row.snapshot_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot not found")
    return FileResponse(row.snapshot_path, media_type="image/jpeg")
