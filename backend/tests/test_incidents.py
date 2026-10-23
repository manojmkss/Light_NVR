"""Visits across cameras: camera groups, and the same person joining visits."""

import asyncio

import numpy as np

from app.services import incidents as inc_mod
from app.services.incidents import IncidentManager, area_rank


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def manager():
    clock = Clock()
    sent, merged = [], []

    async def emit(kind, message, snapshot, payload, camera_id, inc):
        sent.append((kind, message, payload))

    ids = iter(range(1, 100))

    async def persist(inc, closed=False):
        return inc.id or next(ids)

    async def on_merged(src, dst):
        merged.append((src, dst))

    m = IncidentManager(persist=persist, emit=emit, clock=clock)
    m._merged = on_merged
    return m, clock, sent, merged


def run(coro):
    return asyncio.run(coro)


def sig(seed: int) -> tuple:
    """A clothing signature (two normalised colour histograms)."""
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(2):
        h = rng.random(72).astype(np.float32)
        out.append(h / h.sum())
    return tuple(out)


def test_cameras_in_one_group_close_in_time_are_one_visit():
    async def go():
        m, clock, _, _ = manager()
        a, _, alert_a = await m.join(1, "Gate cam", "Front porch")
        m.mark_alerted(a)  # the worker does this once the first alert went out
        clock.t += 20
        b, _, alert_b = await m.join(2, "Porch cam", "Front porch")
        return a is b, alert_a, alert_b

    same, alert_a, alert_b = run(go())
    assert same and alert_a and not alert_b  # one alert for the visit


def test_cameras_without_a_group_are_each_on_their_own():
    async def go():
        m, clock, _, _ = manager()
        a, ma, _ = await m.join(1, "Gate cam", None)
        clock.t += 10
        b, mb, alert_b = await m.join(2, "Yard cam", None)
        return a is b, alert_b, ma.area, mb.area

    same, alert_b, area_a, area_b = run(go())
    assert not same and alert_b
    assert (area_a, area_b) == ("Gate cam", "Yard cam")  # the visit's route names the camera


def test_same_recognised_person_in_another_group_within_a_minute_joins_the_visit():
    async def go():
        m, clock, sent, merged = manager()
        a, ma, _ = await m.join(1, "Gate cam", "Front porch")
        m.mark_alerted(a)
        # As the camera worker does: compare with other groups, then record the name.
        await m.same_person(a, "Front porch", "Manoj", sig(1))
        assert m.add_identity(a, "Manoj")  # new to the visit: "Manoj arrived" goes out
        await m.leave(a, ma, {"person": 1}, [], (990, 1010), b"x", ["Manoj"])
        clock.t += 30
        b, mb, _ = await m.join(3, "Yard cam", "Back yard")  # starts its own visit...
        joined = await m.same_person(b, "Back yard", "Manoj", sig(99))  # ...until he's recognised there
        assert not m.add_identity(b, "Manoj")  # same visit now: no second arrival alert
        await m.leave(joined, mb, {"person": 1}, [("Manoj", 290, "Back yard")], (1030, 1330), None, ["Manoj"])
        clock.t += inc_mod.IDLE_CLOSE + 1
        closed = await m.sweep()
        return a, b, joined, closed, sent, merged

    a, b, joined, closed, sent, merged = run(go())
    assert joined is a and b.merged_into is a and IncidentManager.current(b) is a
    assert merged == [(b.id, a.id)]  # the yard visit's events move to the porch visit
    assert len(closed) == 1
    kind, message, payload = sent[0]
    assert kind == "incident_summary"
    assert "Route: Front porch -> Back yard" in message
    assert "Seen: 1 person (Manoj)" in message  # one person on two cameras, not two
    assert "waited ~5m at Back yard" in message


def test_same_person_after_the_window_is_a_new_visit():
    async def go():
        m, clock, _, merged = manager()
        a, ma, _ = await m.join(1, "Gate cam", "Front porch")
        await m.same_person(a, "Front porch", "Manoj", None)
        clock.t += inc_mod.JOIN_WINDOW + 20
        b, _, _ = await m.join(3, "Yard cam", "Back yard")
        joined = await m.same_person(b, "Back yard", "Manoj", None)
        return joined is b, merged

    stays, merged = run(go())
    assert stays and merged == []


def test_clothing_decides_only_when_no_face_says_otherwise():
    async def go():
        m, clock, _, _ = manager()
        a, _, _ = await m.join(1, "Gate cam", "Front porch")
        await m.same_person(a, "Front porch", None, sig(7))
        clock.t += 15
        b, _, _ = await m.join(3, "Yard cam", "Back yard")
        different_clothes = await m.same_person(b, "Back yard", None, sig(8))
        same_clothes = await m.same_person(b, "Back yard", None, sig(7))
        return a, b, different_clothes, same_clothes

    a, b, different_clothes, same_clothes = run(go())
    assert different_clothes is b  # unknown person, other clothes: someone else
    assert same_clothes is a  # unknown person, the same clothes seconds later: one visit

    async def faces_overrule():
        m, clock, _, _ = manager()
        a, _, _ = await m.join(1, "Gate cam", "Front porch")
        await m.same_person(a, "Front porch", "Manoj", sig(7))
        clock.t += 15
        b, _, _ = await m.join(3, "Yard cam", "Back yard")
        return await m.same_person(b, "Back yard", "Son", sig(7)), b

    joined, b = run(faces_overrule())
    assert joined is b  # matching clothes, different recognised faces: two people


