"""The disk filling up (Docker images, other apps) never stops recording: the
oldest footage makes room, recent footage is never touched, and the warning
isn't repeated every half hour."""

import asyncio
from collections import namedtuple
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import retention

GB = 1024**3
Usage = namedtuple("Usage", "total used free")


def _setup(tmp_path, monkeypatch, free_gb, total_gb=300):
    import app.models  # noqa: F401
    from app.db.session import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    cache, primary = tmp_path / "cache", tmp_path / "primary"
    cache.mkdir()
    primary.mkdir()
    monkeypatch.setattr(retention, "AsyncSessionLocal", maker)
    monkeypatch.setattr(retention.storage_manager, "cache_dir", lambda: str(cache))
    monkeypatch.setattr(retention.storage_manager, "primary_dir", lambda: str(primary))
    monkeypatch.setattr(retention.storage_manager, "is_primary_available", lambda: True)
    monkeypatch.setattr(retention.storage_manager, "is_backup_available", lambda: False)
    state = {"free": free_gb * GB}
    monkeypatch.setattr(retention.shutil, "disk_usage",
                        lambda p: Usage(total_gb * GB, total_gb * GB - state["free"], state["free"]))
    events = []

    async def emit(camera_id, kind, message, payload=None, **k):
        from app.models.event import Event

        events.append((kind, message, payload))
        async with maker() as db:
            db.add(Event(camera_id=camera_id, type=kind, message=message, payload=payload))
            await db.commit()

    monkeypatch.setattr(retention, "emit_event", emit)

    async def create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(create())
    return maker, primary, state, events


def _recordings(maker, primary, ages_hours, size_gb=5):
    from app.models.recording import Recording

    async def go():
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with maker() as db:
            for i, age in enumerate(ages_hours):
                f = primary / f"{i}.mp4"
                f.write_bytes(b"x")
                db.add(Recording(id=i + 1, camera_id=1, file_path=str(f), storage_tier="primary", trigger="continuous",
                                 started_at=now - timedelta(hours=age), ended_at=now - timedelta(hours=age) + timedelta(minutes=5),
                                 size_bytes=size_gb * GB))
            await db.commit()

    asyncio.run(go())


def _left(maker):
    from app.models.recording import Recording

    async def go():
        async with maker() as db:
            return sorted((await db.execute(select(Recording.id))).scalars().all())

    return asyncio.run(go())


def test_oldest_footage_makes_room_before_the_disk_fills(tmp_path, monkeypatch):
    maker, primary, state, events = _setup(tmp_path, monkeypatch, free_gb=10)
    _recordings(maker, primary, ages_hours=[72, 70, 48, 30, 1])
    removed = asyncio.run(retention.ensure_free_space())
    # Floor 15 GB (+2 GB margin) with 10 GB free: the two oldest 5 GB files go.
    assert removed == 2 and _left(maker) == [3, 4, 5]
    assert not (primary / "0.mp4").exists() and (primary / "4.mp4").exists()
    state["free"] = 40 * GB
    assert asyncio.run(retention.ensure_free_space()) == 0  # enough room: nothing touched


def test_recent_footage_is_never_removed_to_make_room(tmp_path, monkeypatch):
    maker, primary, state, events = _setup(tmp_path, monkeypatch, free_gb=5)
    monkeypatch.setattr(retention, "_full_of_other_warned_at", None)
    _recordings(maker, primary, ages_hours=[20, 10, 1])
    assert asyncio.run(retention.ensure_free_space()) == 0 and _left(maker) == [1, 2, 3]
    assert [e[0] for e in events] == ["system"] and "something else is filling it" in events[0][1]


def test_low_storage_warning_once_then_only_when_worse_and_when_recovered(tmp_path, monkeypatch):
    from app.models.alert_settings import AlertSettings

    maker, primary, state, events = _setup(tmp_path, monkeypatch, free_gb=18.6)  # 6.2% of 300 GB

    async def settings():
        async with maker() as db:
            db.add(AlertSettings(id=1, low_storage_alerts_enabled=True, low_storage_threshold_percent=10))
            await db.commit()

    asyncio.run(settings())

    def check(free_gb):
        state["free"] = free_gb * GB
        before = len(events)
        asyncio.run(retention._check_low_storage())
        return events[before:]

    first = check(18.6)
    assert len(first) == 1 and "6.2% free" in first[0][1] and "keep 15GB free" in first[0][1]
    assert check(18.0) == [] and check(18.6) == []  # the next half-hourly passes: quiet
    assert len(check(12.0)) == 1  # 2 points worse: worth telling
    back = check(40.0)  # 13.3% - comfortably above 10%
    assert len(back) == 1 and "OK again" in back[0][1] and back[0][2]["cleared"]
    assert check(40.0) == []


def test_update_removes_only_images_no_container_uses(tmp_path):
    import os
    import shutil
    import subprocess
    import textwrap

    import pytest

    lib = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "lib-env.sh")
    if shutil.which("bash") is None or not os.path.exists(lib):
        pytest.skip("no bash")
    log = tmp_path / "docker.log"
    stub = tmp_path / "docker"
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "$@" >> "{log}"
        case "$1 $2" in
          "ps -a") printf '%s\n' ghcr.io/manojmkss/light_nvr-backend:0.4.13 ghcr.io/manojmkss/light_nvr-ai-worker:0.4.13-openvino nginx:1.27-alpine ;;
          "image ls") printf '%s\n' ghcr.io/manojmkss/light_nvr-backend:0.4.12 ghcr.io/manojmkss/light_nvr-backend:0.4.13 \
                        ghcr.io/manojmkss/light_nvr-backend:latest ghcr.io/manojmkss/light_nvr-ai-worker:0.4.13-openvino \
                        nginx:1.26-alpine python:3.11-slim ;;
        esac
        exit 0
        """))
    stub.chmod(0o755)
    script = f'set -euo pipefail; ENV_FILE="{tmp_path}/.env"; DOCKER=("{stub}"); source "{lib}"; prune_old_images; echo "N=$PRUNED_IMAGES"'
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    removed = [line.split()[-1] for line in log.read_text().splitlines() if line.startswith("image rm")]
    assert removed == ["ghcr.io/manojmkss/light_nvr-backend:0.4.12", "ghcr.io/manojmkss/light_nvr-backend:latest"]
    assert "N=2" in out.stdout  # in-use images, and anything not LightNVR's, are left alone
