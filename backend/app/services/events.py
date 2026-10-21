import asyncio
import logging

from app.db.session import AsyncSessionLocal
from app.models.event import Event

logger = logging.getLogger(__name__)

# Alert deliveries in flight (strong references; asyncio only keeps weak ones).
_deliveries: set[asyncio.Task] = set()


async def emit_event(
    camera_id: int | None,
    event_type: str,
    message: str,
    snapshot_path: str | None = None,
    payload: dict | None = None,
    priority: int = 0,
    incident_id: int | None = None,
    notify: bool = True,
) -> Event:
    """Record an event and (unless notify=False) send its alerts.

    Delivery runs in the background: a slow mail server or a Telegram outage
    must never hold up the caller - often a camera's AI loop or recorder,
    which would otherwise stop watching while an email times out."""
    async with AsyncSessionLocal() as db:
        event = Event(
            camera_id=camera_id, type=event_type, message=message, snapshot_path=snapshot_path,
            payload=payload, priority=priority, incident_id=incident_id,
        )
        db.add(event)
        await db.commit()
        await db.refresh(event)

    if notify:
        task = asyncio.create_task(_deliver(event))
        _deliveries.add(task)
        task.add_done_callback(_deliveries.discard)
    return event


async def _deliver(event: Event) -> None:
    from app.services.alerts import maybe_send_alert

    try:
        await asyncio.wait_for(maybe_send_alert(event), timeout=180)
    except asyncio.TimeoutError:
        logger.warning("Alert delivery for event %s timed out", event.id)
    except Exception:
        logger.exception("Alert delivery for event %s failed", event.id)


async def flush_deliveries(timeout: float = 10.0) -> None:
    """On shutdown: give in-flight alerts a moment to go out."""
    if _deliveries:
        await asyncio.wait(list(_deliveries), timeout=timeout)
