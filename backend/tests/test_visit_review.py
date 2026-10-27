"""The visit review (services/visit_review.py): after a visit, the AI goes over
its main-stream recording at 10 frames a second - names who it knows, tells
strangers from faces that never showed, describes the visit once, replaces its
pictures with main-stream ones and sends one message per visit. It waits for a
quiet system, but never forever."""

import asyncio
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import visit_review as vr
from app.services.ai.types import DetectedObject
from app.services.motion_event import Track

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _naive(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


# ── how the recording is read ───────────────────────────────────────────────


def test_a_short_visit_is_read_at_ten_frames_a_second_throughout():
    assert vr.plan_rates((0, 90), []) == [(0, 90, 10.0)]


def test_a_long_visit_is_read_closely_only_where_someone_was_seen():
    stretches = vr.plan_rates((0, 600), [(100, 130), (125, 140), (400, 410)])
    assert stretches == [(0, 95, 2.0), (95, 145, 10.0), (145, 395, 2.0), (395, 415, 10.0), (415, 600, 2.0)]


def test_a_very_long_visit_keeps_the_close_looks_within_the_cap(monkeypatch):
    monkeypatch.setattr(vr, "MAX_SECONDS_PER_CAMERA", 60)
    stretches = vr.plan_rates((0, 3000), [(100, 140), (1000, 1100)])
    assert all(fps == 10.0 for _, _, fps in stretches)
    assert sum(b - a for a, b, _ in stretches) == pytest.approx(60)


def test_a_stretch_over_two_segments_reads_both_files(tmp_path):
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    t0 = 1_800_000_000.0
    recs = [SimpleNamespace(file_path=str(a), started_at=_naive(t0), ended_at=_naive(t0 + 300), duration_seconds=300),
            SimpleNamespace(file_path=str(b), started_at=_naive(t0 + 300), ended_at=_naive(t0 + 600), duration_seconds=300)]
    pieces = vr.pieces_for(recs, [(t0 + 290, t0 + 310, 10.0)])
    assert [(os.path.basename(p.path), round(p.inpoint), round(p.duration), p.wall - t0) for p in pieces] == [
        ("a.mp4", 290, 10, 290.0), ("b.mp4", 0, 10, 300.0)]


def _clip(path, seconds=4, size="1920x1080"):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"testsrc=size={size}:rate=25:duration={seconds}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-y", str(path)], check=True)


@needs_ffmpeg
def test_frames_come_at_the_rate_asked_and_full_resolution(tmp_path):
    clip = tmp_path / "main.mp4"
    _clip(clip)

    async def read(fps):
        return [(w, f.shape) async for w, f in vr.read_frames(vr.Piece(str(clip), 1.0, 2.0, 1000.0, fps))]

    ten = asyncio.run(read(10.0))
    two = asyncio.run(read(2.0))
    assert 19 <= len(ten) <= 21 and 3 <= len(two) <= 5
    assert ten[0][1] == (1080, 1920, 3)
    assert ten[1][0] - ten[0][0] == pytest.approx(0.1)
    assert ten[0][0] == 1000.0


# ── who was there ───────────────────────────────────────────────────────────


def _track(n, *, identity=None, possibly=None, face=False, sig=None, seen=(0.0, 5.0)):
    t = Track(id=n, group="person", first_seen=seen[0], last_seen=seen[1], box=(0.1, 0.1, 0.2, 0.5))
    t.announced = t.confirmed = True
    t.labels["person"] = 5
    t.identity = identity
    t.possibly = possibly
    t.best_score = n
    if face:
        t.best_face = SimpleNamespace(embedding=np.asarray(face, np.float32), quality=0.8)
    t.sig = sig
    return t


def _result(camera_id, tracks):
    res = vr.CameraResult(camera_id, vr.ReviewTracker())
    res.tracker.tracks = tracks
    return res


def test_known_people_strangers_and_unseen_faces_are_told_apart():
    manoj = {"id": 3, "name": "Manoj Sharma", "category": "household"}
    people = vr.people_of([_result(1, [
        _track(1, identity=manoj, face=[1, 0, 0]),
        _track(2, possibly="Nidhi Tayal", face=[0, 1, 0]),
        _track(3, face=[0, 0, 1]),
        _track(4),
    ])], threshold=0.45)
    assert [(p.kind, p.name) for p in people] == [("known", "Manoj Sharma"), ("probable", "Nidhi Tayal"),
                                                   ("stranger", None), ("unseen", None)]
    assert vr.people_line(people) == "Manoj Sharma, possibly Nidhi Tayal, 1 stranger, 1 person (face not visible)"


