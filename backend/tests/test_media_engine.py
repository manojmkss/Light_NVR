"""The media engine (services/media_engine.py + fmp4.py): live and playback as
fragmented MP4 over one websocket, the camera's own video, never re-encoded."""

import asyncio
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import fmp4, media_engine
from app.services.active_segment import ActiveSegmentRegistry

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
UTC = timezone.utc


def _clip(path, seconds, codec="libx264", fps=10, gop=10, audio=None, fragmented=False, extra=()):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=320x240:rate={fps}:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=8000:duration={seconds}"]
    cmd += ["-c:v", codec, "-pix_fmt", "yuv420p"]
    cmd += ["-x265-params", f"keyint={gop}:min-keyint={gop}:scenecut=0:bframes=0:open-gop=0:log-level=error"] if codec == "libx265" else ["-g", str(gop)]
    if audio:
        cmd += ["-c:a", audio]
    if fragmented:
        cmd += ["-movflags", "+frag_keyframe+empty_moov+default_base_moof"]
    cmd += [*extra, str(path)]
    subprocess.run(cmd, check=True)
    return str(path)


async def _fragments(cmd):
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    reader = fmp4.FragmentReader(proc.stdout)
    init, info = await reader.init()
    frags = []
    try:
        while True:
            frags.append(await reader.fragment())
    except asyncio.IncompleteReadError:
        pass
    await proc.wait()
    return init, info, frags


def _remux_every_frame(src):
    return ["ffmpeg", "-loglevel", "error", "-i", src, "-c", "copy", "-f", "mp4",
            "-movflags", "empty_moov+default_base_moof+frag_every_frame", "pipe:1"]


# ── fragmented MP4 ─────────────────────────────────────────────────────────


@needs_ffmpeg
def test_h264_codec_string_and_a_keyframe_every_gop(tmp_path):
    src = _clip(tmp_path / "a.mp4", 4, gop=10)
    init, info, frags = asyncio.run(_fragments(_remux_every_frame(src)))
    assert re.fullmatch(r"avc1\.[0-9A-F]{6}", info.codecs)
    assert info.audio is None and len(frags) == 40
    keys = [i for i, (_, f) in enumerate(frags) if f.key]
    assert keys == [0, 10, 20, 30]
    times = [f.time for _, f in frags]
    assert times == sorted(times) and abs(times[10] - 1.0) < 0.01


@needs_ffmpeg
def test_hevc_is_offered_to_the_browser_as_hvc1(tmp_path):
    src = _clip(tmp_path / "h.mp4", 3, codec="libx265", gop=10, extra=("-tag:v", "hev1"))
    init, info, frags = asyncio.run(_fragments(_remux_every_frame(src)))
    assert info.video.fourcc == b"hvc1" and b"hev1" not in init and b"hvc1" in init
    assert re.fullmatch(r"hvc1\.[A-C]?\d+\.[0-9A-F]+\.[LH]\d+(\.[0-9A-F]+)*", info.codecs), info.codecs
    assert [i for i, (_, f) in enumerate(frags) if f.key][:3] == [0, 10, 20]


@needs_ffmpeg
def test_audio_track_gets_an_mp4a_codec_string(tmp_path):
    src = _clip(tmp_path / "s.mp4", 2, audio="aac")
    _, info, _ = asyncio.run(_fragments(_remux_every_frame(src)))
    assert info.codecs.endswith(",mp4a.40.2")


@needs_ffmpeg
def test_where_a_fragmented_file_ends_is_read_from_its_tail(tmp_path):
    src = _clip(tmp_path / "f.mp4", 6, fragmented=True)
    assert abs(media_engine._last_fragment_end(src) - 6.0) < 0.15


# ── playback ──────────────────────────────────────────────────────────────


class FakeWS:
    def __init__(self):
        self.sent: list = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.new = asyncio.Event()

    async def send_json(self, msg):
        self.sent.append(msg)
        self.new.set()

    async def send_bytes(self, data):
        self.sent.append(bytes(data))
        self.new.set()

    async def receive_json(self):
        return await self.inbox.get()

    def messages(self, kind=None):
        return [m for m in self.sent if isinstance(m, dict) and (kind is None or m.get("type") == kind)]

    def frag_walls(self):
        """Wall time of every fragment sent, decoded as a browser would."""
        out, info, origin = [], None, None
        it = iter(self.sent)
        for m in it:
            if isinstance(m, dict) and m.get("type") == "init":
                origin = m["origin"] + m["offset"]
                info = fmp4.parse_init(bytearray(next(it)))
            elif isinstance(m, bytes) and info is not None:
                moof = m[:int.from_bytes(m[:4], "big")]
                f = fmp4.parse_fragment(moof, info)
                if f.time is not None:
                    out.append(origin + f.time)
        return out


