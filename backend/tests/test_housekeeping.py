"""The NVR's own clean-up (services/housekeeping.py) and its side of the host
guardian: old alert pictures go at the age the user set, recordings and files
are kept in step both ways without ever mistaking an unmounted disk for
deleted footage, and the guardian's messages arrive as alerts."""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import housekeeping


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _setup(tmp_path, monkeypatch):
    import app.models  # noqa: F401
    from app.db.session import Base
    from app.services import retention

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    cache, primary = tmp_path / "cache", tmp_path / "primary"
    cache.mkdir()
    primary.mkdir()
    for mod in (housekeeping, retention):
        monkeypatch.setattr(mod, "AsyncSessionLocal", maker)
    sm = housekeeping.storage_manager
    monkeypatch.setattr(sm, "cache_dir", lambda: str(cache))
    monkeypatch.setattr(sm, "primary_dir", lambda: str(primary))
    monkeypatch.setattr(sm, "is_primary_available", lambda: True)
    monkeypatch.setattr(sm, "is_backup_available", lambda: False)
    monkeypatch.setattr(housekeeping, "GUARDIAN_DIR", str(tmp_path / "guardian"))
    events = []

    async def emit(camera_id, kind, message, payload=None, notify=True, **k):
        events.append({"type": kind, "message": message, "payload": payload, "notify": notify})

    monkeypatch.setattr(housekeeping, "emit_event", emit)

    async def create():
        from app.models.camera import Camera

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with maker() as db:
            db.add(Camera(id=1, name="Gate", rtsp_main_url="rtsp://x", recording_mode="continuous", status="online"))
            db.add(Camera(id=2, name="Yard", rtsp_main_url="rtsp://y", recording_mode="motion", status="online"))
            await db.commit()

    asyncio.run(create())
    return maker, cache, primary, events


def _add_recordings(maker, primary, n, hours_ago=1):
    from app.models.recording import Recording

    (primary / "1").mkdir(exist_ok=True)

    async def go():
        async with maker() as db:
            for i in range(n):
                f = primary / "1" / f"seg{i}.mp4"
                f.write_bytes(b"x")
                start = _now() - timedelta(hours=hours_ago, minutes=5 * (n - i))
                db.add(Recording(camera_id=1, file_path=str(f), storage_tier="primary", trigger="continuous",
                                 started_at=start, ended_at=start + timedelta(minutes=5), size_bytes=1))
            await db.commit()

    asyncio.run(go())


def _paths(maker):
    from app.models.recording import Recording

    async def go():
        async with maker() as db:
            return sorted(os.path.basename(p) for p in (await db.execute(select(Recording.file_path))).scalars())

    return asyncio.run(go())


def test_alert_pictures_go_at_the_age_set_but_their_events_stay(tmp_path, monkeypatch):
    from app.models.detection import Detection
    from app.models.event import Event

    maker, cache, _, _ = _setup(tmp_path, monkeypatch)
    (cache / "events").mkdir()
    old, new, shared = (cache / "events" / n for n in ("old.jpg", "new.jpg", "shared.jpg"))
    for f in (old, new, shared):
        f.write_bytes(b"jpeg")

    async def seed():
        async with maker() as db:
            db.add(Event(type="motion", message="old", snapshot_path=str(old), created_at=_now() - timedelta(days=40)))
            db.add(Event(type="motion", message="new", snapshot_path=str(new), created_at=_now() - timedelta(days=5)))
            db.add(Event(type="motion", message="kept for a detection", snapshot_path=str(shared),
                         created_at=_now() - timedelta(days=40)))
            db.add(Detection(camera_id=1, label="person", snapshot_path=str(shared)))
            await db.commit()

    asyncio.run(seed())
    files, size = asyncio.run(housekeeping.prune_snapshots(30))
    assert (files, size) == (1, 4)
    assert not old.exists() and new.exists() and shared.exists()

    async def rows():
        async with maker() as db:
            return {e.message: e.snapshot_path for e in (await db.execute(select(Event))).scalars()}

    left = asyncio.run(rows())
    assert left["old"] is None  # the event stays, without its picture
    assert left["new"] == str(new) and left["kept for a detection"] == str(shared)


def test_recordings_whose_file_has_gone_are_dropped(tmp_path, monkeypatch):
    maker, _, primary, _ = _setup(tmp_path, monkeypatch)
    _add_recordings(maker, primary, 10)
    (primary / "1" / "seg0.mp4").unlink()
    (primary / "1" / "seg1.mp4").unlink()
    result = asyncio.run(housekeeping.reconcile())
    assert result["dropped"] == 2
    assert _paths(maker) == [f"seg{i}.mp4" for i in range(2, 10)]


