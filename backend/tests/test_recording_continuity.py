"""No footage is thrown away by a restart: the segment being recorded is kept
when recording stops, and files a crash left behind are repaired, not deleted."""

import asyncio
import os
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from app.services import ffmpeg_recorder, recovery

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _fragmented_mp4(path, seconds=3):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10",
                    "-t", str(seconds), "-c:v", "libx264", "-g", "10",
                    "-movflags", "+frag_keyframe+empty_moov+default_base_moof", str(path)], check=True)


@needs_ffmpeg
def test_stopping_mid_segment_keeps_what_was_recorded(tmp_path, monkeypatch):
    saved = []

    def live_cmd(url, out, audio, duration):
        # A "camera" that streams in real time, like RTSP does.
        return ["ffmpeg", "-y", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
                "-t", str(duration), "-c:v", "libx264", "-g", "10",
                "-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-flush_packets", "1", out]

    async def register(camera_id, path, trigger, started, ended, playback_path=None):
        saved.append((camera_id, path, trigger, (ended - started).total_seconds()))
        return object()

    async def nothing(*a, **k):
        return None

    async def room():
        return True, "ok"

    monkeypatch.setattr(ffmpeg_recorder, "_build_record_cmd", live_cmd)
    monkeypatch.setattr(ffmpeg_recorder, "_camera_dir", lambda cid: str(tmp_path))
    monkeypatch.setattr(ffmpeg_recorder, "register_recording", register)
    monkeypatch.setattr(ffmpeg_recorder, "mark_online", nothing)
    monkeypatch.setattr(ffmpeg_recorder.storage_manager, "cache_has_room", room)

    async def go():
        rec = ffmpeg_recorder.ContinuousRecorder(7, "rtsp://cam", has_audio=False)
        task = asyncio.create_task(rec.run())
        await asyncio.sleep(4)
        await rec.stop()  # an update / restart / camera edit, 4 s into a 5-minute segment
        await asyncio.wait_for(task, 5)

    asyncio.run(go())
    assert len(saved) == 1
    camera_id, path, trigger, seconds = saved[0]
    assert camera_id == 7 and trigger == "continuous" and os.path.exists(path)
    assert seconds > 1.5  # the seconds recorded before the stop are kept, not discarded


@needs_ffmpeg
def test_files_left_by_a_crash_are_repaired_and_kept(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    cam = root / "1"
    cam.mkdir(parents=True)
    (root / "faces").mkdir()
    known = cam / "20261026_100000_continuous.mp4"
    cut_short = cam / "20261026_112530_continuous.mp4"
    empty = cam / "20261026_113000_continuous.mp4"
    playback_copy = cam / "20261026_112530_continuous.sub.mp4"
    half_remux = cam / "20261026_112530_continuous.mp4.seek.tmp"
    face = root / "faces" / "20261021_face.jpg"
    _fragmented_mp4(cut_short)
    for f in (known, playback_copy, half_remux, face):
        f.write_bytes(b"x")
    empty.write_bytes(b"")
    garbage = cam / "20261026_114000_continuous.mp4"
    garbage.write_bytes(b"not a video")
    gone_cam = root / "9"
    gone_cam.mkdir()
    (gone_cam / "20261026_112530_continuous.mp4").write_bytes(b"x")

    registered, events = [], []

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return self.rows

        def scalars(self):
            return self

    class DB:
        calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *_a):
            DB.calls += 1
            return Result([(str(known), None, None)] if DB.calls == 1 else [1])

    async def register(camera_id, path, trigger, started, ended, playback_path=None):
        registered.append((camera_id, path, trigger, started, ended))
        return object()

    async def event(*a, **k):
        events.append((a, k))

    monkeypatch.setattr(recovery, "AsyncSessionLocal", lambda: DB())
    monkeypatch.setattr(recovery, "emit_event", event)
    monkeypatch.setattr("app.services.recording_registry.register_recording", register)

    async def go():
        task = await recovery.cleanup_orphaned_files(str(root))
        await task

    asyncio.run(go())
    assert [r[1] for r in registered] == [str(cut_short)] and cut_short.exists()  # kept, not deleted
    cam_id, _, trigger, started, ended = registered[0]
    assert cam_id == 1 and trigger == "continuous" and 2 <= (ended - started).total_seconds() <= 4
    assert known.exists() and face.exists()  # never touched
    assert not empty.exists() and not playback_copy.exists() and not half_remux.exists() and not garbage.exists()
    assert not (gone_cam / "20261026_112530_continuous.mp4").exists()  # its camera was deleted
    # Keeping footage is quiet; only a real loss is an alert.
    assert any(k.get("notify") is False and "Kept 1" in a[2] for a, k in events)
    assert any("unreadable" in a[2] and k.get("notify", True) for a, k in events)


def test_recording_time_comes_from_the_file_name():
    when, trigger = recovery._recorded_at("20261026_112530_motion.mp4")
    assert when == datetime(2026, 10, 26, 11, 25, 30, tzinfo=timezone.utc) and trigger == "motion"
    assert recovery._recorded_at("odd.mp4") == (None, "continuous")
