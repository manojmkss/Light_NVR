"""Just enough fragmented-MP4 parsing for the media engine.

FFmpeg writes the camera's video (never re-encoded) as fragmented MP4: one
init segment (ftyp + moov) and then fragments (moof + mdat). The engine needs
three things from that stream, and they're all here:

- the boundaries between boxes, to forward whole fragments;
- the codec string a browser's MediaSource wants ("hvc1.1.6.L153.B0",
  "avc1.64002A", "mp4a.40.2"), read from the init segment;
- for each fragment, whether it starts with a keyframe (a new viewer, or one
  that fell behind, must start there) and its timestamp.

Box layouts: ISO/IEC 14496-12 (ISO BMFF) and 14496-15 (avcC / hvcC).
"""

import asyncio
import struct
from dataclasses import dataclass, field

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"mvex", b"moof", b"traf", b"edts", b"dinf"}
NON_SYNC = 0x00010000  # sample_is_non_sync_sample


class Fmp4Error(Exception):
    pass


def boxes(data: bytes, start: int = 0, end: int | None = None):
    """(type, payload_start, box_end) for each box in data[start:end]."""
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            if pos + 16 > end:
                raise Fmp4Error("truncated large box")
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise Fmp4Error(f"bad box size for {kind!r}")
        yield kind, pos + header, pos + size
        pos += size


def find(data: bytes, path: list[bytes], start: int = 0, end: int | None = None):
    """Every box at `path` (e.g. [b"trak", b"mdia", b"hdlr"]): (payload_start, box_end)."""
    for kind, ps, be in boxes(data, start, end):
        if kind == path[0]:
            if len(path) == 1:
                yield ps, be
            elif kind in CONTAINERS:
                yield from find(data, path[1:], ps, be)


def first(data: bytes, path: list[bytes], start: int = 0, end: int | None = None):
    return next(find(data, path, start, end), None)


@dataclass
class Track:
    track_id: int
    kind: str  # "video" | "audio" | other handler
    timescale: int
    codec: str  # browser codec string
    fourcc: bytes = b""
    default_flags: int = 0  # from trex


@dataclass
class InitInfo:
    tracks: dict[int, Track] = field(default_factory=dict)

    @property
    def video(self) -> Track | None:
        return next((t for t in self.tracks.values() if t.kind == "video"), None)

    @property
    def audio(self) -> Track | None:
        return next((t for t in self.tracks.values() if t.kind == "audio"), None)

    @property
    def codecs(self) -> str:
        return ",".join(t.codec for t in (self.video, self.audio) if t is not None)

    @property
    def mime(self) -> str:
        return f'video/mp4; codecs="{self.codecs}"'


def _avc_codec(fourcc: bytes, cfg: bytes) -> str:
    if len(cfg) < 4:
        raise Fmp4Error("short avcC")
    return f"{fourcc.decode()}.{cfg[1]:02X}{cfg[2]:02X}{cfg[3]:02X}"


def _hevc_codec(fourcc: bytes, cfg: bytes) -> str:
    """ISO/IEC 14496-15 Annex E, e.g. hvc1.1.6.L153.B0."""
    if len(cfg) < 13:
        raise Fmp4Error("short hvcC")
    space = "" if (cfg[1] >> 6) == 0 else "ABC"[(cfg[1] >> 6) - 1]
    tier = "H" if cfg[1] & 0x20 else "L"
    profile = cfg[1] & 0x1F
    compat = int.from_bytes(cfg[2:6], "big")
    compat_rev = int(f"{compat:032b}"[::-1], 2)
    constraints = list(cfg[6:12])
    while constraints and constraints[-1] == 0:
        constraints.pop()
    parts = [fourcc.decode(), f"{space}{profile}", f"{compat_rev:X}", f"{tier}{cfg[12]}"]
    parts += [f"{b:X}" for b in constraints]
    return ".".join(parts)


