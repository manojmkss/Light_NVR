from app.db.session import AsyncSessionLocal
from app.models.event import Event


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
    """Record an event and (unless notify=False) run it through alerting.
    notify=False is for events that are part of a visit another camera
    already alerted about: stored and shown, but not sent again."""
    async with AsyncSessionLocal() as db:
        event = Event(
            camera_id=camera_id, type=event_type, message=message, snapshot_path=snapshot_path,
            payload=payload, priority=priority, incident_id=incident_id,
        )
        db.add(event)
        await db.commit()
        await db.refresh(event)

    if notify:
        from app.services.alerts import maybe_send_alert

        await maybe_send_alert(event)
    return event