def test_an_unmounted_disk_is_never_read_as_every_recording_deleted(tmp_path, monkeypatch):
    maker, _, primary, events = _setup(tmp_path, monkeypatch)
    _add_recordings(maker, primary, 30)
    for f in (primary / "1").iterdir():
        f.unlink()  # an empty mount point looks exactly like this
    result = asyncio.run(housekeeping.reconcile())
    assert result["dropped"] == 0
    assert len(_paths(maker)) == 30
    assert events and "is the disk mounted" in events[0]["message"]


def test_a_recording_on_disk_with_no_row_is_added_back(tmp_path, monkeypatch):
    from app.services import ffmpeg_recorder

    maker, _, primary, _ = _setup(tmp_path, monkeypatch)
    _add_recordings(maker, primary, 2)
    stray = primary / "1" / "20261020_101500_continuous.mp4"
    stray.write_bytes(b"x" * 100)
    young = primary / "1" / "20261020_102000_continuous.mp4"
    young.write_bytes(b"x")
    past = (datetime.now() - timedelta(hours=1)).timestamp()
    os.utime(stray, (past, past))

    async def duration(path):
        return 300.0

    monkeypatch.setattr(ffmpeg_recorder, "probe_duration", duration)
    result = asyncio.run(housekeeping.reconcile())
    assert result["registered"] == 1  # the one still being written is left alone
    assert stray.name in _paths(maker) and young.name not in _paths(maker)


def test_the_guardian_learns_what_should_be_recording_and_where(tmp_path, monkeypatch):
    maker, cache, primary, _ = _setup(tmp_path, monkeypatch)
    _add_recordings(maker, primary, 3)
    asyncio.run(housekeeping.write_expectations())
    data = json.loads((tmp_path / "guardian" / "expect.json").read_text())
    assert data["continuous_cameras"] == [1] and data["online_cameras"] == [1]
    assert abs(data["last_recording"]["1"] - (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()) < 120
    assert data["recording_dirs"] == {"cache": str(cache), "primary": str(primary)}


def test_guardian_messages_become_alerts_and_its_deletes_drop_their_rows(tmp_path, monkeypatch):
    maker, _, primary, events = _setup(tmp_path, monkeypatch)
    _add_recordings(maker, primary, 30)
    gone = [str(primary / "1" / f"seg{i}.mp4") for i in range(25)]
    for p in gone:
        os.remove(p)
    outbox = tmp_path / "guardian" / "outbox"
    outbox.mkdir(parents=True)
    (outbox / "1-recovery.json").write_text(json.dumps(
        {"message": "Guardian: restarted lightnvr-backend - not answering.", "kind": "recovery", "notify": True}))
    (outbox / "2-recovery.json").write_text(json.dumps(
        {"message": "Guardian: deleted the 25 oldest recordings - disk full.", "kind": "recovery", "notify": False,
         "deleted_files": gone}))
    (outbox / "3-bad.json").write_text("{not json")

    assert asyncio.run(housekeeping.ingest_guardian_messages()) == 2
    assert [e["notify"] for e in events] == [True, False]
    assert all(e["type"] == "guardian" for e in events)
    assert "deleted_files" not in events[1]["payload"]
    # Exactly the files it deleted - even this many at once, which the
    # unmounted-disk guard would otherwise refuse.
    assert _paths(maker) == [f"seg{i}.mp4" for i in range(25, 30)]
    assert list(outbox.iterdir()) == []


def test_guardian_status_is_shown_only_while_it_is_still_writing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert housekeeping.guardian_status() is None
    d = tmp_path / "guardian"
    d.mkdir(exist_ok=True)
    (d / "status.json").write_text(json.dumps({"at": 1, "problems": []}))
    assert housekeeping.guardian_status()["fresh"] is True
    old = datetime.now().timestamp() - 600
    os.utime(d / "status.json", (old, old))
    assert housekeeping.guardian_status()["fresh"] is False


def test_snapshot_retention_is_validated(tmp_path):
    import pytest
    from pydantic import ValidationError

    from app.schemas.storage import StorageConfigUpdate

    assert StorageConfigUpdate(snapshot_retention_days=30).snapshot_retention_days == 30
    with pytest.raises(ValidationError):
        StorageConfigUpdate(snapshot_retention_days=0)
