"""Rolling buffer + motion clip assembly against a generated video read at
real-time speed (ffmpeg -re), standing in for an RTSP camera."""

import asyncio
import json
import os
import shutil
import subprocess
import time

import pytest

from app.services import ffmpeg_recorder, segment_buffer
from app.services.storage_manager import storage_manager

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture
def camera_source(tmp_path):
    """60s 15fps H.264 with a 1s GOP, so segments close every ~2s."""
    path = tmp_path / "camera.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15:duration=60",
         "-c:v", "libx264", "-g", "15", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )
    return str(path)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    root.mkdir()
    monkeypatch.setattr(storage_manager, "cache_dir", lambda: str(root))

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(segment_buffer, "mark_offline", noop)
    return root


def _probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
        check=True, capture_output=True, text=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def test_buffer_rotates_contiguous_segments_and_prunes(camera_source, cache, monkeypatch):
    monkeypatch.setattr(segment_buffer.SegmentBuffer, "PRUNE_MARGIN", 0.0)

    async def scenario():
        buf = segment_buffer.SegmentBuffer(1, camera_source, None, keep_seconds=2.0)
        task = asyncio.create_task(buf.run())
        seen: list = []
        try:
            for _ in range(100):
                await asyncio.sleep(0.1)
                for s in buf.segments:
                    if s not in seen:
                        seen.append(s)
                if len(seen) >= 5:
                    break
        finally:
            await buf.stop()
            await asyncio.gather(task, return_exceptions=True)

        assert len(seen) >= 5
        for a, b in zip(seen, seen[1:]):
            assert abs(b.start - a.end) < 0.05  # back-to-back, no gap or overlap
            assert 1.5 < a.duration < 2.5
        # Wall times are close to real time (segment end ~ when it closed).
        assert abs(seen[-1].end - time.time()) < 3.0
        # Pruned: only segments within keep_seconds (+ the one spanning it) remain on disk.
        files = [f for f in os.listdir(buf.directory) if f.endswith(".ts")]
        assert len(files) <= 4

    asyncio.run(scenario())


def test_motion_clip_includes_pre_and_post_roll(camera_source, cache, monkeypatch):
    saved: list = []

    async def fake_register(camera_id, file_path, trigger, started_at, ended_at, playback_path=None, thumbnail_at=0.5):
        saved.append((file_path, trigger, started_at.timestamp(), ended_at.timestamp(), thumbnail_at))
        return object()

    monkeypatch.setattr(ffmpeg_recorder, "register_recording", fake_register)

    async def scenario():
        rec = ffmpeg_recorder.MotionRecorder(1, camera_source, False, pre_roll_seconds=3, post_roll_seconds=2)
        await rec.start()
        try:
            await asyncio.sleep(7)  # buffer fills
            t0 = time.time()
            await rec.on_motion_start()
            await asyncio.sleep(3)
            await rec.on_motion_start()  # re-trigger mid-event: same clip
            await asyncio.sleep(1)
            t_stop = time.time()
            await rec.on_motion_stop()
            for _ in range(150):
                await asyncio.sleep(0.1)
                if saved:
                    break
        finally:
            await rec.stop()
        return t0, t_stop

    t0, t_stop = asyncio.run(scenario())
    assert len(saved) == 1
    path, trigger, started, ended, thumb_at = saved[0]
    assert trigger == "motion"
    assert os.path.exists(path) and path.endswith("_motion.mp4")
    # Starts at least pre_roll before the trigger (segment granularity may add up to one segment).
    assert t0 - 3 - 2.5 <= started <= t0 - 3 + 0.05
    # Ends no earlier than stop + post_roll.
    assert ended >= t_stop + 2 - 0.05
    # File really holds that span of footage.
    assert abs(_probe_duration(path) - (ended - started)) < 0.6
    # Thumbnail taken where motion was detected, not at the start of the pre-roll.
    assert abs(thumb_at - (t0 - started)) < 0.1


def test_hevc_with_audio_clip_keeps_both_streams(tmp_path, cache, monkeypatch):
    src = tmp_path / "hevc_audio.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15:duration=40",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000:duration=40",
         "-c:v", "libx265", "-x265-params", "keyint=15:log-level=error", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(src)],
        check=True,
    )
    saved: list = []

    async def fake_register(camera_id, file_path, *_a, **_k):
        saved.append(file_path)
        return object()

    monkeypatch.setattr(ffmpeg_recorder, "register_recording", fake_register)

    async def scenario():
        rec = ffmpeg_recorder.MotionRecorder(1, str(src), True, pre_roll_seconds=2, post_roll_seconds=1)
        await rec.start()
        try:
            await asyncio.sleep(5)
            await rec.on_motion_start()
            await asyncio.sleep(2)
            await rec.on_motion_stop()
            for _ in range(150):
                await asyncio.sleep(0.1)
                if saved:
                    break
        finally:
            await rec.stop()

    asyncio.run(scenario())
    assert len(saved) == 1
    streams = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", saved[0]],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    assert "hevc" in streams and "aac" in streams


def test_stop_cleans_buffer_dir(camera_source, cache):
    async def scenario():
        rec = ffmpeg_recorder.MotionRecorder(1, camera_source, False, pre_roll_seconds=2, post_roll_seconds=1)
        await rec.start()
        await asyncio.sleep(3)
        directory = rec.buffer.directory
        assert os.path.isdir(directory)
        await rec.stop()
        return directory

    directory = asyncio.run(scenario())
    assert not os.path.exists(directory)


def test_stalled_ffmpeg_is_killed_and_restarted(tmp_path, cache, monkeypatch):
    """An input that keeps ffmpeg alive but never delivers video (a FIFO with
    no writer) must not leave the buffer silently empty forever."""
    fifo = tmp_path / "silent.ts"
    os.mkfifo(fifo)
    monkeypatch.setattr(segment_buffer.SegmentBuffer, "STALL_SECONDS", 1.5)

    async def scenario():
        buf = segment_buffer.SegmentBuffer(1, str(fifo), None)
        task = asyncio.create_task(buf.run())
        await asyncio.sleep(6)
        runs = buf._run_no
        await buf.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return runs

    assert asyncio.run(scenario()) >= 2  # killed after the stall, then retried
