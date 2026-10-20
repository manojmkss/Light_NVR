"""The address people use to reach this NVR from outside (for links in
alerts). Only what the user typed in Settings -> Alerts; never guessed."""

from app.db.session import AsyncSessionLocal
from app.models.alert_settings import AlertSettings


async def public_base_url() -> str | None:
    async with AsyncSessionLocal() as db:
        row = await db.get(AlertSettings, 1)
    url = (getattr(row, "public_base_url", "") or "").strip() if row else ""
    return url if url.startswith(("http://", "https://")) else None
