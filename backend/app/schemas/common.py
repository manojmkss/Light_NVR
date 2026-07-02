from datetime import datetime, timezone
from typing import Annotated

from pydantic import PlainSerializer


def serialize_utc(value: datetime) -> str:
    """Render a stored timestamp as ISO 8601 in UTC with an explicit ``Z``.

    Recordings/events are tagged with ``datetime.now(timezone.utc)`` - the
    NVR's own clock is the single reference time - but SQLite stores datetimes
    naive, so they come back off the database without tzinfo. Serialising them
    without a zone makes browsers parse the string as *local* time, which is a
    silent offset bug: a 12:50 UTC recording shows as 12:50 in an IST browser
    instead of 18:20. Forcing an explicit UTC marker makes every client that
    does ``new Date(value)`` convert to the viewer's local time correctly.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


# Annotate any client-facing stored timestamp with this so it is always emitted
# as unambiguous UTC.
UtcDatetime = Annotated[datetime, PlainSerializer(serialize_utc, return_type=str, when_used="json")]