async def wait_for(cond, timeout=20.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.05)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import app.models  # noqa: F401
    from app.db.session import Base
    from app.services import playback

    reg = ActiveSegmentRegistry()
    monkeypatch.setattr(media_engine, "active_segment_registry", reg)
    monkeypatch.setattr(playback, "active_segment_registry", reg)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(media_engine, "AsyncSessionLocal", maker)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        from app.models.camera import Camera

        async with maker() as db:
            db.add(Camera(id=1, name="c", rtsp_main_url="rtsp://x/main", enabled=True))
            await db.commit()

    asyncio.run(setup())
    yield tmp_path, reg, maker
    asyncio.run(engine.dispose())


def _add_recording(maker, path, start, seconds):
    from app.models.recording import Recording

    async def go():
        async with maker() as db:
            db.add(Recording(camera_id=1, file_path=str(path), trigger="continuous",
                             started_at=datetime.fromtimestamp(start, UTC).replace(tzinfo=None),
                             ended_at=datetime.fromtimestamp(start + seconds, UTC).replace(tzinfo=None),
                             duration_seconds=seconds, size_bytes=os.path.getsize(path)))
            await db.commit()

    asyncio.run(go())


@needs_ffmpeg
def test_playback_runs_ahead_only_so_far_then_follows_the_viewer_across_files(env, monkeypatch):
    tmp, reg, maker = env
    monkeypatch.setattr(media_engine, "AHEAD_SECONDS", 4.0)
    t0 = time.time() - 3600
    _add_recording(maker, _clip(tmp / "r1.mp4", 10), t0, 10)
    _add_recording(maker, _clip(tmp / "r2.mp4", 10), t0 + 11, 10)  # 1 s gap: played straight across

    async def scenario():
        ws = FakeWS()
        session = media_engine.MediaSession(ws, media_engine.Access())
        await session.handle({"op": "play", "camera": 1, "at": t0 + 2, "audio": False})
        await wait_for(lambda: ws.frag_walls())
        await asyncio.sleep(1.5)
        walls = ws.frag_walls()
        # From the keyframe before the asked moment (one per second here) - not
        # from the start of the file.
        assert t0 + 2 - 1.05 <= walls[0] <= t0 + 2 + 0.01
        assert max(walls) <= t0 + 2 + 4.0 + 1.1  # not the whole file: ~AHEAD past the screen
        inits = ws.messages("init")
        assert inits[0]["reset"] is True and inits[0]["origin"] == pytest.approx(t0)
        # The viewer watches on: the engine follows into the next file.
        for wall in (t0 + 6, t0 + 10, t0 + 14, t0 + 18, t0 + 22):
            await session.handle({"op": "pos", "wall": wall})
            await asyncio.sleep(0.4)
        await wait_for(lambda: ws.messages("gap"))
        second = ws.messages("init")[1]
        assert second["reset"] is False and second["offset"] == pytest.approx(11.0)
        walls = ws.frag_walls()
        assert max(walls) > t0 + 19 and walls == sorted(walls)
        assert ws.messages("gap")[-1]["next_at"] is None  # nothing after: the end
        await session._cancel_job()

    asyncio.run(scenario())


@needs_ffmpeg
def test_footage_after_a_skipped_gap_is_sent_without_waiting_for_the_viewer(env, monkeypatch):
    """Pacing is against the viewer's position; a gap the engine plays across
    moves that position too - else the footage after it waits for a position
    the viewer can't reach without it (seen in the soak: no picture)."""
    tmp, reg, maker = env
    monkeypatch.setattr(media_engine, "AHEAD_SECONDS", 4.0)
    t0 = time.time() - 3600
    _add_recording(maker, _clip(tmp / "a.mp4", 4), t0, 4)
    _add_recording(maker, _clip(tmp / "b.mp4", 10), t0 + 14, 10)  # 10 s gap: played across

    async def scenario():
        ws = FakeWS()
        session = media_engine.MediaSession(ws, media_engine.Access())
        await session.handle({"op": "play", "camera": 1, "at": t0 + 1, "audio": False})  # no position reports
        await wait_for(lambda: any(w >= t0 + 14 for w in ws.frag_walls()), timeout=10)
        await asyncio.sleep(1)
        assert max(ws.frag_walls()) <= t0 + 14 + 4.0 + 1.1  # and still paced from there
        await session._cancel_job()

    asyncio.run(scenario())


@needs_ffmpeg
def test_playback_follows_the_segment_still_being_recorded(env, monkeypatch):
    tmp, reg, maker = env
    path = tmp / "growing.mp4"
    started = datetime.now(UTC)
    writer = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=12",
         "-c:v", "libx264", "-g", "10", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
         "-flush_packets", "1", str(path)])
    reg.set(1, str(path), started)

    async def scenario():
        await wait_for(lambda: path.exists() and path.stat().st_size > 20000)
        await asyncio.sleep(1.5)
        ws = FakeWS()
        session = media_engine.MediaSession(ws, media_engine.Access())
        await session.handle({"op": "play", "camera": 1, "at": started.timestamp() + 0.5, "audio": False})
        await wait_for(lambda: ws.frag_walls())
        first_last = max(ws.frag_walls())
        await asyncio.sleep(4)
        assert max(ws.frag_walls()) > first_last + 2.5  # new footage as it's written
        # Its wall times match the real clock (origin from the file's own end).
        assert abs(max(ws.frag_walls()) - time.time()) < 3
        await session._cancel_job()

    try:
        asyncio.run(scenario())
    finally:
        writer.kill()
        writer.wait()


