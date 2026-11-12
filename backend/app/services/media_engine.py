"""The NVR's media engine: live view and playback, one path, no re-encoding.

The browser gets the camera's own H.264/H.265 - live from the camera, or from
the recordings on disk - as fragmented MP4 over a single websocket per
player, and plays it with MediaSource. FFmpeg only repackages (stream copy);
nothing is decoded or re-encoded, except camera audio for the full-screen
live view (G.711 -> AAC, tiny), so a tile costs what the camera sends:
~0.4 Mbit/s sub stream, 1-2 Mbit/s main.

Live (`LiveSource`)
  One FFmpeg per camera stream, shared by every viewer, started on the first
  and stopped 10 s after the last. A new viewer starts at once from the last
  keyframe; a viewer whose connection can't keep up skips ahead to the next
  keyframe instead of falling further behind.

Playback (`MediaSession._playback`)
  From any moment: the recording covering it - a finished file, or the
  segment still being written - streamed from the keyframe before that
  moment, kept ~20 s ahead of what the viewer has watched (not whole files),
  carrying on into the next file across small gaps.

Protocol (JSON text + binary; see frontend/src/components/MediaPlayer.tsx)
  client -> {"op": "live", "camera": 1, "quality": "sub"|"main", "audio": bool}
            {"op": "play", "camera": 1, "at": <unix s>, "audio": bool}
            {"op": "pos", "wall": <unix s of the frame on screen>}
            {"op": "stop"}
  server -> {"type": "init", "mode", "codecs", "reset", "origin", "offset", "at"}
            then one binary message: the init segment
            binary fragments
            {"type": "gap", "at", "next_at"}   no footage from `at`
            {"type": "error", "message"}
"""

import asyncio
import logging
import os
import struct
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.camera import Camera
from app.models.recording import Recording
from app.services.active_segment import active_segment_registry
from app.services.camera_gate import camera_gate
from app.services.fmp4 import Fmp4Error, FragmentReader, InitInfo, boxes, first, parse_fragment, parse_init

logger = logging.getLogger(__name__)

LINGER_SECONDS = 10.0  # keep a camera stream this long after its last viewer
START_TIMEOUT = 15.0  # camera connect + first keyframe
STALL_TIMEOUT = 15.0  # no fragment for this long: the camera stream is dead
GOP_CACHE_BYTES = 8 * 1024 * 1024
SUB_QUEUE_BYTES = {"sub": 1 * 1024 * 1024, "main": 3 * 1024 * 1024}  # then skip to the next keyframe
AHEAD_SECONDS = 20.0  # playback runs this far ahead of what's on screen
SMALL_GAP = 15.0  # gaps up to this long are played straight across
MAX_PLAYBACKS = 12  # FFmpeg playback processes at once, NVR-wide
# What may wait in memory between FFmpeg and a viewer. A playback that's far
# enough ahead stops reading; with a small pipe buffer FFmpeg then simply
# waits, instead of up to 2x this sitting in the NVR's memory per viewer.
PIPE_LIMIT = 256 * 1024