def test_same_group_never_merges_through_same_person():
    async def go():
        m, clock, _, merged = manager()
        a, _, _ = await m.join(1, "Gate cam", "Front porch")
        await m.same_person(a, "Front porch", "Manoj", None)
        await m.leave(a, a.members[0], {}, [], None, None, [])
        clock.t += inc_mod.SAME_AREA_GAP + inc_mod.IDLE_CLOSE + 5
        await m.sweep()  # the first visit has closed
        b, _, _ = await m.join(2, "Porch cam", "Front porch")
        return await m.same_person(b, "Front porch", "Manoj", None) is b, merged

    stays, merged = run(go())
    assert stays and merged == []


def test_single_camera_visit_keeps_its_own_summary_and_no_visit_summary():
    async def go():
        m, clock, sent, _ = manager()
        a, ma, _ = await m.join(1, "Gate", None)
        own = await m.leave(a, ma, {"person": 1}, [], None, None, [])
        clock.t += inc_mod.IDLE_CLOSE + 1
        await m.sweep()
        return own, sent

    own, sent = run(go())
    assert own is True and sent == []


def test_open_incident_not_closed_while_a_camera_is_still_active():
    async def go():
        m, clock, _, _ = manager()
        a, ma, _ = await m.join(1, "Gate", "Front porch")
        clock.t += 5
        b, mb, _ = await m.join(2, "Porch", "Front porch")
        await m.leave(a, ma, {}, [], None, None, [])
        clock.t += 500
        closed = await m.sweep()
        return closed, a.active

    closed, active = run(go())
    assert closed == [] and active


def test_area_rank_orders_public_to_private():
    assert area_rank("Street") < area_rank("Front Gate") < area_rank("Driveway") < area_rank("Back door") \
        < area_rank("Living room interior")
    assert area_rank("Somewhere") == -1


def test_visit_whose_camera_never_reported_its_end_is_closed_eventually():
    async def go():
        m, clock, _, _ = manager()
        a, ma, _ = await m.join(1, "Gate", None)  # never leaves (worker killed mid-event)
        clock.t += inc_mod.MAX_INCIDENT + inc_mod.IDLE_CLOSE + 1
        return await m.sweep(), m.open

    closed, still_open = run(go())
    assert len(closed) == 1 and still_open == []


# ── storage side ─────────────────────────────────────────────────────────────


def _db(tmp_path):
    import app.models  # noqa: F401
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.session import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(setup())
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_merging_visits_moves_their_events(tmp_path, monkeypatch):
    from sqlalchemy import select

    from app.models.detection import Detection
    from app.models.event import Event
    from app.models.incident import Incident
    from app.services import incident_store

    engine, maker = _db(tmp_path)
    monkeypatch.setattr(incident_store, "AsyncSessionLocal", maker)

    async def go():
        async with maker() as db:
            db.add_all([Incident(id=1, status="open"), Incident(id=2, status="open")])
            db.add_all([Event(camera_id=1, type="motion", message="a", incident_id=1),
                        Event(camera_id=3, type="motion", message="b", incident_id=2, payload={"incident_id": 2})])
            db.add(Detection(camera_id=3, label="person", confidence=90, incident_id=2))
            await db.commit()
        await incident_store.merge_rows(2, 1)
        async with maker() as db:
            events = (await db.execute(select(Event.incident_id))).scalars().all()
            payloads = [e for e in (await db.execute(select(Event.payload))).scalars().all() if e]
            dets = (await db.execute(select(Detection.incident_id))).scalars().all()
            visits = (await db.execute(select(Incident.id))).scalars().all()
        await engine.dispose()
        return events, dets, visits, payloads

    events, dets, visits, payloads = asyncio.run(go())
    assert events == [1, 1] and dets == [1] and visits == [1]
    assert payloads == [{"incident_id": 1}]


def test_moving_a_camera_to_another_group_does_not_restart_it(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.api.routes import cameras as route
    from app.models.camera import Camera
    from app.schemas.camera import CameraUpdate
    from app.services import camera_supervisor

    engine, maker = _db(tmp_path)
    calls = []
    monkeypatch.setattr(camera_supervisor.supervisor, "apply_live", lambda cam: calls.append(("live", cam.area)))

    async def restart(cam):
        calls.append(("restart", cam.name))

    monkeypatch.setattr(camera_supervisor.supervisor, "sync_camera", restart)

    async def go():
        async with maker() as db:
            db.add(Camera(id=1, name="Gate cam", rtsp_main_url="rtsp://x/1"))
            await db.commit()
            await route.update_camera(1, CameraUpdate(area="Front porch"), db=db, _=SimpleNamespace())
            await route.update_camera(1, CameraUpdate(name="Gate"), db=db, _=SimpleNamespace())
        await engine.dispose()

    asyncio.run(go())
    assert calls == [("live", "Front porch"), ("restart", "Gate")]


def test_upgrade_moves_ai_out_of_the_nvr_into_its_ai_service(tmp_path):
    from sqlalchemy import text

    from app.db.schema_sync import migrate_data

    from app.models.ai_settings import AISettings

    engine, maker = _db(tmp_path)

    async def go():
        async with maker() as db:  # an install from before: AI inside the NVR
            db.add(AISettings(id=1, enabled=True, backend="local", remote_url="", remote_api_key=""))
            await db.commit()
        async with engine.begin() as conn:
            await migrate_data(conn)
            row = (await conn.execute(text("SELECT backend, remote_url FROM ai_settings"))).one()
            await conn.execute(text("UPDATE ai_settings SET remote_url = 'http://192.168.1.50:8811'"))
            await migrate_data(conn)  # another PC is left alone
            row2 = (await conn.execute(text("SELECT backend, remote_url FROM ai_settings"))).one()
        await engine.dispose()
        return tuple(row), tuple(row2)

    first, second = asyncio.run(go())
    assert first == ("remote", "http://ai-worker:8811")
    assert second == ("remote", "http://192.168.1.50:8811")
