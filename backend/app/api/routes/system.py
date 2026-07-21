import asyncio
import platform
import shutil
import time
from datetime import datetime as dt
from datetime import timedelta
from datetime import timezone as tz
from zoneinfo import ZoneInfo

import psutil
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_admin
from app.core.log_buffer import get_recent_logs
from app.db.session import AsyncSessionLocal, get_db
from app.models.alert_settings import AlertSettings
from app.models.camera import Camera
from app.models.event import Event
from app.models.recording import Recording
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.schemas.system import (
    AlertSettingsOut,
    AlertSettingsUpdate,
    DashboardOut,
    EventOut,
    LogLinesOut,
    NtpPushResultOut,
    SystemSettingsOut,
    SystemSettingsUpdate,
    SystemStatusOut,
    SystemTimeOut,
    TestEmailRequest,
    TestMessageResult,
    TestTelegramRequest,
    TestWhatsAppRequest,
)
from app.services.alerts import send_email
from app.services.camera_supervisor import supervisor
from app.services.storage_manager import storage_manager
from app.services.telegram import send_telegram_message
from app.services.whatsapp import send_whatsapp_message

router = APIRouter(prefix="/api/system", tags=["system"])

_start_time = time.monotonic()


@router.get("/status", response_model=SystemStatusOut)
async def get_status(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    cpu_percent = await asyncio.to_thread(psutil.cpu_percent, 0.3)
    memory = psutil.virtual_memory()
    # Primary is the capacity number that matters long-term; fall back to
    # cache so this still reflects somewhere real during a primary outage.
    disk_path = storage_manager.primary_dir() if storage_manager.is_primary_available() else storage_manager.cache_dir()
    disk = await asyncio.to_thread(shutil.disk_usage, disk_path)

    result = await db.execute(select(Camera))
    cameras = result.scalars().all()
    online = sum(1 for c in cameras if c.status == "online")
    offline = sum(1 for c in cameras if c.status == "offline")

    return SystemStatusOut(
        cpu_percent=cpu_percent,
        memory_percent=memory.percent,
        memory_used_bytes=memory.used,
        memory_total_bytes=memory.total,
        storage_used_bytes=disk.used,
        storage_total_bytes=disk.total,
        storage_free_bytes=disk.free,
        cameras_total=len(cameras),
        cameras_online=online,
        cameras_offline=offline,
        active_workers=len(supervisor.get_active_camera_ids()),
        uptime_seconds=time.monotonic() - _start_time,
    )


@router.get("/events", response_model=list[EventOut])
async def list_events(
    camera_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    stmt = select(Event).order_by(Event.created_at.desc())
    if camera_id is not None:
        stmt = stmt.where(Event.camera_id == camera_id)
    stmt = stmt.offset(offset).limit(min(limit, 200))
    result = await db.execute(stmt)
    return result.scalars().all()


def _resolve_tz(name: str):
    """Return the configured display timezone, falling back to UTC when unset
    or unavailable (e.g. tzdata missing for an odd zone name)."""
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return tz.utc


@router.get("/dashboard", response_model=DashboardOut)
async def get_dashboard(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    """Composite dashboard payload: today's activity counts, a weekly event
    heatmap, and a storage-fill projection. Bucketed in the configured display
    timezone so "today"/hour-of-day line up with what the user sees elsewhere.
    Stored timestamps are naive UTC, so all DB bounds are naive-UTC too.
    """
    settings_row = await db.get(SystemSettings, 1)
    tzinfo = _resolve_tz(settings_row.timezone if settings_row else "")

    now_local = dt.now(tzinfo)
    midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_utc = midnight_local.astimezone(tz.utc).replace(tzinfo=None)
    week_ago_utc = (dt.now(tz.utc) - timedelta(days=7)).replace(tzinfo=None)

    async def _scalar(stmt) -> int:
        result = await db.execute(stmt)
        return int(result.scalar_one() or 0)

    motion_today = await _scalar(
        select(func.count()).select_from(Event).where(Event.type == "motion", Event.created_at >= midnight_utc)
    )
    failures_today = await _scalar(
        select(func.count()).select_from(Event).where(Event.type == "camera_error", Event.created_at >= midnight_utc)
    )
    recordings_today = await _scalar(
        select(func.count()).select_from(Recording).where(Recording.started_at >= midnight_utc)
    )
    cameras_offline = await _scalar(
        select(func.count()).select_from(Camera).where(Camera.status == "offline")
    )

    # Weekly heatmap: 7 rows (Mon..Sun) x 24 hourly columns of event counts.
    heatmap = [[0] * 24 for _ in range(7)]
    ev_result = await db.execute(select(Event.created_at).where(Event.created_at >= week_ago_utc))
    for (created_at,) in ev_result.all():
        if created_at is None:
            continue
        local_dt = created_at.replace(tzinfo=tz.utc).astimezone(tzinfo)
        heatmap[local_dt.weekday()][local_dt.hour] += 1

    # Storage projection from the last 7 days of recording growth.
    disk_path = storage_manager.primary_dir() if storage_manager.is_primary_available() else storage_manager.cache_dir()
    disk = await asyncio.to_thread(shutil.disk_usage, disk_path)
    recent_bytes = await _scalar(
        select(func.coalesce(func.sum(Recording.size_bytes), 0)).where(Recording.started_at >= week_ago_utc)
    )
    per_day = recent_bytes / 7 if recent_bytes else 0
    days_to_full = full_date = None
    if per_day > 0:
        days_to_full = disk.free / per_day
        full_date = (now_local + timedelta(days=days_to_full)).date().isoformat()

    return DashboardOut(
        motion_events_today=motion_today,
        recording_failures_today=failures_today,
        cameras_offline=cameras_offline,
        recordings_today=recordings_today,
        heatmap=heatmap,
        storage_days_to_full=days_to_full,
        storage_full_date=full_date,
    )


@router.get("/alert-settings", response_model=AlertSettingsOut)
async def get_alert_settings(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return await db.get(AlertSettings, 1)


@router.put("/alert-settings", response_model=AlertSettingsOut)
async def update_alert_settings(
    payload: AlertSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    alert_settings = await db.get(AlertSettings, 1)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(alert_settings, field, value)
    await db.commit()
    await db.refresh(alert_settings)
    return alert_settings


@router.post("/alert-settings/test-email", response_model=TestMessageResult)
async def test_email(payload: TestEmailRequest, _: User = Depends(require_admin)):
    success, message = await send_email(payload, "LightNVR test email", "This is a test email from LightNVR.")
    return TestMessageResult(success=success, message=message)


@router.post("/alert-settings/test-telegram", response_model=TestMessageResult)
async def test_telegram(payload: TestTelegramRequest, _: User = Depends(require_admin)):
    success, message = await send_telegram_message(
        payload.bot_token, payload.chat_id, "This is a test message from LightNVR."
    )
    return TestMessageResult(success=success, message=message)


@router.post("/alert-settings/test-whatsapp", response_model=TestMessageResult)
async def test_whatsapp(payload: TestWhatsAppRequest, _: User = Depends(require_admin)):
    success, message = await send_whatsapp_message(
        payload.phone_number_id, payload.access_token, payload.recipient_number, "This is a test message from LightNVR."
    )
    return TestMessageResult(success=success, message=message)


@router.get("/time", response_model=SystemTimeOut)
async def get_system_time(_: User = Depends(get_current_user)):
    now = dt.now(tz.utc)
    return SystemTimeOut(server_utc=now.isoformat(), server_timestamp=now.timestamp())


@router.get("/settings", response_model=SystemSettingsOut)
async def get_system_settings(_: User = Depends(get_current_user)):
    # Readable by any authenticated user (not just admins): the frontend needs
    # the display timezone to render timestamps consistently for viewers too.
    # Writing settings stays admin-only below.
    async with AsyncSessionLocal() as db:
        record = await db.get(SystemSettings, 1)
        return SystemSettingsOut.model_validate(record)


@router.put("/settings", response_model=SystemSettingsOut)
async def update_system_settings(payload: SystemSettingsUpdate, _: User = Depends(require_admin)):
    async with AsyncSessionLocal() as db:
        record = await db.get(SystemSettings, 1)
        for field, value in payload.model_dump(exclude_none=True).items():
            setattr(record, field, value)
        await db.commit()
        await db.refresh(record)
        return SystemSettingsOut.model_validate(record)


@router.post("/push-ntp", response_model=list[NtpPushResultOut])
async def push_ntp(_: User = Depends(require_admin)):
    """Push the configured NTP server to every enabled ONVIF camera and switch
    their clock source to NTP - keeps camera timestamps (and therefore the
    playback timeline) aligned without touching each camera's own web UI.
    """
    from app.services.onvif_time import push_ntp_to_cameras

    async with AsyncSessionLocal() as db:
        record = await db.get(SystemSettings, 1)
        ntp_server = (record.ntp_server or "").strip() if record else ""

    if not ntp_server:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Set an NTP server above and save first")

    results = await push_ntp_to_cameras(ntp_server)
    return [NtpPushResultOut(camera_id=r.camera_id, name=r.name, success=r.success, detail=r.detail) for r in results]


@router.get("/logs", response_model=LogLinesOut)
async def get_logs(limit: int = Query(default=200, le=500), _: User = Depends(require_admin)):
    """Recent backend log lines (credential-scrubbed, in-memory ring buffer) -
    the GUI answer to `docker compose logs backend`.
    """
    return LogLinesOut(lines=get_recent_logs(limit))


@router.get("/diagnostics")
async def download_diagnostics(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    """A single JSON support bundle: versions, health numbers, camera states
    (no URLs or credentials), recent events, and recent logs. Meant to be
    attached to a bug report without leaking anything sensitive.
    """
    memory = psutil.virtual_memory()
    disk_path = storage_manager.primary_dir() if storage_manager.is_primary_available() else storage_manager.cache_dir()
    disk = await asyncio.to_thread(shutil.disk_usage, disk_path)

    cam_result = await db.execute(select(Camera))
    cameras = [
        {
            "id": c.id,
            "name": c.name,
            "status": c.status,
            "enabled": c.enabled,
            "codec": c.codec,
            "recording_mode": c.recording_mode,
            "motion_enabled": c.motion_enabled,
            "has_onvif": bool(c.onvif_address),
            "last_error": c.last_error,
            "last_seen_at": c.last_seen_at.isoformat() + "Z" if c.last_seen_at else None,
        }
        for c in cam_result.scalars().all()
    ]

    ev_result = await db.execute(select(Event).order_by(Event.created_at.desc()).limit(50))
    events = [
        {"type": e.type, "camera_id": e.camera_id, "message": e.message, "created_at": e.created_at.isoformat() + "Z"}
        for e in ev_result.scalars().all()
    ]

    settings_row = await db.get(SystemSettings, 1)

    bundle = {
        "generated_at": dt.now(tz.utc).isoformat(),
        "app_version": "0.1.0",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "uptime_seconds": round(time.monotonic() - _start_time),
        "memory_percent": memory.percent,
        "disk": {"total": disk.total, "free": disk.free},
        "settings": {
            "timezone": settings_row.timezone if settings_row else "",
            "ntp_server": settings_row.ntp_server if settings_row else "",
        },
        "active_workers": len(supervisor.get_active_camera_ids()),
        "cameras": cameras,
        "recent_events": events,
        "recent_logs": get_recent_logs(300),
    }

    filename = f"lightnvr-diagnostics-{dt.now(tz.utc).strftime('%Y%m%d-%H%M%S')}.json"
    return JSONResponse(bundle, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
