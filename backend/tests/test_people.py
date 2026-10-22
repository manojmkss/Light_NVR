"""People page: trust levels, arrival notifications, grouping of unknown faces."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.services.faces import group_faces, recognition_strength
from app.services.notify_policy import decide_with


def _emb(seed: int, noise: float = 0.0, base: int | None = None) -> bytes:
    rng = np.random.default_rng(seed)
    v = np.random.default_rng(base).normal(size=128) if base is not None else rng.normal(size=128)
    v = v + rng.normal(size=128) * noise
    return (v / np.linalg.norm(v)).astype(np.float32).tobytes()


def _sample(i, emb, track=None):
    return SimpleNamespace(id=i, embedding=emb, track_uid=track)


def test_same_persons_sightings_group_together_and_others_stay_apart():
    samples = [
        _sample(1, _emb(1, 0.3, base=100)),  # milkman, 3 sightings
        _sample(2, _emb(2, 0.3, base=200)),  # someone else
        _sample(3, _emb(3, 0.3, base=100)),
        _sample(4, _emb(4, 0.3, base=100)),
        _sample(5, _emb(5, 0.3, base=200)),
    ]
    groups = sorted(sorted(g.ids) for g in group_faces(samples, 0.45))
    assert groups == [[1, 3, 4], [2, 5]]


def test_captures_from_one_track_always_group():
    # Two very different angles of the same walk-past still belong together.
    samples = [_sample(1, _emb(1), track="t1"), _sample(2, _emb(2), track="t1"), _sample(3, _emb(3))]
    groups = sorted(sorted(g.ids) for g in group_faces(samples, 0.45))
    assert groups == [[1, 2], [3]]


def test_recognition_strength_words():
    assert recognition_strength([])[0] == "weak"
    assert recognition_strength([0.8, 0.8])[0] == "weak"
    assert recognition_strength([0.8, 0.7, 0.5])[0] == "good"
    assert recognition_strength([0.8] * 6)[0] == "strong"
    assert recognition_strength([0.4] * 8)[0] == "good"  # many but blurry


def rule(**kw):
    base = dict(id=1, name="r", position=0, enabled=True, modes=None, camera_ids=None, kinds=None, objects=None,
                zones=None, identities=None, action="notify", cooldown_s=None, webhook_url=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_recognised_stranger_counts_as_unknown_for_rules():
    stranger = {"objects": ["person"], "identities": ["person:4"], "categories": ["stranger"]}
    urgent_unknown = [rule(identities=["unknown"], action="notify_high")]
    assert decide_with(urgent_unknown, "motion", 1, "home", stranger).action == "notify_high"
    quiet_known = [rule(identities=["known"], action="silent")]
    assert decide_with(quiet_known, "motion", 1, "home", stranger).action == "notify"


def test_old_regular_and_visitor_rules_match_familiar_people():
    familiar = {"objects": ["person"], "identities": ["person:2"], "categories": ["familiar"]}
    assert decide_with([rule(identities=["staff"], action="silent")], "arrival", 1, "home", familiar).action == "silent"


def test_suspicious_is_urgent_even_if_a_rule_would_silence_it():
    sus = {"objects": ["person"], "identities": ["person:9"], "categories": ["watchlist"]}
    assert decide_with([rule(action="silent")], "arrival", 1, "home", sus).action == "notify_high"


def test_arrival_notify_choice(monkeypatch):
    from app.services import camera_worker, security_mode

    mode = {"mode": "home"}

    async def current_mode():
        return mode

    monkeypatch.setattr(security_mode, "current_mode", current_mode)
    person = lambda **kw: SimpleNamespace(**{"category": "familiar", "notify": "always", "arrival_alert": True, **kw})
    want = lambda p: asyncio.run(camera_worker._wants_arrival(p))
    assert want(person()) is True
    assert want(person(notify="never")) is False
    assert want(person(notify="away")) is False
    mode["mode"] = "away"
    assert want(person(notify="away")) is True
    assert want(person(notify="never", category="watchlist")) is True  # suspicious: always
    assert want(person(notify=None, arrival_alert=False)) is False  # older rows


def test_upgrade_merges_regular_and_visitor_and_keeps_alerts_off(tmp_path):
    import app.models  # noqa: F401
    from app.db.schema_sync import migrate_data, sync_missing_columns
    from app.db.session import Base

    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE persons (id INTEGER PRIMARY KEY, name VARCHAR(128), note VARCHAR(256), "
                "category VARCHAR(16), arrival_alert BOOLEAN, arrival_cooldown_s INTEGER, created_at DATETIME)"
            ))
            now = datetime.now(timezone.utc).isoformat()
            await conn.execute(text(
                f"INSERT INTO persons VALUES (1,'A',NULL,'staff',1,1800,'{now}'), (2,'B',NULL,'visitor',0,1800,'{now}'), "
                f"(3,'C',NULL,'household',1,1800,'{now}')"
            ))
            await conn.run_sync(Base.metadata.create_all)
            await sync_missing_columns(conn)
            await migrate_data(conn)
            await migrate_data(conn)  # idempotent
            rows = (await conn.execute(text("SELECT id, category, notify FROM persons ORDER BY id"))).fetchall()
        await engine.dispose()
        return [tuple(r) for r in rows]

    assert asyncio.run(go()) == [(1, "familiar", "always"), (2, "familiar", "never"), (3, "household", "always")]


def test_face_capture_reasons_are_counted_per_camera():
    from app.services.faces import FaceService
    from app.vision.face import Face

    svc = FaceService()
    svc._loaded = True
    emb = np.ones(128, dtype=np.float32) / np.sqrt(128)
    frame = np.zeros((1000, 2000, 3), dtype=np.uint8)
    faces = [
        Face(0.11, 0.11, 0.01, 0.02, 0.9, 0.8, emb),  # 20px wide: too small
        Face(0.51, 0.11, 0.03, 0.05, 0.9, 0.2, emb),  # 60px but blurry
        Face(0.81, 0.11, 0.03, 0.05, 0.9, 0.8, emb),  # usable
    ]

    async def find(settings, img, boxes, keep_crops=True, min_face=40):
        return faces

    svc.find = find
    boxes = [[0.1, 0.1, 0.1, 0.5], [0.5, 0.1, 0.1, 0.5], [0.8, 0.1, 0.1, 0.5], [0.3, 0.1, 0.05, 0.5]]
    out = asyncio.run(svc.recognise(SimpleNamespace(face_threshold=60), frame, boxes, camera_id=7))
    assert [i for i, _f, _m in out] == [2]
    st = svc.capture_stats()[7]
    assert (st["checked"], st["usable"], st["too_small"], st["low_quality"], st["no_face"]) == (4, 1, 1, 1, 1)
    assert st["frame_width"] == 2000


def test_forget_all_keeps_family_unless_asked(tmp_path, monkeypatch):
    import app.models  # noqa: F401
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.api.routes import faces as route
    from app.db.session import Base
    from app.models.person import FaceSample, Person
    from app.services import events

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(route.face_service, "reload", noop)
    monkeypatch.setattr(events, "emit_event", noop)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    user = SimpleNamespace(username="admin")

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with maker() as db:
            db.add_all([Person(id=1, name="Son", category="household"), Person(id=2, name="Milkman", category="familiar")])
            db.add_all([FaceSample(person_id=1, embedding=b"x"), FaceSample(person_id=2, embedding=b"x"),
                        FaceSample(person_id=None, embedding=b"x")])
            await db.commit()
            r = await route.forget_all(include_family=False, db=db, user=user)
            people = (await db.execute(select(Person.name))).scalars().all()
            samples = (await db.execute(select(FaceSample.person_id))).scalars().all()
            r2 = await route.forget_all(include_family=True, db=db, user=user)
            left = (await db.execute(select(Person))).scalars().all()
        await engine.dispose()
        return r, people, samples, r2, left

    r, people, samples, r2, left = asyncio.run(go())
    assert r == {"deleted_samples": 2, "kept_family": 1}
    assert people == ["Son"] and samples == [1]
    assert r2["kept_family"] == 0 and left == []


def test_face_moment_falls_back_for_older_samples():
    from datetime import timedelta

    from app.api.routes.faces import _seen

    saved = datetime(2026, 1, 1, 10, 0, 30)
    assert _seen(SimpleNamespace(seen_at=None, created_at=saved)) == saved - timedelta(seconds=15)
    exact = datetime(2026, 1, 1, 10, 0, 2)
    assert _seen(SimpleNamespace(seen_at=exact, created_at=saved)) == exact