def _ts(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def _naive(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


async def _drain(stream: asyncio.StreamReader, keep: int = 400) -> bytes:
    tail = b""
    try:
        while chunk := await stream.read(4096):
            tail = (tail + chunk)[-keep:]
    except Exception:
        pass
    return tail


async def _end(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        logger.warning("FFmpeg %s did not exit after kill", proc.pid)


def live_cmd(url: str, audio: bool) -> list[str]:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-rtsp_transport", "tcp", "-timeout", "10000000",
        "-fflags", "nobuffer", "-analyzeduration", "500000", "-probesize", "262144",
        "-i", url, "-map", "0:v:0", "-c:v", "copy",
    ]
    if audio:
        # Cameras send G.711 (or AAC): browsers take AAC. 16 kHz mono is
        # plenty for a camera microphone and costs next to nothing to encode.
        cmd += ["-map", "0:a:0?", "-c:a", "aac", "-ar", "16000", "-ac", "1", "-b:a", "48k"]
    else:
        cmd += ["-an"]
    # A fragment per frame: nothing waits for a whole GOP before it's sent.
    cmd += ["-f", "mp4", "-movflags", "empty_moov+default_base_moof+frag_every_frame",
            "-flush_packets", "1", "pipe:1"]
    return cmd


def playback_cmd(path: str, offset: float, audio: bool) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    # -copyts: fragments keep the file's own timestamps, so a frame's wall time
    # is just the file's origin + its timestamp.
    cmd += ["-ss", f"{max(0.0, offset):.3f}", "-copyts", "-i", path, "-map", "0:v:0", "-c:v", "copy"]
    cmd += ["-map", "0:a:0?", "-c:a", "copy"] if audio else ["-an"]
    # Without these two the MP4 writer starts its timestamps at 0 again,
    # whatever -copyts says - and the footage lands at the file's start.
    cmd += ["-avoid_negative_ts", "disabled",
            "-f", "mp4", "-movflags", "empty_moov+default_base_moof+frag_keyframe+frag_discont",
            "-frag_duration", "1000000", "pipe:1"]
    return cmd


# ── live ──────────────────────────────────────────────────────────────────


class Subscriber:
    """One viewer's queue. Bounded: a viewer that can't keep up loses the
    rest of the current GOP and carries on from the next keyframe."""

    def __init__(self, quality: str):
        self.max_bytes = SUB_QUEUE_BYTES.get(quality, SUB_QUEUE_BYTES["main"])
        self.items: deque = deque()
        self.bytes = 0
        self.ready = asyncio.Event()
        self.waiting_key = True
        self.skips = 0
        self.closed: str | None = None

    def reset(self, init: bytes, info: InitInfo) -> None:
        self.items.clear()
        self.bytes = 0
        self.items.append(("init", init, info))
        self.waiting_key = True
        self.ready.set()

    def push(self, data: bytes, key: bool) -> None:
        if self.waiting_key:
            if not key:
                return
            self.waiting_key = False
        if self.bytes + len(data) > self.max_bytes:
            # Behind by more than the connection can carry: drop what's queued
            # (but never a pending init) and start again at the next keyframe.
            kept = [i for i in self.items if i[0] == "init"]
            self.items = deque(kept)
            self.bytes = 0
            self.skips += 1
            if not key:
                self.waiting_key = True
                return
        self.items.append(("frag", data))
        self.bytes += len(data)
        self.ready.set()

    def close(self, why: str) -> None:
        self.closed = why
        self.ready.set()

    async def get(self):
        while not self.items:
            if self.closed:
                return ("closed", self.closed)
            self.ready.clear()
            await self.ready.wait()
        item = self.items.popleft()
        if item[0] == "frag":
            self.bytes -= len(item[1])
        return item


class LiveSource:
    def __init__(self, hub: "LiveHub", camera_id: int, quality: str):
        self.hub = hub
        self.camera_id = camera_id
        self.quality = quality
        self.subs: set[Subscriber] = set()
        self.init: bytes | None = None
        self.info: InitInfo | None = None
        self.gop: list[bytes] = []
        self.gop_bytes = 0
        self.audio_ok = True
        self.audio_fails = 0
        self.task: asyncio.Task | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.empty_since: float | None = None
        self.bytes_in = 0
        self.restarts = 0

    def attach(self, sub: Subscriber) -> None:
        self.subs.add(sub)
        self.empty_since = None
        if self.init is not None and self.info is not None:
            sub.reset(self.init, self.info)
            for i, frag in enumerate(self.gop):
                sub.push(frag, key=i == 0)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    def detach(self, sub: Subscriber) -> None:
        self.subs.discard(sub)
        if not self.subs:
            self.empty_since = time.monotonic()

    def _idle(self) -> bool:
        return not self.subs and self.empty_since is not None and time.monotonic() - self.empty_since > LINGER_SECONDS

    async def _camera(self) -> tuple[str, bool] | None:
        async with AsyncSessionLocal() as db:
            cam = await db.get(Camera, self.camera_id)
        if cam is None or not cam.enabled or not cam.rtsp_main_url:
            return None
        url = cam.rtsp_main_url if self.quality == "main" else (cam.rtsp_sub_url or cam.rtsp_main_url)
        # Sound only on the main stream (full screen); the grid is silent.
        return url, self.quality == "main" and bool(cam.has_audio)

    async def _run(self) -> None:
        backoff = 1.0
        try:
            while not self._idle():
                if not self.subs and self.empty_since is None:
                    self.empty_since = time.monotonic()
                cam = await self._camera()
                if cam is None:
                    for sub in list(self.subs):
                        sub.close("camera unavailable")
                    return
                url, has_audio = cam
                audio = has_audio and self.audio_ok
                # Optional: wait while the camera is resting from failures.
                while camera_gate.wait_seconds(self.camera_id, essential=False) > 0:
                    if self._idle():
                        return
                    await asyncio.sleep(1)
                produced = await self._stream_once(url, audio)
                if produced:
                    camera_gate.ok(self.camera_id)
                elif not self._idle():
                    camera_gate.failed(self.camera_id, "live stream did not start")
                if produced:
                    backoff = 1.0
                    self.audio_fails = 0
                    if has_audio and not audio:
                        # Played without sound after sound failed - for this
                        # run. Next time the stream (re)starts, sound gets
                        # another chance.
                        self.audio_ok = True
                else:
                    if audio:
                        self.audio_fails += 1
                        if self.audio_fails >= 2:
                            # Two failed starts with sound: try once without, to
                            # see whether the sound is what stops it.
                            logger.warning("Live camera %s: no picture with sound - trying without",
                                           self.camera_id)
                            self.audio_ok = False
                    elif has_audio and not self.audio_ok:
                        # Without sound it didn't start either: the camera was
                        # busy or offline, not its sound. Keep the sound.
                        self.audio_ok = True
                        self.audio_fails = 0
                    backoff = min(backoff * 2, 30.0)
                if self._idle():
                    break
                self.restarts += 1
                await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Live source camera %s/%s crashed", self.camera_id, self.quality)
            for sub in list(self.subs):
                sub.close("live stream error")
        finally:
            await _end(self.proc)
            self.proc = None
            self.init = self.info = None
            self.gop, self.gop_bytes = [], 0
            self.hub._forget(self)

    async def _stream_once(self, url: str, audio: bool) -> bool:
        self.proc = proc = await asyncio.create_subprocess_exec(
            *live_cmd(url, audio), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=PIPE_LIMIT,
        )
        err = asyncio.create_task(_drain(proc.stderr))
        reader = FragmentReader(proc.stdout)
        produced = False
        try:
            init, info = await asyncio.wait_for(reader.init(), timeout=START_TIMEOUT)
            first_frag = await asyncio.wait_for(reader.fragment(), timeout=START_TIMEOUT)
            self.init, self.info = init, info
            self.gop, self.gop_bytes = [], 0
            for sub in list(self.subs):
                sub.reset(init, info)
            frag = first_frag
            while True:
                data, finfo = frag
                produced = True
                self.bytes_in += len(data)
                if finfo.key:
                    self.gop, self.gop_bytes = [data], len(data)
                elif self.gop and self.gop_bytes + len(data) <= GOP_CACHE_BYTES:
                    self.gop.append(data)
                    self.gop_bytes += len(data)
                for sub in list(self.subs):
                    sub.push(data, finfo.key)
                if self._idle():
                    return produced
                frag = await asyncio.wait_for(reader.fragment(), timeout=STALL_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, Fmp4Error) as exc:
            await _end(proc)
            if not self._idle():
                try:
                    tail = await asyncio.wait_for(err, 5)
                except Exception:
                    tail = b""
                why = str(exc) if isinstance(exc, Fmp4Error) else type(exc).__name__
                logger.info("Live camera %s/%s stream ended (%s)%s", self.camera_id, self.quality, why,
                            f": {tail.decode(errors='ignore').strip()[-200:]}" if tail else "")
            return produced
        finally:
            await _end(proc)
            err.cancel()


class LiveHub:
    def __init__(self) -> None:
        self.sources: dict[tuple[int, str], LiveSource] = {}

    def subscribe(self, camera_id: int, quality: str) -> tuple[LiveSource, Subscriber]:
        key = (camera_id, quality)
        src = self.sources.get(key)
        if src is None:
            src = self.sources[key] = LiveSource(self, camera_id, quality)
        sub = Subscriber(quality)
        src.attach(sub)
        return src, sub

    def _forget(self, src: LiveSource) -> None:
        if self.sources.get((src.camera_id, src.quality)) is src:
            del self.sources[(src.camera_id, src.quality)]
            # A viewer that joined while it was shutting down gets a fresh one.
            waiting = [s for s in src.subs if not s.closed]
            if waiting:
                fresh = self.sources[(src.camera_id, src.quality)] = LiveSource(self, src.camera_id, src.quality)
                for sub in waiting:
                    fresh.attach(sub)

    def restart_camera(self, camera_id: int) -> None:
        """The camera's settings changed: reconnect its live streams."""
        for (cid, _), src in list(self.sources.items()):
            if cid == camera_id and src.proc is not None:
                try:
                    src.proc.kill()
                except ProcessLookupError:
                    pass

    def stats(self) -> list[dict]:
        return [{"camera": s.camera_id, "quality": s.quality, "viewers": len(s.subs),
                 "mbytes_in": round(s.bytes_in / 1e6, 1), "restarts": s.restarts,
                 "skips": sum(x.skips for x in s.subs), "audio": s.audio_ok}
                for s in self.sources.values()]

    async def shutdown(self) -> None:
        for src in list(self.sources.values()):
            if src.task:
                src.task.cancel()
        await asyncio.gather(*(s.task for s in self.sources.values() if s.task), return_exceptions=True)


live_hub = LiveHub()


# ── playback ──────────────────────────────────────────────────────────────


@dataclass
class Piece:
    path: str
    origin: float  # wall time of timestamp 0 in the file
    end: float  # wall time of its last frame (so far)
    growing: bool  # still being written
    fragmented: bool = False  # a segment as the recorder writes it (fragmented MP4)


def _last_fragment_end(path: str) -> float | None:
    """Timestamp (s) where the footage in a fragmented MP4 ends so far: from
    its init segment and its last fragment - no need to read the whole file."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = bytearray(f.read(256 * 1024))
            moov = None
            for kind, ps, be in boxes(head):
                if kind == b"moov":
                    moov = bytearray(head[ps - 8:be])
                    break
            if moov is None:
                return None
            info = parse_init(moov)
            video = info.video
            tail_at = max(0, size - 6 * 1024 * 1024)
            f.seek(tail_at)
            tail = f.read()
    except (OSError, Fmp4Error):
        return None
    pos = len(tail)
    while True:
        pos = tail.rfind(b"moof", 0, pos)
        if pos < 4:
            return None
        start = pos - 4
        box_size = struct.unpack_from(">I", tail, start)[0]
        if 8 < box_size <= len(tail) - start:
            try:
                return _moof_end(tail[start:start + box_size], video)
            except (Fmp4Error, struct.error, IndexError):
                pass
        pos -= 1


def _moof_end(moof: bytes, video) -> float | None:
    for tps, tbe in _children(moof, b"traf"):
        tfhd = first(moof, [b"tfhd"], tps, tbe)
        if tfhd is None or struct.unpack_from(">I", moof, tfhd[0] + 4)[0] != video.track_id:
            continue
        tf_flags = int.from_bytes(moof[tfhd[0] + 1:tfhd[0] + 4], "big")
        pos = tfhd[0] + 8 + (8 if tf_flags & 0x01 else 0) + (4 if tf_flags & 0x02 else 0)
        default_dur = struct.unpack_from(">I", moof, pos)[0] if tf_flags & 0x08 else 0
        tfdt = first(moof, [b"tfdt"], tps, tbe)
        trun = first(moof, [b"trun"], tps, tbe)
        if tfdt is None or trun is None:
            return None
        base = (struct.unpack_from(">Q", moof, tfdt[0] + 4)[0] if moof[tfdt[0]] == 1
                else struct.unpack_from(">I", moof, tfdt[0] + 4)[0])
        tr_flags = int.from_bytes(moof[trun[0] + 1:trun[0] + 4], "big")
        count = struct.unpack_from(">I", moof, trun[0] + 4)[0]
        pos = trun[0] + 8 + (4 if tr_flags & 0x01 else 0) + (4 if tr_flags & 0x04 else 0)
        total = 0
        per = sum(4 for bit in (0x100, 0x200, 0x400, 0x800) if tr_flags & bit)
        for i in range(count):
            total += struct.unpack_from(">I", moof, pos + i * per)[0] if tr_flags & 0x100 else default_dur
        return (base + total) / video.timescale
    return None


def _box_header(f, pos: int, size_now: int):
    """(type, header length, box size) at pos, or None if not all written yet."""
    if pos + 8 > size_now:
        return None
    f.seek(pos)
    size, kind = struct.unpack(">I4s", f.read(8))
    hdr = 8
    if size == 1:
        if pos + 16 > size_now:
            return None
        size = struct.unpack(">Q", f.read(8))[0]
        hdr = 16
    elif size == 0:
        return None  # "to the end of the file": not finished yet
    if size < hdr or size > 512 * 1024 * 1024:
        raise Fmp4Error(f"bad box size {size} for {kind!r} at {pos}")
    if pos + size > size_now:
        return None
    return kind, hdr, size


def _read_box_at(f, pos: int):
    h = _box_header(f, pos, os.fstat(f.fileno()).st_size)
    if h is None:
        return None
    kind, _, size = h
    f.seek(pos)
    return kind, f.read(size)


def _fragment_at(f, pos: int):
    """The next complete fragment from pos: (its moof, start, end of its
    mdat) - the media itself isn't read yet. None: not all written yet."""
    size_now = os.fstat(f.fileno()).st_size
    while True:
        h = _box_header(f, pos, size_now)
        if h is None:
            return None
        kind, _, size = h
        if kind == b"moof":
            break
        pos += size  # mfra / free / sidx: skip
    start = pos
    f.seek(pos)
    moof = f.read(size)
    pos += size
    while True:
        h = _box_header(f, pos, size_now)
        if h is None:
            return None
        kind, _, size = h
        pos += size
        if kind == b"mdat":
            return moof, start, pos


def _read_span(f, span) -> bytes:
    f.seek(span[0])
    return f.read(span[1] - span[0])


def _children(data: bytes, kind: bytes):
    for k, ps, be in boxes(data, 8, len(data)):
        if k == kind:
            yield ps, be


async def find_piece(camera_id: int, at: float, done: set[str] | frozenset = frozenset()) -> Piece | None:
    """The footage covering wall time `at`, skipping files in `done` (already
    played to their end in this session)."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Recording)
            .where(Recording.camera_id == camera_id, Recording.started_at <= _naive(at),
                   Recording.ended_at >= _naive(at))
            .order_by(Recording.started_at.desc()).limit(3)
        )).scalars().all()
    for row in rows:
        if row.file_path not in done and os.path.exists(row.file_path):
            return Piece(row.file_path, _ts(row.started_at), _ts(row.ended_at), False)
    segs = active_segment_registry.closing(camera_id)
    active = active_segment_registry.get(camera_id)
    if active is not None:
        segs.append(active)
    now = time.time()
    for seg in segs:
        start = _ts(seg.started_at)
        end = _ts(seg.ended_at) if seg.ended_at else now
        if not (start - 5 <= at <= end) or seg.file_path in done or not os.path.exists(seg.file_path):
            continue
        # Where the footage really starts: the last frame was written at the
        # file's mtime (ended_at once closed), and its timestamp is known.
        written = await asyncio.to_thread(_last_fragment_end, seg.file_path)
        last_wall = _ts(seg.ended_at) if seg.ended_at else os.path.getmtime(seg.file_path)
        origin = last_wall - written if written else start
        return Piece(seg.file_path, origin, last_wall, seg.ended_at is None, fragmented=True)
    return None


async def next_footage(camera_id: int, after: float) -> float | None:
    from app.services import playback

    async with AsyncSessionLocal() as db:
        return await playback.next_footage(db, camera_id, after)


# ── one player = one websocket ──────────────────────────────────────────────


@dataclass
class Access:
    cameras: set[int] | None = None  # None: all
    floor: float | None = None  # earliest wall time playback may reach (kiosk)

    def allows(self, camera_id: int) -> bool:
        return self.cameras is None or camera_id in self.cameras


_playbacks = 0


class MediaSession:
    def __init__(self, ws, access: Access):
        self.ws = ws
        self.access = access
        self.job: asyncio.Task | None = None
        self.send_lock = asyncio.Lock()
        self.client_wall = 0.0
        self.pos_event = asyncio.Event()
        self.bytes_out = 0

    async def send_json(self, msg: dict) -> None:
        async with self.send_lock:
            await self.ws.send_json(msg)

    async def send_bytes(self, data: bytes) -> None:
        async with self.send_lock:
            await self.ws.send_bytes(data)
        self.bytes_out += len(data)

    async def run(self) -> None:
        try:
            while True:
                msg = await self.ws.receive_json()
                await self.handle(msg)
        finally:
            await self._cancel_job()

    async def handle(self, msg: dict) -> None:
        op = msg.get("op")
        if op == "pos":
            try:
                self.client_wall = float(msg.get("wall") or 0)
            except (TypeError, ValueError):
                return
            self.pos_event.set()
            return
        if op == "stop":
            await self._cancel_job()
            return
        if op not in ("live", "play"):
            return
        try:
            camera_id = int(msg.get("camera"))
        except (TypeError, ValueError):
            await self.send_json({"type": "error", "message": "bad camera"})
            return
        if not self.access.allows(camera_id):
            await self.send_json({"type": "error", "message": "camera not available"})
            return
        audio = bool(msg.get("audio"))
        await self._cancel_job()
        if op == "live":
            quality = "main" if msg.get("quality") == "main" else "sub"
            self.job = asyncio.create_task(self._live(camera_id, quality))
        else:
            try:
                at = float(msg.get("at"))
            except (TypeError, ValueError):
                await self.send_json({"type": "error", "message": "bad time"})
                return
            if self.access.floor is not None:
                at = max(at, self.access.floor)
            self.client_wall = at
            self.job = asyncio.create_task(self._playback(camera_id, at, audio))

    async def _cancel_job(self) -> None:
        job, self.job = self.job, None
        if job and not job.done():
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def _live(self, camera_id: int, quality: str) -> None:
        src, sub = live_hub.subscribe(camera_id, quality)
        try:
            while True:
                item = await sub.get()
                if item[0] == "init":
                    _, init, info = item
                    await self.send_json({"type": "init", "mode": "live", "codecs": info.codecs,
                                          "audio": info.audio is not None, "reset": True})
                    await self.send_bytes(init)
                elif item[0] == "frag":
                    await self.send_bytes(item[1])
                else:
                    await self.send_json({"type": "error", "message": item[1]})
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # the socket went away; run() notices
        finally:
            src.detach(sub)

    async def _playback(self, camera_id: int, at: float, audio: bool) -> None:
        global _playbacks
        if _playbacks >= MAX_PLAYBACKS:
            await self.send_json({"type": "error", "message": "The NVR is busy - try again in a moment"})
            return
        _playbacks += 1
        try:
            t = at
            session_origin: float | None = None
            codecs: str | None = None
            done: set[str] = set()
            while True:
                piece = await find_piece(camera_id, t, done)
                if piece is None:
                    nxt = await next_footage(camera_id, t)
                    if nxt is not None and nxt - t <= SMALL_GAP and nxt < time.time():
                        t = nxt + 0.05
                        # The viewer's picture jumps the gap too: pace from there,
                        # or the footage after it would wait for a position the
                        # viewer can't reach without it.
                        self.client_wall = max(self.client_wall, t)
                        continue
                    await self.send_json({"type": "gap", "at": t, "next_at": nxt})
                    return
                if session_origin is None:
                    session_origin = piece.origin
                play = self._play_fragmented if piece.fragmented else self._play_piece
                reached, codecs = await play(camera_id, piece, t, audio, session_origin, codecs)
                # Played to its end (or unplayable): on to whatever follows.
                done.add(piece.path)
                t = max(t, reached if reached is not None else piece.end) + 0.05
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Playback camera %s failed", camera_id)
            try:
                await self.send_json({"type": "error", "message": "Playback failed"})
            except Exception:
                pass
        finally:
            _playbacks -= 1

    async def _play_piece(self, camera_id: int, piece: Piece, t: float, audio: bool, session_origin: float,
                          codecs: str | None) -> tuple[float | None, str | None]:
        """Stream one file from wall time t. Returns (wall time reached, codecs)."""
        proc = await asyncio.create_subprocess_exec(
            *playback_cmd(piece.path, t - piece.origin, audio),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=PIPE_LIMIT,
        )
        err = asyncio.create_task(_drain(proc.stderr))
        reader = FragmentReader(proc.stdout)
        reached: float | None = None
        try:
            try:
                init, info = await asyncio.wait_for(reader.init(), timeout=START_TIMEOUT)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, Fmp4Error):
                return None, codecs
            reset = info.codecs != codecs
            await self.send_json({"type": "init", "mode": "playback", "codecs": info.codecs,
                                  "audio": info.audio is not None, "reset": reset,
                                  "origin": session_origin, "offset": piece.origin - session_origin, "at": t})
            await self.send_bytes(init)
            while True:
                try:
                    data, finfo = await asyncio.wait_for(reader.fragment(), timeout=START_TIMEOUT)
                except (asyncio.TimeoutError, asyncio.IncompleteReadError, Fmp4Error):
                    break
                reached = await self._emit(piece, data, finfo, reached)
            return reached, info.codecs
        finally:
            await _end(proc)
            err.cancel()

    async def _emit(self, piece: Piece, data: bytes, finfo, reached: float | None) -> float | None:
        if finfo.time is not None:
            wall = piece.origin + finfo.time
            # Stay AHEAD_SECONDS ahead of the screen (FFmpeg simply waits on
            # the full pipe, a file isn't read further, meanwhile).
            while wall - self.client_wall > AHEAD_SECONDS:
                self.pos_event.clear()
                try:
                    await asyncio.wait_for(self.pos_event.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
            reached = wall
        await self.send_bytes(data)
        return reached

    async def _play_fragmented(self, camera_id: int, piece: Piece, t: float, audio: bool, session_origin: float,
                               codecs: str | None) -> tuple[float | None, str | None]:
        """A segment as the recorder writes it is already fragmented MP4 - just
        what the browser takes. Its fragments are sent straight from the file
        (no FFmpeg at all), from the one at `t`, following the file as it
        grows until the recorder closes it. (FFmpeg's MP4 reader stops at the
        size the file had when it opened it.) Sound: it's in the file anyway."""
        offset = t - piece.origin
        try:
            f = open(piece.path, "rb")
        except OSError:
            return None, codecs
        reached: float | None = None
        try:
            pos, parts, waited = 0, [], 0.0
            while True:  # init: ftyp + moov
                box = await asyncio.to_thread(_read_box_at, f, pos)
                if box is None:
                    if waited > START_TIMEOUT or not piece.growing:
                        return None, codecs
                    await asyncio.sleep(0.5)
                    waited += 0.5
                    continue
                kind, data = box
                parts.append(data)
                pos += len(data)
                if kind == b"moov":
                    break
            init = bytearray(b"".join(parts))
            info = parse_init(init)
            reset = info.codecs != codecs
            await self.send_json({"type": "init", "mode": "playback", "codecs": info.codecs,
                                  "audio": info.audio is not None, "reset": reset,
                                  "origin": session_origin, "offset": piece.origin - session_origin, "at": t})
            await self.send_bytes(bytes(init))
            lead: list = []  # (start, end, info) from the last keyframe before t
            started = False
            while True:
                frag = await asyncio.to_thread(_fragment_at, f, pos)
                if frag is None:
                    if not started and lead:  # t is past the last fragment so far
                        started = True
                        for span in lead:
                            data = await asyncio.to_thread(_read_span, f, span)
                            reached = await self._emit(piece, data, span[2], reached)
                        lead = []
                    if self._still_growing(camera_id, piece):
                        await asyncio.sleep(0.5)
                        continue
                    break
                moof, start, end = frag
                pos = end
                finfo = parse_fragment(moof, info)
                if not started:
                    if finfo.time is not None and finfo.time <= offset:
                        lead = [(start, end, finfo)] if finfo.key else lead + [(start, end, finfo)]
                        continue
                    started = True
                    for span in lead:
                        data = await asyncio.to_thread(_read_span, f, span)
                        reached = await self._emit(piece, data, span[2], reached)
                    lead = []
                data = await asyncio.to_thread(_read_span, f, (start, end))
                reached = await self._emit(piece, data, finfo, reached)
            return reached, info.codecs
        except Fmp4Error as exc:
            logger.info("Playback of %s stopped: %s", piece.path, exc)
            return reached, codecs
        finally:
            f.close()

    @staticmethod
    def _still_growing(camera_id: int, piece: Piece) -> bool:
        seg = active_segment_registry.get(camera_id)
        return seg is not None and seg.file_path == piece.path and seg.ended_at is None
