"""The arm mode in force right now: manual choice, or the weekly schedule.

Resolution:
- a manual/API/HA choice with `hold` stays until changed again;
- otherwise a manual choice lasts until the next scheduled transition after
  it was made, then the schedule takes over again;
- with no schedule, the last manual choice simply stays.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.security import MODES, ModeSchedule, SecurityState

logger = logging.getLogger(__name__)


def _parse_hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return max(0, min(23, int(h))), max(0, min(59, int(m)))


def last_transition(schedules: list, now_local: datetime) -> tuple[datetime, str] | None:
    """Latest enabled transition at or before now (local time), looking back
    up to 8 days so a weekly pattern always resolves."""
    best: tuple[datetime, str] | None = None
    for sch in schedules:
        if not sch.enabled or sch.mode not in MODES:
            continue
        try:
            hh, mm = _parse_hhmm(sch.at_hhmm)
        except (ValueError, AttributeError):
            continue
        for back in range(0, 8):
            day = now_local - timedelta(days=back)
            if not (sch.days_mask >> day.weekday()) & 1:
                continue
            at = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if at <= now_local:
                if best is None or at > best[0]:
                    best = (at, sch.mode)
                break
    return best


def next_transition(schedules: list, after_local: datetime) -> datetime | None:
    best = None
    for sch in schedules:
        if not sch.enabled or sch.mode not in MODES:
            continue
        try:
            hh, mm = _parse_hhmm(sch.at_hhmm)
        except (ValueError, AttributeError):
            continue
        for ahead in range(0, 8):
            day = after_local + timedelta(days=ahead)
            if not (sch.days_mask >> day.weekday()) & 1:
                continue
            at = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if at > after_local:
                if best is None or at < best:
                    best = at
                break
    return best


def resolve(state, schedules: list, now_local: datetime, tz) -> tuple[str, str]:
    """(mode, why) for `now_local`."""
    manual_mode = state.mode if state else "home"
    if state is None:
        return manual_mode, "default"
    if state.hold or not any(s.enabled for s in schedules):
        return manual_mode, state.source
    set_local = state.set_at.replace(tzinfo=state.set_at.tzinfo or timezone.utc).astimezone(tz)
    nxt = next_transition(schedules, set_local)
    if nxt is not None and now_local < nxt:
        return manual_mode, state.source  # the manual choice hasn't been superseded yet
    last = last_transition(schedules, now_local)
    if last is None:
        return manual_mode, state.source
    return last[1], "schedule"


async def _tz():
    from app.services.ai.digest import _display_tz

    try:
        return await _display_tz()
    except Exception:
        return timezone.utc


async def current_mode() -> dict:
    tz = await _tz()
    async with AsyncSessionLocal() as db:
        state = await db.get(SecurityState, 1)
        schedules = (await db.execute(select(ModeSchedule))).scalars().all()
    now_local = datetime.now(tz)
    mode, why = resolve(state, schedules, now_local, tz)
    nxt = next_transition(schedules, now_local)
    return {
        "mode": mode,
        "source": why,
        "manual_mode": state.mode if state else "home",
        "hold": bool(state and state.hold),
        "set_by": state.set_by if state else None,
        "set_at": state.set_at if state else None,
        "next_change_at": nxt.astimezone(timezone.utc) if nxt else None,
    }


async def set_mode(mode: str, source: str = "manual", set_by: str | None = None, hold: bool = False) -> dict:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    async with AsyncSessionLocal() as db:
        state = await db.get(SecurityState, 1)
        if state is None:
            state = SecurityState(id=1)
            db.add(state)
        state.mode, state.source, state.set_by, state.hold = mode, source, set_by, hold
        state.set_at = datetime.now(timezone.utc)
        await db.commit()
    info = await current_mode()
    await _announce(info["mode"], f"set by {set_by or source}")
    return info


_last_announced: str | None = None


async def _announce(mode: str, why: str) -> None:
    global _last_announced
    if mode == _last_announced:
        return
    _last_announced = mode
    from app.services.events import emit_event

    try:
        await emit_event(None, "mode_changed", f"Security mode is now {mode.upper()} ({why})", payload={"mode": mode})
    except Exception:
        logger.exception("Could not record mode change")


async def watch_schedule() -> None:
    """Background loop: notice scheduled transitions and record them."""
    global _last_announced
    try:
        _last_announced = (await current_mode())["mode"]
    except Exception:
        _last_announced = None
    while True:
        await asyncio.sleep(30)
        try:
            info = await current_mode()
            if info["mode"] != _last_announced:
                await _announce(info["mode"], "schedule" if info["source"] == "schedule" else info["source"])
        except Exception:
            logger.exception("Security mode schedule check failed")
