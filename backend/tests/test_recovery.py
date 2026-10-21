import asyncio
import os

from app.services import recovery


class Row(tuple):
    pass


def test_recovery_keeps_known_files_face_crops_and_playback_copies(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    (root / "1").mkdir(parents=True)
    (root / "faces").mkdir()
    keep_video = root / "1" / "a_motion.mp4"
    keep_thumb = root / "1" / "a_motion.jpg"
    keep_sub = root / "1" / "a_motion.sub.mp4"
    orphan = root / "1" / "partial_continuous.mp4"
    face = root / "faces" / "20261021_face.jpg"
    for f in (keep_video, keep_thumb, keep_sub, orphan, face):
        f.write_bytes(b"x")

    class Result:
        def all(self):
            return [(str(keep_video), str(keep_thumb), str(keep_sub))]

    class DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *_a):
            return Result()

    async def no_event(*a, **k):
        return None

    monkeypatch.setattr(recovery, "AsyncSessionLocal", lambda: DB())
    monkeypatch.setattr(recovery, "emit_event", no_event)
    asyncio.run(recovery.cleanup_orphaned_files(str(root)))
    assert keep_video.exists() and keep_thumb.exists() and keep_sub.exists() and face.exists()
    assert not orphan.exists()
