"""Live-view scrubbing: "what plays at this moment?" (services/playback.py)."""

import asyncio
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.active_segment import ActiveSegmentRegistry
from app.services.ffmpeg_recorder import footage_start
from app.services.playback import merge_spans

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
UTC = timezone.utc


def test_footage_start_is_anchored_on_the_end():
    start = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
    end = start + timedelta(seconds=303)
    # 3s went on connecting: the first frame was at 10:00:03.
    assert footage_start(start, end, 300.0) == start + timedelta(seconds=3)
    assert footage_start(start, end, None) == start
    assert footage_start(start, end, 900.0) == start  # implausible probe: keep the wall clock


def test_merge_spans_joins_back_to_back_segments_and_clips_to_window():
    spans = [(100, 400), (401.5, 700), (720, 800), (0, 50)]
    assert merge_spans(spans, 60, 790) == [[100, 700], [720, 790]]


def test_closed_segment_stays_findable_until_finalized():
    reg = ActiveSegmentRegistry()
    t0 = datetime.now(UTC)
    reg.set(1, "/c/a.mp4", t0)
    reg.close(1, "/c/a.mp4", t0 + timedelta(seconds=300))
    reg.set(1, "/c/b.mp4", t0 + timedelta(seconds=302))
    assert [s.file_path for s in reg.closing(1)] == ["/c/a.mp4"]
    assert reg.get(1).file_path == "/c/b.mp4"
    reg.close(1, "/c/zzz.mp4", t0)  # not the active one: ignored
    assert reg.get(1).file_path == "/c/b.mp4"
    reg.finalized(1, "/c/a.mp4")
    assert reg.closing(1) == []


def _fragmented_clip(path: str, seconds: int) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10:duration={seconds}",
         "-c:v", "libx264", "-g", "10", "-movflags", "+frag_keyframe+empty_moov+default_base_moof", path],
        check=True,
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import app.models  # noqa: F401
    from app.db.session import Base
    from app.services import active_segment, playback
    from app.services.storage_manager import storage_manager

    monkeypatch.setattr(storage_manager, "cache_dir", lambda: str(tmp_path / "cache"))
    reg = ActiveSegmentRegistry()
    monkeypatch.setattr(active_segment, "active_segment_registry", reg)
    monkeypatch.setattr(playback, "active_segment_registry", reg)
    monkeypatch.setattr(playback, "snapshots", playback.SnapshotStore())
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(setup())
    yield tmp_path, reg, async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(engine.dispose())


def _run(maker, fn):
    async def go():
        async with maker() as db:
            return await fn(db)

    return asyncio.run(go())


def test_recording_row_wins_and_gaps_report_where_footage_resumes(env):
    from app.models.recording import Recording
    from app.services import playback

    _tmp, _reg, maker = env
    t0 = time.time() - 1800
    naive = lambda t: datetime.fromtimestamp(t, UTC).replace(tzinfo=None)

    async def add(db):
        db.add(Recording(camera_id=1, file_path="/x/a.mp4", trigger="continuous", started_at=naive(t0),
                         ended_at=naive(t0 + 300), duration_seconds=300, codec="h264"))
        db.add(Recording(camera_id=1, file_path="/x/b.mp4", trigger="continuous", started_at=naive(t0 + 600),
                         ended_at=naive(t0 + 900), duration_seconds=300, codec="h265"))
        await db.commit()

    _run(maker, add)
    hit = _run(maker, lambda db: playback.resolve(db, 1, t0 + 150))
    assert hit["found"] and hit["kind"] == "recording" and abs(hit["origin"] - t0) < 0.01 and hit["codec"] == "h264"
    miss = _run(maker, lambda db: playback.resolve(db, 1, t0 + 400))
    assert not miss["found"] and abs(miss["next_at"] - (t0 + 600)) < 0.01
    other_camera = _run(maker, lambda db: playback.resolve(db, 2, t0 + 150))
    assert not other_camera["found"] and other_camera["next_at"] is None
    tl = _run(maker, lambda db: playback.timeline(db, 1, t0 - 60, t0 + 1000))
    assert tl["spans"] == [[round(t0, 3), round(t0 + 300, 3)], [round(t0 + 600, 3), round(t0 + 900, 3)]]
    floored = _run(maker, lambda db: playback.resolve(db, 1, t0 + 150, not_before=t0 + 700))
    assert floored["found"] and abs(floored["origin"] - (t0 + 600)) < 0.01  # kiosk window


@needs_ffmpeg
def test_closed_but_unregistered_segment_plays_from_a_seekable_snapshot(env):
    from app.services import playback

    tmp, reg, maker = env
    path = str(tmp / "seg.mp4")
    _fragmented_clip(path, 8)
    ended = datetime.now(UTC)
    reg.set(1, path, ended - timedelta(seconds=11))  # wall start includes a 3s connect
    reg.close(1, path, ended)
    at = ended.timestamp() - 4
    ans = _run(maker, lambda db: playback.resolve(db, 1, at))
    assert ans["found"] and ans["kind"] == "segment" and not ans["growing"]
    # The origin comes from the footage itself, not the early wall-clock start.
    assert abs(ans["origin"] - (ended.timestamp() - 8)) < 0.6
    snap = playback.snapshots.get(1, ans["snapshot"])
    head = open(snap.path, "rb").read(64)
    assert b"ftyp" in head and b"moov" in open(snap.path, "rb").read()[:4096]  # faststart = seekable
    again = _run(maker, lambda db: playback.resolve(db, 1, at - 2))
    assert again["snapshot"] == ans["snapshot"]  # a closed segment's snapshot is reused


@needs_ffmpeg
def test_growing_segment_snapshot_refreshes_and_old_ones_expire(env, monkeypatch):
    from app.services import playback

    tmp, reg, maker = env
    path = str(tmp / "live.mp4")
    _fragmented_clip(path, 6)
    reg.set(1, path, datetime.now(UTC) - timedelta(seconds=7))
    first = _run(maker, lambda db: playback.resolve(db, 1, time.time() - 3))
    assert first["found"] and first["growing"]
    same = _run(maker, lambda db: playback.resolve(db, 1, time.time() - 3))
    assert same["snapshot"] == first["snapshot"]  # within the reuse window
    monkeypatch.setattr(playback, "SNAP_REUSE_SECONDS", 0.0)
    newer = _run(maker, lambda db: playback.resolve(db, 1, time.time() - 3))
    assert newer["snapshot"] != first["snapshot"]
    monkeypatch.setattr(playback, "SNAP_KEEP_SECONDS", 0.0)
    _run(maker, lambda db: playback.resolve(db, 1, time.time() - 3))
    snaps_dir = tmp / "cache" / "playback_snaps" / "1"
    assert len([f for f in os.listdir(snaps_dir) if f.endswith(".mp4")]) == 1  # older copies deleted


def test_range_requests_on_a_snapshot(tmp_path):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from app.core.http_range import range_file_response

    p = tmp_path / "f.mp4"
    p.write_bytes(bytes(range(256)) * 40)
    api = FastAPI()

    @api.get("/f")
    def f(request: Request):
        return range_file_response(request, str(p))

    r = TestClient(api).get("/f", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206 and len(r.content) == 100 and r.headers["content-range"] == "bytes 100-199/10240"
