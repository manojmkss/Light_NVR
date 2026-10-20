"""Smoke test for StreamViewer against a real (generated) video file: frames
reach the frame bus at native resolution, the live JPEG keeps the source
aspect ratio, and stopping clears the camera's frames."""

import asyncio
import shutil
import subprocess

import cv2
import numpy as np
import pytest

from app.services import stream_viewer as sv
from app.services.frame_bus import frame_bus

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture
def clip_4x3(tmp_path):
    path = tmp_path / "testsrc.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=800x600:rate=15:duration=20",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )
    return str(path)


async def _noop(*_args, **_kwargs):
    return None


def test_frames_reach_bus_with_correct_aspect(clip_4x3, monkeypatch):
    monkeypatch.setattr(sv, "mark_online", _noop)
    monkeypatch.setattr(sv, "mark_offline", _noop)

    async def scenario():
        viewer = sv.StreamViewer(99, clip_4x3, motion_enabled=True, sensitivity=50)
        await viewer.start()
        try:
            for _ in range(50):
                await asyncio.sleep(0.1)
                if frame_bus.get_latest(99) and frame_bus.get_fresh(99):
                    break
            raw = frame_bus.get_fresh(99)
            live = frame_bus.get_latest(99)
            assert raw is not None and live is not None
            assert raw[0].shape[:2] == (600, 800)  # native, not downscaled
            img = cv2.imdecode(np.frombuffer(live, np.uint8), cv2.IMREAD_COLOR)
            assert img.shape[:2] == (480, 640)  # 4:3 kept, not squashed to 640x360
            assert viewer.last_sample is not None  # detector is being fed
        finally:
            await viewer.stop()
        assert frame_bus.get_fresh(99) is None
        assert frame_bus.get_latest(99) is None

    asyncio.run(scenario())
