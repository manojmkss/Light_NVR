from app.db.session import AsyncSessionLocal
from app.models.event import Event


async def emit_event(camera_id: int | None, event_type: str, message: str) -> Event:
    async with AsyncSessionLocal() as db:
        event = Event(camera_id=camera_id, type=event_type, message=message)
        db.add(event)
        await db.commit()
        await db.refresh(event)

    from app.services.alerts import maybe_send_alert

    await maybe_send_alert(event)
    return event