def test_the_same_person_on_two_cameras_is_one_person():
    manoj = {"id": 3, "name": "Manoj Sharma", "category": "household"}
    people = vr.people_of([
        _result(1, [_track(1, identity=manoj), _track(2, face=[0.6, 0.8, 0])]),
        _result(2, [_track(1, identity=manoj), _track(2, face=[0.62, 0.78, 0])]),  # the same stranger's face
    ], threshold=0.45)
    assert [(p.kind, p.name) for p in people] == [("known", "Manoj Sharma"), ("stranger", None)]


def test_two_different_strangers_stay_two():
    people = vr.people_of([_result(1, [_track(1, face=[1, 0, 0]), _track(2, face=[0, 1, 0])])], threshold=0.45)
    assert vr.people_line(people) == "2 strangers"


# ── a whole visit ───────────────────────────────────────────────────────────


@pytest.fixture
def nvr(tmp_path, monkeypatch):
    import app.models  # noqa: F401
    from app.db.session import Base
    from app.services import events, faces, incident_store
    from app.services.ai import pipeline, vlm
    from app.services.storage_manager import storage_manager

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (vr, incident_store):
        monkeypatch.setattr(mod, "AsyncSessionLocal", maker)
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(storage_manager, "cache_dir", lambda: str(cache))
    monkeypatch.setattr(vr, "_live_busy", lambda: False)

    settings = SimpleNamespace(enabled=True, detection_enabled=True, detection_confidence=50, face_enabled=True,
                               privacy_ack=True, vlm_enabled=True, event_summary_vlm=True, vlm_provider="ollama",
                               vlm_url="http://describer")
    seen = {"detect": 0, "faces": 0, "describe": [], "sent": []}

    async def get_settings():
        return settings

    async def detect(s, frame, camera_id, priority):
        seen["detect"] += 1
        seen.setdefault("widths", set()).add(frame.shape[1])
        return [DetectedObject("person", 0.9, 0.4, 0.3, 0.1, 0.4)]

    async def recognise(s, frame, boxes, camera_id=None):
        seen["faces"] += 1
        seen.setdefault("face_widths", set()).add(frame.shape[1])
        face = faces.Face(0.42, 0.32, 0.03, 0.05, 0.95, 0.8, embedding=np.ones(128, np.float32) / 128 ** 0.5,
                          crop=np.zeros((112, 112, 3), np.uint8))
        return [(0, face, faces.Match(3, "Manoj Sharma", None, "household", 0.7, "known"))]

    async def describe(s, jpeg, labels, camera, prompt=None):
        seen["describe"].append(prompt)
        return "Manoj walks up to the front door."

    async def private(s, frame, quality=90):
        return b"jpeg"

    async def emit(camera_id, kind, message, snapshot_path=None, payload=None, incident_id=None, **k):
        seen["sent"].append({"kind": kind, "message": message, "payload": payload, "incident_id": incident_id})

    async def learnable(*a):
        return None

    async def save_sample(*a, **k):
        seen["samples"] = seen.get("samples", 0) + 1
        return 1

    monkeypatch.setattr(pipeline.ai_pipeline, "settings", get_settings)
    monkeypatch.setattr(pipeline.ai_pipeline, "detect_frame", detect)
    monkeypatch.setattr(faces.face_service, "recognise", recognise)
    monkeypatch.setattr(faces.face_service, "learnable", learnable)
    monkeypatch.setattr(faces.face_service, "save_sample", save_sample)
    monkeypatch.setattr(faces, "threshold_for", lambda s, m=None: 0.45)
    monkeypatch.setattr(faces, "privacy_safe_jpeg", private)
    monkeypatch.setattr(vlm, "describe_frame", describe)
    monkeypatch.setattr(events, "emit_event", emit)
    from app.services.ai import digest

    async def utc():
        return timezone.utc

    monkeypatch.setattr(digest, "_display_tz", utc)

    async def create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(create())
    return SimpleNamespace(maker=maker, cache=cache, tmp=tmp_path, seen=seen, settings=settings)


