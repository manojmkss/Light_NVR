"""The visit review: once a visit is over and the system is quiet, the AI goes
over the visit's main-stream recording at 10 frames a second and finishes the
visit properly.

Live checks (every 500 ms, on the sub stream until a person is confirmed)
are tuned for instant alerts and miss the moment a face turns to the camera.
Nothing here is in a hurry, so it:
- reads the recording at 10 frames/s where the live check saw a person or a
  vehicle (and 2 frames/s over the rest of the visit, to catch anyone the
  live check missed), at the main stream's full resolution;
- follows everyone with the same tracker as live, and searches faces on the
  full-resolution frames - so it names the people it knows, and tells a
  stranger (a clear face that matches nobody) from someone whose face never
  showed;
- replaces the visit's pictures (its snapshot, the alert pictures of its
  events, the picture of each detected object) with main-stream ones;
- writes one AI description of the visit, and sends one visit message.

A live event with someone in view pauses it (live checks come first); after
30 minutes of a busy system it carries on anyway, at the lowest priority.
"""

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import cv2
import numpy as np
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.camera import Camera
from app.models.detection import Detection
from app.models.event import Event
from app.models.incident import Incident
from app.models.recording import Recording
from app.services.motion_event import (
    REID_SIM,
    ObjectTracker,
    Track,
    _identities_differ,
    _jpeg,
    draw_boxes,
    fit_for_storage,
    format_duration,
    group_of,
    plural,
    signature_similarity,
    vote_face,
)

logger = logging.getLogger(__name__)

FOCUS_FPS = 10.0  # where someone was seen: a look every 100 ms
SCAN_FPS = 2.0  # the rest of the visit, for anyone the live check missed
FOCUS_PAD = 5.0  # seconds either side of a live sighting read at FOCUS_FPS
SHORT_VISIT = 120.0  # a visit this short is read at FOCUS_FPS throughout
EDGE_PAD = 3.0  # before and after the visit (the pre-roll)
MAX_SECONDS_PER_CAMERA = 15 * 60
MAX_WIDTH = 2688  # 4MP; anything bigger is decoded at this width
DETECT_WIDTH = 1280  # sent for detection (the model letterboxes to 640 anyway)
FACE_EVERY = 0.2  # seconds between face looks at one person
FACE_MAX_TRIES = 60
QUIET_LOAD = 0.7  # load average per core below which the system counts as quiet
MAX_WAIT_FOR_QUIET = 30 * 60
FOOTAGE_WAIT = 20 * 60  # the last minutes may still be in a segment being written
TOO_OLD = 24 * 3600
POLL_SECONDS = 30
PHOTO_DIR = "visits"
FOCUS_GROUPS = ("person", "vehicle", "two_wheeler")
SUMMARY_TYPES = ("motion_summary", "incident_summary")


class ReviewTracker(ObjectTracker):
    # Ten looks a second instead of two: a stray box twice in a row is no
    # evidence, so a track needs more sightings before it counts.
    CONFIRM_HITS = 4


@dataclass
class Piece:
    path: str
    inpoint: float  # seconds into the file
    duration: float
    wall: float  # wall time of `inpoint`
    fps: float


@dataclass
class Person:
    kind: str  # known | probable | stranger | unseen
    name: str | None
    camera_id: int
    track: Track
    tracks: list = field(default_factory=list)