def _mp4a_codec(data: bytes, ps: int, be: int) -> str:
    """mp4a.40.<audio object type>, from the esds DecoderSpecificInfo."""
    # AudioSampleEntry: 8 reserved/dri + 8 reserved + 2 channels + 2 size + 4 + 4 rate = 28
    esds = first(data, [b"esds"], ps + 28, be)
    if esds is None:
        return "mp4a.40.2"
    pos, end = esds[0] + 4, esds[1]  # full box
    try:
        while pos < end:
            tag = data[pos]
            pos += 1
            size = 0
            for _ in range(4):
                b = data[pos]
                pos += 1
                size = (size << 7) | (b & 0x7F)
                if not b & 0x80:
                    break
            if tag == 0x03:  # ES_Descriptor: ES_ID(2) + flags(1) [+ optional fields]
                flags = data[pos + 2]
                pos += 3 + (2 if flags & 0x80 else 0) + (2 if flags & 0x20 else 0)
                if flags & 0x40:
                    pos += 1 + data[pos]
            elif tag == 0x04:  # DecoderConfigDescriptor
                oti = data[pos]
                if oti != 0x40:
                    return f"mp4a.{oti:02X}"
                pos += 13
            elif tag == 0x05:  # DecoderSpecificInfo
                aot = data[pos] >> 3
                if aot == 31:
                    aot = 32 + (((data[pos] & 0x07) << 3) | (data[pos + 1] >> 5))
                return f"mp4a.40.{aot}"
            else:
                pos += size
    except IndexError:
        pass
    return "mp4a.40.2"


def parse_init(data: bytearray) -> InitInfo:
    """Tracks and codec strings from an init segment. Rewrites an HEVC
    sample entry 'hev1' to 'hvc1' in place: Safari only plays hvc1, and FFmpeg
    labels a stream-copied camera HEVC hev1 (parameter sets may also come in
    band - every decoder that plays hvc1 copes with that)."""
    info = InitInfo()
    trex_flags = {}
    for ps, be in find(data, [b"moov", b"mvex", b"trex"]):
        track_id, _, _, _, flags = struct.unpack_from(">IIIII", data, ps + 4)
        trex_flags[track_id] = flags
    for tps, tbe in find(data, [b"moov", b"trak"]):
        tkhd = first(data, [b"tkhd"], tps, tbe)
        hdlr = first(data, [b"mdia", b"hdlr"], tps, tbe)
        mdhd = first(data, [b"mdia", b"mdhd"], tps, tbe)
        stsd = first(data, [b"mdia", b"minf", b"stbl", b"stsd"], tps, tbe)
        if not (tkhd and hdlr and mdhd and stsd):
            continue
        version = data[tkhd[0]]
        track_id = struct.unpack_from(">I", data, tkhd[0] + (20 if version == 1 else 12))[0]
        handler = bytes(data[hdlr[0] + 8:hdlr[0] + 12])
        mv = data[mdhd[0]]
        timescale = struct.unpack_from(">I", data, mdhd[0] + (20 if mv == 1 else 12))[0]
        entries = list(boxes(data, stsd[0] + 8, stsd[1]))  # full box + entry_count
        if not entries:
            continue
        fourcc, eps, ebe = entries[0]
        codec = ""
        if handler == b"vide":
            kind = "video"
            children = eps + 78  # VisualSampleEntry fields
            if fourcc in (b"hev1", b"hvc1"):
                cfg = first(data, [b"hvcC"], children, ebe)
                if cfg is None:
                    raise Fmp4Error("HEVC without hvcC")
                if fourcc == b"hev1":
                    data[eps - 4:eps] = b"hvc1"
                    fourcc = b"hvc1"
                codec = _hevc_codec(fourcc, bytes(data[cfg[0]:cfg[1]]))
            elif fourcc in (b"avc1", b"avc3"):
                cfg = first(data, [b"avcC"], children, ebe)
                if cfg is None:
                    raise Fmp4Error("H.264 without avcC")
                codec = _avc_codec(fourcc, bytes(data[cfg[0]:cfg[1]]))
            else:
                raise Fmp4Error(f"unsupported video {fourcc!r}")
        elif handler == b"soun":
            kind = "audio"
            if fourcc == b"mp4a":
                codec = _mp4a_codec(data, eps, ebe)
            elif fourcc in (b"Opus", b"fLaC"):
                codec = fourcc.decode().lower()
            else:
                raise Fmp4Error(f"unsupported audio {fourcc!r}")
        else:
            continue
        info.tracks[track_id] = Track(track_id, kind, timescale, codec, fourcc, trex_flags.get(track_id, 0))
    if info.video is None:
        raise Fmp4Error("no video track")
    return info