@needs_ffmpeg
def test_nothing_recorded_then_is_a_gap_with_where_footage_resumes(env):
    tmp, reg, maker = env
    t0 = time.time() - 7200
    _add_recording(maker, _clip(tmp / "r.mp4", 5), t0 + 600, 5)

    async def scenario():
        ws = FakeWS()
        session = media_engine.MediaSession(ws, media_engine.Access())
        await session.handle({"op": "play", "camera": 1, "at": t0, "audio": False})
        await wait_for(lambda: ws.messages("gap"))
        assert ws.messages("gap")[0]["next_at"] == pytest.approx(t0 + 600)

    asyncio.run(scenario())


def test_kiosk_access_is_limited_to_its_cameras_and_window():
    async def scenario():
        ws = FakeWS()
        floor = time.time() - 3600
        session = media_engine.MediaSession(ws, media_engine.Access(cameras={2}, floor=floor))
        await session.handle({"op": "live", "camera": 1, "quality": "sub"})
        assert ws.messages("error")[0]["message"] == "camera not available"
        assert session.job is None

    asyncio.run(scenario())


# ── live ──────────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_live_is_shared_starts_new_viewers_on_a_keyframe_and_stops_after_the_last(tmp_path, monkeypatch):
    src = _clip(tmp_path / "cam.mp4", 30, gop=10)
    monkeypatch.setattr(media_engine, "LINGER_SECONDS", 1.0)
    monkeypatch.setattr(media_engine, "SUB_QUEUE_BYTES", {"sub": 8_000, "main": 8_000})
    monkeypatch.setattr(media_engine, "live_cmd", lambda url, audio: [
        "ffmpeg", "-loglevel", "error", "-re", "-i", url, "-c", "copy", "-f", "mp4",
        "-movflags", "empty_moov+default_base_moof+frag_every_frame", "-flush_packets", "1", "pipe:1"])

    async def fake_camera(self):
        return src, False

    monkeypatch.setattr(media_engine.LiveSource, "_camera", fake_camera)
    hub = media_engine.LiveHub()

    async def drain(sub, seconds):
        got, end = [], time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                got.append(await asyncio.wait_for(sub.get(), timeout=0.5))
            except asyncio.TimeoutError:
                pass
        return got

    async def scenario():
        s1, a = hub.subscribe(1, "sub")
        s2, b = hub.subscribe(1, "sub")
        assert s1 is s2  # one camera connection for every viewer
        got_a = await drain(a, 2.5)
        assert got_a[0][0] == "init"
        info = got_a[0][2]
        frags = [i[1] for i in got_a if i[0] == "frag"]
        moof = frags[0][:int.from_bytes(frags[0][:4], "big")]
        assert fmp4.parse_fragment(moof, info).key  # starts on a keyframe
        # A viewer that joins later gets a picture at once - from the last keyframe.
        _, c = hub.subscribe(1, "sub")
        first = await asyncio.wait_for(c.get(), 1)
        nxt = await asyncio.wait_for(c.get(), 1)
        assert first[0] == "init" and fmp4.parse_fragment(nxt[1][:int.from_bytes(nxt[1][:4], "big")], info).key
        # b never read anything: it skipped ahead rather than piling up.
        assert b.skips > 0 and b.bytes <= 8_000
        proc = s1.proc
        for sub in (a, b, c):
            s1.detach(sub)
        await asyncio.sleep(3.5)
        assert proc.returncode is not None and (1, "sub") not in hub.sources

    asyncio.run(scenario())


# ── recording audio: tried, not assumed ─────────────────────────────────────


@needs_ffmpeg
def test_audio_is_copied_converted_or_dropped_by_what_actually_records(tmp_path, monkeypatch):
    from app.services import ffmpeg_recorder

    aac = _clip(tmp_path / "aac.mp4", 4, audio="aac")
    # Said to be "aac" but the MP4 writer won't take it as it is (G.711 here -
    # standing in for a camera AAC stream it rejects): converted, not copied.
    odd = _clip(tmp_path / "odd.mov", 4, audio="pcm_mulaw")

    async def says_aac(url):
        return "aac"

    monkeypatch.setattr(ffmpeg_recorder, "_probe_audio_codec", says_aac)

    async def scenario():
        assert await ffmpeg_recorder._resolve_audio_mode(1, aac, True) == "copy"
        assert await ffmpeg_recorder._resolve_audio_mode(1, odd, True) == "aac"
        assert await ffmpeg_recorder._resolve_audio_mode(1, aac, False) == "none"
        # Camera unreachable: undecided (asked again later), not "no sound".
        assert await ffmpeg_recorder._resolve_audio_mode(1, str(tmp_path / "missing.mp4"), True) is None

    asyncio.run(scenario())