def _ts(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def _naive(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


def _merge(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def plan_rates(window: tuple[float, float], focus: list[tuple[float, float]]) -> list[tuple[float, float, float]]:
    """(start, end, fps) stretches covering the window: FOCUS_FPS around the
    live sightings, SCAN_FPS elsewhere; capped at MAX_SECONDS_PER_CAMERA
    (the 10 fps stretches are kept first)."""
    ws, we = window
    if we - ws <= SHORT_VISIT:
        return [(ws, we, FOCUS_FPS)]
    focus = _merge([(max(ws, a - FOCUS_PAD), min(we, b + FOCUS_PAD)) for a, b in focus if b >= ws and a <= we])
    out, at = [], ws
    for a, b in focus:
        if a > at:
            out.append((at, a, SCAN_FPS))
        out.append((a, b, FOCUS_FPS))
        at = b
    if at < we:
        out.append((at, we, SCAN_FPS))
    if sum(b - a for a, b, _ in out) > MAX_SECONDS_PER_CAMERA:
        out = [s for s in out if s[2] == FOCUS_FPS]
        kept, total = [], 0.0
        for a, b, fps in out:
            if total >= MAX_SECONDS_PER_CAMERA:
                break
            b = min(b, a + MAX_SECONDS_PER_CAMERA - total)
            kept.append((a, b, fps))
            total += b - a
        out = kept
    return [s for s in out if s[1] - s[0] > 0.2]


def pieces_for(recordings: list[Recording], stretches: list[tuple[float, float, float]]) -> list[Piece]:
    """Which part of which file covers each stretch (a stretch can span two
    5-minute segments)."""
    out = []
    recs = sorted((r for r in recordings if r.file_path and os.path.exists(r.file_path)),
                  key=lambda r: r.started_at)
    for a, b, fps in stretches:
        for r in recs:
            rs = _ts(r.started_at)
            re = _ts(r.ended_at) if r.ended_at else rs + (r.duration_seconds or 0.0)
            lo, hi = max(a, rs), min(b, re)
            if hi - lo > 0.2:
                out.append(Piece(r.file_path, lo - rs, hi - lo, lo, fps))
    return out


# ── reading the recording ───────────────────────────────────────────────────


async def _probe_size(path: str) -> tuple[int, int] | None:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
        "-of", "csv=p=0", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        w, h = (int(x) for x in out.decode().strip().split(",")[:2])
        return w, h
    except (asyncio.TimeoutError, ValueError):
        if proc.returncode is None:
            proc.kill()
        return None


async def read_frames(piece: Piece):
    """Frames of one piece, as (wall time, BGR frame), at its rate and the
    main stream's own resolution (up to MAX_WIDTH)."""
    size = await _probe_size(piece.path)
    if size is None:
        return
    w, h = size
    if w > MAX_WIDTH:
        w, h = MAX_WIDTH, int(h * MAX_WIDTH / w)
    w, h = w - w % 2, h - h % 2
    nice = ["nice", "-n", "10"] if shutil.which("nice") else []
    proc = await asyncio.create_subprocess_exec(
        *nice, "ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "2",
        "-ss", f"{piece.inpoint:.3f}", "-i", piece.path, "-t", f"{piece.duration:.3f}",
        "-vf", f"fps={piece.fps},scale={w}:{h}", "-an", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    n = 0
    try:
        while True:
            try:
                buf = await proc.stdout.readexactly(w * h * 3)
            except asyncio.IncompleteReadError:
                break
            yield piece.wall + n / piece.fps, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            n += 1
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


# ── when to run ─────────────────────────────────────────────────────────────


def _live_busy() -> bool:
    from app.services.camera_supervisor import supervisor

    return supervisor.events_in_progress() > 0


def system_quiet() -> bool:
    if _live_busy():
        return False
    try:
        return os.getloadavg()[0] < QUIET_LOAD * (os.cpu_count() or 1)
    except OSError:
        return True


async def _yield_to_live(deadline: float) -> None:
    """Between frames: while someone is in view on a camera, wait (live
    checks come first) - unless this visit has waited long enough."""
    while _live_busy() and time.time() < deadline:
        await asyncio.sleep(1.0)


# ── one camera of one visit ─────────────────────────────────────────────────


@dataclass
class CameraResult:
    camera_id: int
    tracker: ReviewTracker
    frames: int = 0
    event_shots: dict = field(default_factory=dict)  # event id -> (distance s, jpeg)


async def review_camera(settings, camera: Camera, pieces: list[Piece], targets: dict[int, float],
                        deadline: float) -> CameraResult:
    from app.services.ai.pipeline import ai_pipeline
    from app.services.ai.scheduler import P_BACKGROUND, SKIPPED
    from app.services.faces import face_service, faces_enabled, threshold_for
    from app.services.motion_detector import point_in_watch_area

    zones = camera.motion_zones
    tracker = ReviewTracker(in_zone=lambda x, y: point_in_watch_area(zones, x, y),
                            rules=[z for z in (zones or []) if z.get("kind") in ("zone", "line")])
    res = CameraResult(camera.id, tracker)
    high = (getattr(settings, "detection_confidence", 50) or 50) / 100.0
    faces_on = faces_enabled(settings)
    thr = threshold_for(settings) if faces_on else 1.0
    failures = 0
    for piece in pieces:
        async for wall, frame in read_frames(piece):
            await _yield_to_live(deadline)
            res.frames += 1
            h, w = frame.shape[:2]
            small = cv2.resize(frame, (DETECT_WIDTH, int(h * DETECT_WIDTH / w)),
                               interpolation=cv2.INTER_AREA) if w > DETECT_WIDTH else frame
            objects = await ai_pipeline.detect_frame(settings, small, camera.id, P_BACKGROUND)
            if objects is SKIPPED:
                continue
            if objects is None:
                failures += 1
                if failures >= 20:
                    raise RuntimeError("the AI service isn't answering")
                continue
            failures = 0
            tracker.update(objects, wall, wall, frame, high=high)
            if faces_on:
                due = [t for t in tracker.visible_now(wall, "person")
                       if t.identity is None and t.face_tries < FACE_MAX_TRIES and wall - t.last_face_try >= FACE_EVERY]
                if due:
                    for t in due:
                        t.face_tries += 1
                        t.last_face_try = wall
                    try:
                        # camera_id None: not counted in the live capture statistics.
                        results = await face_service.recognise(settings, frame, [t.box for t in due], camera_id=None)
                    except Exception as exc:
                        logger.debug("Visit review: face search failed (%s)", exc)
                        results = []
                    for idx, face, match in results:
                        vote_face(due[idx], face, match, thr, wall)
            for eid, at in targets.items():
                d = abs(wall - at)
                if d < 2.0 and d < res.event_shots.get(eid, (9e9, None))[0]:
                    boxes = [(o.label, o.confidence, (o.x, o.y, o.w, o.h)) for o in objects if o.confidence >= high]
                    res.event_shots[eid] = (d, await asyncio.to_thread(lambda: _jpeg(draw_boxes(frame, boxes))))
    for t in tracker.tracks:
        if t.left_at is None:
            t.left_at = t.last_seen
    return res


# ── people ──────────────────────────────────────────────────────────────────


def _kind(t: Track) -> tuple[str, str | None]:
    from app.services.faces import face_service

    if t.identity:
        return "known", t.identity["name"]
    if t.possibly:
        return "probable", t.possibly
    if t.face_votes:
        person = face_service.person(t.face_votes.most_common(1)[0][0])
        return "probable", person.name if person else None
    if t.best_face is not None and getattr(t.best_face, "embedding", None) is not None:
        return "stranger", None
    return "unseen", None


def _face_cos(a: Track, b: Track) -> float:
    ea, eb = getattr(a.best_face, "embedding", None), getattr(b.best_face, "embedding", None)
    if ea is None or eb is None:
        return -1.0
    ea, eb = np.asarray(ea, np.float32), np.asarray(eb, np.float32)
    return float(np.dot(ea, eb) / ((np.linalg.norm(ea) * np.linalg.norm(eb)) or 1.0))


def people_of(results: list[CameraResult], threshold: float) -> list[Person]:
    """Everyone in the visit, once each: a person seen on two cameras, or who
    left the view and came back, is one person - by name, by face, or (same
    camera, the live tracker's rule) by clothing."""
    people: list[Person] = []
    for res in results:
        for t in res.tracker.confirmed():
            if t.group != "person":
                continue
            if t.reid_of is not None and not _identities_differ(t, t.reid_of):
                continue  # came back into view: already counted
            kind, name = _kind(t)
            same = None
            for p in people:
                if kind in ("known", "probable") and p.kind == kind and p.name == name:
                    same = p
                elif kind == "stranger" and p.kind == "stranger" and _face_cos(p.track, t) >= threshold:
                    same = p
                elif (p.camera_id != res.camera_id and kind in ("stranger", "unseen") and p.kind in ("stranger", "unseen")
                      and p.track.sig is not None and t.sig is not None
                      and signature_similarity(p.track.sig, t.sig) >= REID_SIM and abs(p.track.last_seen - t.first_seen) < 120):
                    same = p
                if same:
                    break
            if same is None:
                people.append(Person(kind, name, res.camera_id, t, [t]))
            else:
                same.tracks.append(t)
                if t.best_score > same.track.best_score:
                    same.track = t
                if kind == "stranger" and same.kind == "unseen":
                    same.kind = "stranger"
    order = {"known": 0, "probable": 1, "stranger": 2, "unseen": 3}
    return sorted(people, key=lambda p: (order[p.kind], p.name or ""))


def people_line(people: list[Person]) -> str:
    names = [p.name for p in people if p.kind == "known"]
    maybe = [f"possibly {p.name}" for p in people if p.kind == "probable" and p.name]
    strangers = sum(1 for p in people if p.kind == "stranger")
    unseen = sum(1 for p in people if p.kind == "unseen" or (p.kind == "probable" and not p.name))
    bits = names + maybe
    if strangers:
        bits.append(f"{strangers} stranger{'s' if strangers > 1 else ''}")
    if unseen:
        bits.append(f"{plural('person', unseen)} (face not visible)")
    return ", ".join(bits)


# ── pictures ────────────────────────────────────────────────────────────────


def _visit_dir(incident_id: int) -> str:
    from app.services.storage_manager import storage_manager

    d = os.path.join(storage_manager.cache_dir(), PHOTO_DIR, str(incident_id))
    os.makedirs(d, exist_ok=True)
    return d


def _frame_of(t: Track) -> np.ndarray | None:
    if t.best_frame is not None:
        return t.best_frame
    if t.best_jpeg:
        return cv2.imdecode(np.frombuffer(t.best_jpeg, np.uint8), cv2.IMREAD_COLOR)
    return None


def _crop(frame: np.ndarray, box: tuple, pad: float = 0.15) -> np.ndarray:
    H, W = frame.shape[:2]
    x, y, w, h = box
    x1, y1 = max(0, int((x - w * pad) * W)), max(0, int((y - h * pad) * H))
    x2, y2 = min(W, int((x + w * (1 + pad)) * W)), min(H, int((y + h * (1 + pad)) * H))
    return frame[y1:y2, x1:x2] if x2 > x1 and y2 > y1 else frame


def _write(path: str, jpeg: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(jpeg)
    os.replace(tmp, path)


def _encode(img: np.ndarray, quality: int = 90) -> bytes | None:
    ok, buf = cv2.imencode(".jpg", fit_for_storage(img), [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buf.tobytes() if ok else None


# ── the description ─────────────────────────────────────────────────────────


async def describe_visit(settings, frame: np.ndarray, people: list[Person], counts: dict, duration: float,
                         camera_names: list[str]) -> str:
    if not (settings and settings.enabled and settings.vlm_enabled and getattr(settings, "event_summary_vlm", True)):
        return ""
    if not any(group_of(lbl) in FOCUS_GROUPS for lbl in counts):
        return ""  # the describer is kept for people and vehicles
    from app.services.ai.vlm import DescriberBusy, describe_frame, timeout_for
    from app.services.faces import privacy_safe_jpeg

    jpeg = await privacy_safe_jpeg(settings, frame)
    if not jpeg:
        return ""
    seen = ", ".join(plural(lbl, n) for lbl, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    who = people_line(people)
    prompt = (
        f"This is the clearest picture of a {format_duration(duration)} visit to a home, seen on the security "
        f"camera(s) {', '.join(camera_names)}. Seen during the visit: {seen}."
        + (f" The people are: {who}." if who else "")
        + " Describe in at most two factual sentences (40 words) what is happening. Use the names given if "
          "you refer to those people. No preamble, no speculation beyond the image."
    )
    for attempt in range(4):
        try:
            return (await asyncio.wait_for(describe_frame(settings, jpeg, list(counts), "visit", prompt=prompt),
                                           timeout=timeout_for(settings))).strip()
        except DescriberBusy:
            await asyncio.sleep(60)  # finishing earlier pictures; this one isn't in a hurry
        except Exception as exc:
            logger.warning("Visit review: description failed (%s)", exc)
            return ""
    return ""


# ── one visit ───────────────────────────────────────────────────────────────


def _windows(events: list[Event], row: Incident) -> dict[int, tuple[float, float]]:
    """Each camera's part of the visit, from its events."""
    spans: dict[int, list[float]] = {}
    for e in events:
        if e.camera_id is None:
            continue
        p = e.payload or {}
        if e.type == "motion_summary" and p.get("started_at"):
            # (Its own time is when it was sent, after the clip was saved.)
            pts = [float(p["started_at"]), float(p["started_at"]) + float(p.get("duration_seconds") or 0)]
        else:
            pts = [_ts(e.created_at)]
        s = spans.setdefault(e.camera_id, [min(pts), max(pts)])
        s[0], s[1] = min(s[0], *pts), max(s[1], *pts)
    for cid in row.camera_ids or []:
        spans.setdefault(cid, [_ts(row.started_at), _ts(row.ended_at) or _ts(row.started_at)])
    return {cid: (a - EDGE_PAD, b + EDGE_PAD) for cid, (a, b) in spans.items()}


async def _footage(db, camera_id: int, window: tuple[float, float]) -> tuple[list[Recording], bool]:
    """The camera's recordings over the window, and whether they reach its
    end (the last minutes may still be in a segment that's being written)."""
    a, b = window
    rows = (await db.execute(select(Recording).where(
        Recording.camera_id == camera_id, Recording.started_at <= _naive(b), Recording.ended_at >= _naive(a)
    ))).scalars().all()
    ends = [_ts(r.ended_at) for r in rows if r.ended_at]
    return list(rows), bool(ends) and max(ends) >= b - 1.0


async def ready(row: Incident) -> bool:
    """Its footage is all on disk (or it has waited long enough)."""
    if time.time() - (_ts(row.ended_at) or 0) > FOOTAGE_WAIT:
        return True
    async with AsyncSessionLocal() as db:
        events = (await db.execute(select(Event).where(Event.incident_id == row.id))).scalars().all()
        for cid, window in _windows(events, row).items():
            cam = await db.get(Camera, cid)
            if cam is None or cam.recording_mode == "off":
                continue
            _, complete = await _footage(db, cid, window)
            if not complete:
                return False
    return True


async def review_visit(incident_id: int, deadline: float | None = None) -> dict:
    """Review one visit and update it. Returns what was done (for tests and
    the log)."""
    from app.services.ai.digest import _display_tz
    from app.services.ai.pipeline import ai_pipeline
    from app.services.faces import face_service, faces_enabled, threshold_for

    settings = await ai_pipeline.settings()
    deadline = deadline or time.time() + MAX_WAIT_FOR_QUIET
    async with AsyncSessionLocal() as db:
        row = await db.get(Incident, incident_id)
        if row is None:
            return {"status": "gone"}
        events = (await db.execute(select(Event).where(Event.incident_id == incident_id))).scalars().all()
        dets = (await db.execute(select(Detection).where(Detection.incident_id == incident_id))).scalars().all()
        cameras = {c.id: c for c in (await db.execute(
            select(Camera).where(Camera.id.in_(row.camera_ids or [])))).scalars().all()}
        work = []
        for cid, window in _windows(events, row).items():
            cam = cameras.get(cid)
            if cam is None:
                continue
            recs, _ = await _footage(db, cid, window)
            focus = [(_ts(d.first_seen or d.created_at), _ts(d.last_seen or d.created_at)) for d in dets
                     if d.camera_id == cid and group_of(d.label) in FOCUS_GROUPS]
            pieces = pieces_for(recs, plan_rates(window, focus))
            # Alerts show the moment they were sent; a summary shows the
            # clearest view of the event (below, from the review's tracks).
            targets = {e.id: _ts(e.created_at) for e in events
                       if e.camera_id == cid and e.snapshot_path and e.type not in SUMMARY_TYPES}
            if pieces:
                work.append((cam, pieces, targets))
    if not (settings and settings.enabled and settings.detection_enabled):
        await _finish(incident_id, "skipped")
        return {"status": "skipped", "why": "AI detection is off"}
    if not work:
        await _finish(incident_id, "skipped")
        return {"status": "skipped", "why": "no recording of this visit"}

    started = time.monotonic()
    results = []
    for cam, pieces, targets in work:
        results.append(await review_camera(settings, cam, pieces, targets, deadline))
    if not sum(r.frames for r in results):
        await _finish(incident_id, "failed")
        return {"status": "failed", "why": "the recording couldn't be read"}
    thr = threshold_for(settings) if faces_enabled(settings) else 1.0
    people = people_of(results, thr)

    counts: dict[str, int] = {}
    for res in results:
        for label, n in res.tracker.totals().items():
            counts[label] = max(counts.get(label, 0), n)
    if people:
        counts["person"] = len(people)

    vdir = await asyncio.to_thread(_visit_dir, incident_id)
    people_rows = []
    for i, p in enumerate(people):
        frame = _frame_of(p.track)
        photo = full = None
        if frame is not None:
            photo, full = os.path.join(vdir, f"p{i}.jpg"), os.path.join(vdir, f"p{i}_full.jpg")
            crop_jpeg = _encode(_crop(frame, p.track.best_box), 92)
            full_jpeg = _encode(draw_boxes(frame, [(p.name or "person", p.track.best_conf, p.track.best_box)]))
            if crop_jpeg and full_jpeg:
                await asyncio.to_thread(_write, photo, crop_jpeg)
                await asyncio.to_thread(_write, full, full_jpeg)
            else:
                photo = full = None
        people_rows.append({"kind": p.kind, "name": p.name, "camera_id": p.camera_id,
                            "at": round(p.track.best_at, 3), "photo": photo, "full": full})

    # Better faces for "Who is this?", from the main stream.
    if faces_enabled(settings):
        for p in people:
            t = p.track
            if t.best_face is None or getattr(t.best_face, "embedding", None) is None:
                continue
            try:
                pid = await face_service.learnable(settings, t.identity, t.best_face)
                await face_service.save_sample(t.best_face, pid, p.camera_id, t.best_face_score,
                                               f"review:{incident_id}:{p.camera_id}:{t.id}",
                                               source="auto" if pid else "capture", seen_at=t.best_face_at)
            except Exception:
                logger.exception("Visit review: could not keep a face")

    # The visit's picture: the clearest view of a person (or else anything).
    best = max((t for res in results for t in res.tracker.confirmed() if _frame_of(t) is not None),
               key=lambda t: (t.group == "person", t.best_score), default=None)
    snapshot = None
    best_frame = None
    if best is not None:
        best_frame = _frame_of(best)
        snapshot = _encode(draw_boxes(best_frame, [(best.label, best.best_conf, best.best_box)]))

    by_cam = {res.camera_id: res for res in results}
    names = [cameras[c].name for c in row.camera_ids or [] if c in cameras]
    duration = (_ts(row.ended_at) or _ts(row.started_at)) - _ts(row.started_at)
    description = ""
    if best_frame is not None:
        description = await describe_visit(settings, best_frame, people, counts, duration, names)

    tz = await _display_tz()
    async with AsyncSessionLocal() as db:
        row = await db.get(Incident, incident_id)
        if row is None:
            return {"status": "gone"}
        # Event pictures: alerts get the main-stream frame nearest them, a
        # camera's summary its clearest view of the event.
        for e in (await db.execute(select(Event).where(Event.incident_id == incident_id))).scalars().all():
            res = by_cam.get(e.camera_id)
            if res is None or not e.snapshot_path:
                continue
            jpeg = None
            if e.type in SUMMARY_TYPES:
                t = max((t for t in res.tracker.confirmed() if _frame_of(t) is not None),
                        key=lambda t: (t.group == "person", t.best_score), default=None)
                if t is not None:
                    jpeg = _encode(draw_boxes(_frame_of(t), [(t.label, t.best_conf, t.best_box)]))
            else:
                shot = res.event_shots.get(e.id)
                jpeg = shot[1] if shot else None
            if jpeg:
                await asyncio.to_thread(_write, e.snapshot_path, jpeg)
        # Each detected object's picture: its review track's clearest view.
        for d in (await db.execute(select(Detection).where(Detection.incident_id == incident_id))).scalars().all():
            res = by_cam.get(d.camera_id)
            if res is None or not d.snapshot_path:
                continue
            t = _match_track(res.tracker, d)
            frame = _frame_of(t) if t is not None else None
            if frame is not None:
                jpeg = _encode(draw_boxes(frame, [(t.label, t.best_conf, t.best_box)]))
                if jpeg:
                    await asyncio.to_thread(_write, d.snapshot_path, jpeg)
        if snapshot:
            path = os.path.join(vdir, "visit.jpg")
            await asyncio.to_thread(_write, path, snapshot)
            old = row.snapshot_path
            row.snapshot_path = path
            if old and old != path and os.path.basename(os.path.dirname(old)) == "incidents":
                try:
                    os.remove(old)
                except OSError:
                    pass
        known = sorted({p.name for p in people if p.kind == "known" and p.name})
        row.identities = sorted(set(row.identities or []) | set(known)) or None
        row.people = people_rows or None
        row.strangers = sum(1 for p in people if p.kind == "stranger")
        row.counts = counts or row.counts
        row.description = description or None
        row.summary = visit_summary(row, people, counts, description, names, tz)
        row.review_status = "done"
        row.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()
        summary, path = row.summary, row.snapshot_path
        # The same facts as a live alert, so alert rules ("someone unknown",
        # "Manoj arrived") apply to the visit message too.
        ids = [p.track.identity for p in people if p.kind == "known" and p.track.identity]
        payload = {"incident_id": incident_id, "cameras": row.camera_ids, "areas": row.areas_path,
                   "counts": counts, "objects": sorted(counts), "names": known, "strangers": row.strangers,
                   "duration_seconds": round(duration), "reviewed": True}
        if ids:
            payload["identities"] = sorted({f"person:{i['id']}" for i in ids})
            payload["categories"] = sorted({i.get("category") or "" for i in ids})
        if row.strangers:
            payload["unknown_faces"] = True
    await _send(summary, path, payload, incident_id, counts)
    took = round(time.monotonic() - started)
    logger.info("Visit review %s: %d frames, %s - %ss", incident_id, sum(r.frames for r in results),
                people_line(people) or "nobody", took)
    return {"status": "done", "frames": sum(r.frames for r in results), "people": people_rows, "counts": counts,
            "description": description, "seconds": took}


def _match_track(tracker: ObjectTracker, d: Detection) -> Track | None:
    a, b = _ts(d.first_seen or d.created_at), _ts(d.last_seen or d.created_at)
    best, best_overlap = None, -1e9
    for t in tracker.confirmed():
        if t.group != group_of(d.label):
            continue
        overlap = min(b, t.last_seen) - max(a, t.first_seen)
        if overlap > best_overlap:
            best, best_overlap = t, overlap
    return best if best is not None and best_overlap > -5.0 else None


def visit_summary(row: Incident, people: list[Person], counts: dict, description: str, camera_names: list[str],
                  tz=timezone.utc) -> str:
    start = datetime.fromtimestamp(_ts(row.started_at), tz)
    end = datetime.fromtimestamp(_ts(row.ended_at) or _ts(row.started_at), tz)
    dur = format_duration(end.timestamp() - start.timestamp())
    lines = [f"Visit {start.strftime('%d %b %H:%M:%S')} - {end.strftime('%H:%M:%S')} {start.strftime('%Z')} ({dur})".rstrip()]
    if description:
        lines.append(description)
    who = people_line(people)
    if who:
        lines.append("People: " + who)
    others = [plural(lbl, n) for lbl, n in sorted(counts.items(), key=lambda kv: -kv[1]) if lbl != "person"]
    if others:
        lines.append("Also seen: " + ", ".join(others))
    if row.areas_path:
        lines.append("Route: " + " -> ".join(row.areas_path))
    if camera_names:
        lines.append("Cameras: " + ", ".join(camera_names))
    message = "\n".join(lines)
    return message if len(message) <= 950 else message[:947].rsplit("\n", 1)[0] + "\n..."


async def _send(summary: str, snapshot_path: str | None, payload: dict, incident_id: int, counts: dict) -> None:
    """One message per visit, once it's reviewed - only for visits with
    something in them (motion alone isn't worth a second message)."""
    if not counts:
        return
    from app.services.events import emit_event

    try:
        await emit_event(None, "incident_summary", summary, snapshot_path=snapshot_path, payload=payload,
                         incident_id=incident_id)
    except Exception:
        logger.exception("Visit review: could not send the visit message")


async def _finish(incident_id: int, status: str, send: bool = True) -> None:
    """A visit that won't be reviewed: marked so, and its quick visit summary
    is sent instead (as before the review existed)."""
    async with AsyncSessionLocal() as db:
        row = await db.get(Incident, incident_id)
        if row is None:
            return
        row.review_status = status
        row.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()
    if not send:
        return
    from app.services.incident_store import send_unreviewed_summary

    try:
        await send_unreviewed_summary(incident_id)
    except Exception:
        logger.exception("Could not send the visit summary")


# ── the loop ────────────────────────────────────────────────────────────────


async def _pending() -> list[Incident]:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Incident).where(
            Incident.status == "closed", Incident.review_status == "pending"
        ).order_by(Incident.id).limit(20))).scalars().all()
        for r in rows:
            db.expunge(r)
    return list(rows)


async def review_once() -> int | None:
    """Review the oldest visit that's ready, if the system is quiet (or it
    has waited long enough). Returns the visit reviewed."""
    now = time.time()
    for row in await _pending():
        ended = _ts(row.ended_at) or _ts(row.started_at) or now
        if now - ended > TOO_OLD:
            await _finish(row.id, "skipped", send=False)  # days old: not worth a message now
            continue
        if not await ready(row):
            continue
        waited = now - ended
        if not system_quiet() and waited < MAX_WAIT_FOR_QUIET:
            return None  # oldest first: wait for quiet rather than jump the queue
        try:
            await review_visit(row.id, deadline=ended + MAX_WAIT_FOR_QUIET)
        except Exception as exc:
            logger.exception("Visit review %s failed", row.id)
            await _finish(row.id, "failed")
            logger.warning("Visit review %s: %s", row.id, exc)
        return row.id
    return None


async def review_loop() -> None:
    while True:
        try:
            done = await review_once()
        except Exception:
            logger.exception("Visit review loop failed")
            done = None
        if done is None:
            await asyncio.sleep(POLL_SECONDS)