@dataclass
class FragmentInfo:
    key: bool  # the video in this fragment starts with a keyframe
    time: float | None  # video decode time of its first sample, seconds
    has_video: bool


def parse_fragment(moof: bytes, info: InitInfo) -> FragmentInfo:
    video = info.video
    for ps, be in find(moof, [b"moof", b"traf"]):
        tfhd = first(moof, [b"tfhd"], ps, be)
        if tfhd is None:
            continue
        tf_flags = int.from_bytes(moof[tfhd[0] + 1:tfhd[0] + 4], "big")
        track_id = struct.unpack_from(">I", moof, tfhd[0] + 4)[0]
        if video is None or track_id != video.track_id:
            continue
        pos = tfhd[0] + 8
        default_flags = video.default_flags
        if tf_flags & 0x01:
            pos += 8
        if tf_flags & 0x02:
            pos += 4
        if tf_flags & 0x08:
            pos += 4
        if tf_flags & 0x10:
            pos += 4
        if tf_flags & 0x20:
            default_flags = struct.unpack_from(">I", moof, pos)[0]
        time = None
        tfdt = first(moof, [b"tfdt"], ps, be)
        if tfdt is not None:
            base = (struct.unpack_from(">Q", moof, tfdt[0] + 4)[0] if moof[tfdt[0]] == 1
                    else struct.unpack_from(">I", moof, tfdt[0] + 4)[0])
            time = base / video.timescale if video.timescale else None
        flags = default_flags
        trun = first(moof, [b"trun"], ps, be)
        if trun is not None:
            tr_flags = int.from_bytes(moof[trun[0] + 1:trun[0] + 4], "big")
            count = struct.unpack_from(">I", moof, trun[0] + 4)[0]
            pos = trun[0] + 8 + (4 if tr_flags & 0x01 else 0)
            if tr_flags & 0x04:
                flags = struct.unpack_from(">I", moof, pos)[0]
            elif tr_flags & 0x400 and count:
                pos += (4 if tr_flags & 0x100 else 0) + (4 if tr_flags & 0x200 else 0)
                flags = struct.unpack_from(">I", moof, pos)[0]
        return FragmentInfo(key=not flags & NON_SYNC, time=time, has_video=True)
    return FragmentInfo(key=False, time=None, has_video=False)


async def read_box(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    """One whole top-level box from a stream: (type, bytes incl. header).
    IncompleteReadError at the end of the stream."""
    head = await reader.readexactly(8)
    size, kind = struct.unpack(">I4s", head)
    if size == 1:
        ext = await reader.readexactly(8)
        size = struct.unpack(">Q", ext)[0]
        head += ext
    if size < len(head) or size > 256 * 1024 * 1024:
        raise Fmp4Error(f"bad box size {size} for {kind!r}")
    return kind, head + await reader.readexactly(size - len(head))


class FragmentReader:
    """Init segment, then fragments, from FFmpeg's fragmented MP4 output."""

    def __init__(self, reader: asyncio.StreamReader):
        self.reader = reader
        self.info: InitInfo | None = None

    async def init(self) -> tuple[bytes, InitInfo]:
        parts = []
        while True:
            kind, box = await read_box(self.reader)
            parts.append(box)
            if kind == b"moov":
                break
            if len(parts) > 8:
                raise Fmp4Error("no moov at the start")
        data = bytearray(b"".join(parts))
        self.info = parse_init(data)
        return bytes(data), self.info

    async def fragment(self) -> tuple[bytes, FragmentInfo]:
        """The next moof + mdat (and anything between them)."""
        assert self.info is not None
        while True:
            kind, box = await read_box(self.reader)
            if kind == b"moof":
                break
            # styp / sidx / free between fragments: nothing a browser needs.
        moof = box
        parts = [moof]
        while True:
            kind, box = await read_box(self.reader)
            parts.append(box)
            if kind == b"mdat":
                break
        return b"".join(parts), parse_fragment(moof, self.info)
