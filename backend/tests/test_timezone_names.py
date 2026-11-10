"""Time zone names as browsers report them resolve - not silently UTC.

Debian 13 (the backend image's base) moved the old aliases out of its tzdata
package; Windows browsers still report India as Asia/Calcutta, and with that
setting the digest, dashboard, alerts and visit summaries all ran on UTC."""

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.api.routes.system import _resolve_tz


@pytest.mark.parametrize("name", ["Asia/Calcutta", "Asia/Kolkata", "Asia/Saigon", "Europe/Kiev", "US/Eastern"])
def test_legacy_and_current_names_resolve(name):
    assert _resolve_tz(name) is not timezone.utc
    ZoneInfo(name)


def test_india_is_five_and_a_half_hours_ahead():
    now = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)
    assert now.astimezone(_resolve_tz("Asia/Calcutta")).utcoffset() == timedelta(hours=5, minutes=30)


def test_display_tz_uses_the_stored_setting(monkeypatch):
    from app.services.ai import digest

    class Row:
        timezone = "Asia/Calcutta"

    class Db:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *_a):
            return Row()

    monkeypatch.setattr(digest, "AsyncSessionLocal", lambda: Db())
    tz = asyncio.run(digest._display_tz())
    assert tz is not timezone.utc and datetime(2026, 1, 1, tzinfo=tz).utcoffset() == timedelta(hours=5, minutes=30)