def _small_jpeg(path):
    cv2.imwrite(str(path), np.zeros((180, 320, 3), np.uint8))
    return str(path)


def _visit(nvr, clip=True, cameras=(1,)):
    from app.models.camera import Camera
    from app.models.detection import Detection
    from app.models.event import Event
    from app.models.incident import Incident
    from app.models.recording import Recording

    t0 = time.time() - 600
    path = nvr.tmp / "main.mp4"
    if clip:
        _clip(path)

    async def go():
        async with nvr.maker() as db:
            for cid in cameras:
                db.add(Camera(id=cid, name=f"Porch {cid}", rtsp_main_url="rtsp://x", recording_mode="continuous"))
            if clip:
                db.add(Recording(camera_id=cameras[0], file_path=str(path), storage_tier="cache", trigger="continuous",
                                 started_at=_naive(t0), ended_at=_naive(t0 + 4), duration_seconds=4.0))
            inc = Incident(started_at=_naive(t0 + 0.5), ended_at=_naive(t0 + 3.5), status="closed",
                           camera_ids=list(cameras), areas_path=["Porch"], counts={"person": 1},
                           summary="Visit (quick)", review_status="pending")
            db.add(inc)
            await db.flush()
            db.add(Event(camera_id=cameras[0], type="motion", message="Person on 'Porch'", incident_id=inc.id,
                         created_at=_naive(t0 + 1), snapshot_path=_small_jpeg(nvr.tmp / "alert.jpg")))
            db.add(Event(camera_id=cameras[0], type="motion_summary", message="Motion on 'Porch'", incident_id=inc.id,
                         created_at=_naive(t0 + 40), snapshot_path=_small_jpeg(nvr.tmp / "summary.jpg"),
                         payload={"started_at": t0 + 0.5, "duration_seconds": 3.0}))
            db.add(Detection(camera_id=cameras[0], label="person", confidence=0.8, incident_id=inc.id,
                             first_seen=_naive(t0 + 1), last_seen=_naive(t0 + 3),
                             snapshot_path=_small_jpeg(nvr.tmp / "det.jpg")))
            await db.commit()
            return inc.id

    return asyncio.run(go())


def _row(nvr, incident_id):
    from app.models.incident import Incident

    async def go():
        async with nvr.maker() as db:
            return await db.get(Incident, incident_id)

    return asyncio.run(go())


@needs_ffmpeg
def test_a_reviewed_visit_names_people_describes_it_and_uses_main_stream_pictures(nvr):
    vid = _visit(nvr)
    out = asyncio.run(vr.review_visit(vid))
    assert out["status"] == "done"
    assert 35 <= out["frames"] <= 42  # 4 s of recording at 10 frames a second
    assert nvr.seen["widths"] == {1280} and nvr.seen["face_widths"] == {1920}  # faces on the full frame

    row = _row(nvr, vid)
    assert row.review_status == "done" and row.reviewed_at is not None
    assert row.identities == ["Manoj Sharma"] and row.strangers == 0
    assert [(p["kind"], p["name"]) for p in row.people] == [("known", "Manoj Sharma")]
    assert os.path.exists(row.people[0]["photo"]) and os.path.exists(row.people[0]["full"])
    assert row.description == "Manoj walks up to the front door."
    assert "Manoj walks up to the front door." in row.summary and "People: Manoj Sharma" in row.summary
    assert "Manoj Sharma" in nvr.seen["describe"][0]  # the describer is told who it is

    # Every picture of the visit is now a main-stream one.
    for name in ("alert.jpg", "summary.jpg", "det.jpg"):
        assert cv2.imread(str(nvr.tmp / name)).shape[1] == 1920, name
    assert cv2.imread(row.snapshot_path).shape[1] == 1920
    assert nvr.seen.get("samples") == 1  # a main-stream face for "Who is this?"

    # One message for the visit, with what alert rules look at.
    assert [m["kind"] for m in nvr.seen["sent"]] == ["incident_summary"]
    msg = nvr.seen["sent"][0]
    assert msg["incident_id"] == vid and msg["payload"]["identities"] == ["person:3"]
    assert "People: Manoj Sharma" in msg["message"]


