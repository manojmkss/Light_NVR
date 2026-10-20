"""Short-lived signed links to an event snapshot.

A push notification's image is fetched by the browser's push service with no
login, so it needs a URL that works on its own - but one that can't be
enumerated or kept forever. The link carries only the event id and an expiry,
signed with the server's secret (HMAC-SHA256); no personal data.
"""

import hashlib
import hmac
import time

from app.core.config import settings

TTL_SECONDS = 24 * 3600


def _sig(event_id: int, exp: int) -> str:
    key = (settings.jwt_secret_key or "lightnvr").encode()
    return hmac.new(key, f"snap:{event_id}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


async def signed_event_snapshot_path(event_id: int, ttl: int = TTL_SECONDS) -> str:
    exp = int(time.time()) + ttl
    return f"/api/public/events/{event_id}/snapshot.jpg?exp={exp}&sig={_sig(event_id, exp)}"


def verify(event_id: int, exp: int, sig: str) -> bool:
    if exp < time.time():
        return False
    return hmac.compare_digest(_sig(event_id, exp), sig or "")