@needs_ffmpeg
def test_a_stranger_is_counted_and_flagged_for_alert_rules(nvr, monkeypatch):
    from app.services import faces

    async def stranger(s, frame, boxes, camera_id=None):
        face = faces.Face(0.42, 0.32, 0.03, 0.05, 0.95, 0.8, embedding=np.ones(128, np.float32) / 128 ** 0.5,
                          crop=np.zeros((112, 112, 3), np.uint8))
        return [(0, face, faces.Match(None, None, None, None, 0.1, "unknown"))]

    monkeypatch.setattr(faces.face_service, "recognise", stranger)
    vid = _visit(nvr)
    asyncio.run(vr.review_visit(vid))
    row = _row(nvr, vid)
    assert row.strangers == 1 and not row.identities
    assert "People: 1 stranger" in row.summary
    assert nvr.seen["sent"][0]["payload"]["unknown_faces"] is True


def test_a_visit_without_a_recording_is_skipped_and_gets_its_quick_summary(nvr, monkeypatch):
    from app.services import incident_store

    sent = []

    async def fallback(incident_id):
        sent.append(incident_id)

    monkeypatch.setattr(incident_store, "send_unreviewed_summary", fallback)
    vid = _visit(nvr, clip=False)
    out = asyncio.run(vr.review_visit(vid))
    assert out["status"] == "skipped"
    assert _row(nvr, vid).review_status == "skipped"
    assert sent == [vid]
    assert nvr.seen["sent"] == []


# ── when it runs ────────────────────────────────────────────────────────────


def test_it_waits_for_a_quiet_system_but_not_forever(nvr, monkeypatch):
    from app.models.incident import Incident

    done = []

    async def review(incident_id, deadline=None):
        done.append(incident_id)
        return {"status": "done"}

    async def ready(row):
        return True

    monkeypatch.setattr(vr, "review_visit", review)
    monkeypatch.setattr(vr, "ready", ready)
    monkeypatch.setattr(vr, "system_quiet", lambda: False)
    vid = _visit(nvr, clip=False)  # ended 10 minutes ago
    assert asyncio.run(vr.review_once()) is None and done == []  # busy: wait

    async def older():
        async with nvr.maker() as db:
            row = await db.get(Incident, vid)
            row.ended_at = _naive(time.time() - 31 * 60)
            await db.commit()

    asyncio.run(older())
    assert asyncio.run(vr.review_once()) == vid and done == [vid]  # waited 30 minutes: goes anyway


def test_a_restart_leaves_visits_to_review(nvr):
    from app.models.incident import Incident
    from app.services import incident_store
    from app.services.incidents import incident_manager

    async def go():
        async with nvr.maker() as db:
            db.add(Incident(started_at=_naive(time.time() - 60), status="open", camera_ids=[1]))
            await db.commit()
        task = asyncio.create_task(incident_store.incident_loop())
        await asyncio.sleep(0.3)
        task.cancel()
        async with nvr.maker() as db:
            return (await db.execute(select(Incident))).scalars().all()

    rows = asyncio.run(go())
    incident_manager._persist = incident_manager._emit = incident_manager._merged = None
    assert [(r.status, r.review_status) for r in rows] == [("closed", "pending")]


def test_live_events_with_someone_in_view_hold_the_review_back(monkeypatch):
    from app.services.camera_supervisor import supervisor

    busy = SimpleNamespace(_session=SimpleNamespace(tracker=SimpleNamespace(confirmed=lambda: [
        SimpleNamespace(left_at=None, parked=False)])))
    plants = SimpleNamespace(_session=SimpleNamespace(tracker=SimpleNamespace(confirmed=lambda: [])))
    monkeypatch.setattr(supervisor, "_workers", {1: plants, 2: SimpleNamespace(_session=None)})
    assert supervisor.events_in_progress() == 0  # motion with nothing in it doesn't count
    monkeypatch.setattr(supervisor, "_workers", {1: plants, 2: busy})
    assert supervisor.events_in_progress() == 1
    assert vr.system_quiet() is False


def test_the_instant_per_camera_summary_no_longer_waits_for_a_description():
    import inspect

    from app.services import camera_worker

    src = inspect.getsource(camera_worker.CameraWorker._finalize_session)
    assert "describe=False" in src
