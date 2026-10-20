import asyncio
from types import SimpleNamespace

import numpy as np

from app.services.faces import FaceService, Match, threshold_for
from app.services.motion_event import MotionEventSession
from app.vision.face import Face, cosine
from tests.test_motion_event import Harness, person, settings


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def vec(seed, noise=0.0, base=None):
    rng = np.random.default_rng(seed)
    b = base if base is not None else rng.normal(size=128)
    return unit(b + noise * np.random.default_rng(seed + 1000).normal(size=128))


def service_with(people: dict[int, list[np.ndarray]]):
    svc = FaceService()
    svc._people = {pid: SimpleNamespace(id=pid, name=f"P{pid}", note=None, category="household") for pid in people}
    svc._gallery = [(pid, e) for pid, embs in people.items() for e in embs]
    svc._loaded = True
    return svc


def test_threshold_mapping_default_is_strict():
    assert abs(threshold_for(SimpleNamespace(face_threshold=60)) - 0.45) < 1e-9
    assert threshold_for(SimpleNamespace(face_threshold=1)) < threshold_for(SimpleNamespace(face_threshold=99))


def test_same_person_matches_with_margin():
    ramesh = vec(1)
    svc = service_with({1: [vec(1, 0.3, ramesh), vec(1, 0.3, ramesh)], 2: [vec(2)]})
    m = svc.match(vec(1, 0.35, ramesh), 0.45)
    assert m.band == "known" and m.person_id == 1


def test_stranger_is_unknown():
    svc = service_with({1: [vec(1)], 2: [vec(2)]})
    m = svc.match(vec(99), 0.45)
    assert m.band == "unknown" and m.person_id is None


def test_lookalikes_without_margin_are_only_probable():
    base = vec(5)
    a, b = vec(6, 0.05, base), vec(7, 0.05, base)  # two very similar people
    svc = service_with({1: [a], 2: [b]})
    m = svc.match(vec(8, 0.05, base), 0.45)
    assert m.band == "probable"  # never "known" when two people are this close


def test_cosine_of_normalised_vectors():
    v = vec(3)
    assert abs(cosine(v, v) - 1.0) < 1e-6


def _face(q=0.9):
    return Face(0.4, 0.3, 0.05, 0.08, 0.95, q, unit(np.ones(128)))


def test_session_needs_two_agreeing_sightings_then_reports_identity(monkeypatch):
    monkeypatch.setattr(MotionEventSession, "FACE_RETRY", 0.15)
    calls = []

    async def recognise(frame, boxes):
        calls.append(len(boxes))
        return [(0, _face(), Match(3, "Ramesh", "milkman", "staff", 0.47, "known"))]

    identified = []

    async def on_identity(track):
        identified.append(track.identity["name"])

    h = Harness(lambda t: [person(0.4)], settings(monitor_interval_ms=100, face_threshold=60))

    async def go():
        s = h.session()
        s._recognise = recognise
        s._on_identity = on_identity
        s.start()
        await asyncio.sleep(1.2)
        s.mark_motion_end()
        await s.stop_monitoring()
        return s, await s.build_summary()

    s, summary = asyncio.run(go())
    assert identified == ["Ramesh"]  # once, after the 2nd agreeing sighting
    assert len(calls) == 2
    assert "Seen: 1 person (Ramesh)" in summary.message
    assert s.identities()[0]["id"] == 3


def test_one_clear_sighting_is_enough(monkeypatch):
    async def recognise(frame, boxes):
        return [(0, _face(), Match(3, "Ramesh", None, "staff", 0.60, "known"))]

    h = Harness(lambda t: [person(0.4)], settings(monitor_interval_ms=100, face_threshold=60))

    async def go():
        s = h.session()
        s._recognise = recognise
        s.start()
        await asyncio.sleep(0.6)
        await s.stop_monitoring()
        return s

    s = asyncio.run(go())
    assert s.identities() and s.identities()[0]["name"] == "Ramesh"


def test_probable_and_unknown_never_count_as_recognised(monkeypatch):
    monkeypatch.setattr(MotionEventSession, "FACE_RETRY", 0.1)

    async def recognise(frame, boxes):
        return [(0, _face(), Match(3, "Ramesh", None, "staff", 0.40, "probable"))]

    h = Harness(lambda t: [person(0.4)], settings(monitor_interval_ms=100, face_threshold=60))

    async def go():
        s = h.session()
        s._recognise = recognise
        s.start()
        await asyncio.sleep(1.0)
        await s.stop_monitoring()
        return s

    s = asyncio.run(go())
    assert s.identities() == []
    assert s.tracker.confirmed()[0].possibly == "Ramesh"
    payload = s._payload(s.tracker.confirmed())
    assert "identities" not in payload


def test_unknown_face_flags_payload_for_rules():
    async def recognise(frame, boxes):
        return [(0, _face(), Match(None, None, None, None, 0.1, "unknown"))]

    h = Harness(lambda t: [person(0.4)], settings(monitor_interval_ms=100, face_threshold=60))

    async def go():
        s = h.session()
        s._recognise = recognise
        s.start()
        await asyncio.sleep(0.6)
        await s.stop_monitoring()
        return s

    s = asyncio.run(go())
    assert s._payload(s.tracker.confirmed()).get("unknown_faces") is True


def test_cloud_detection_for_vision_models():
    from app.services.faces import vlm_is_cloud

    assert vlm_is_cloud(SimpleNamespace(vlm_provider="anthropic", vlm_url=""))
    assert vlm_is_cloud(SimpleNamespace(vlm_provider="openai_compatible", vlm_url="https://api.openai.com/v1"))
    assert not vlm_is_cloud(SimpleNamespace(vlm_provider="openai_compatible", vlm_url="http://192.168.1.20:1234/v1"))
    assert not vlm_is_cloud(SimpleNamespace(vlm_provider="ollama", vlm_url="http://10.0.0.5:11434"))


def test_faces_never_sent_to_cloud_when_they_cant_be_found(monkeypatch):
    from app.services import faces as faces_mod

    s = SimpleNamespace(enabled=True, face_enabled=True, privacy_ack=True, vlm_provider="anthropic", vlm_url="",
                        backend="local", remote_url="")

    async def broken(*a, **k):
        raise FileNotFoundError("models missing")

    monkeypatch.setattr(faces_mod.face_service, "find", broken)
    out = asyncio.run(faces_mod.privacy_safe_jpeg(s, np.zeros((50, 50, 3), np.uint8)))
    assert out is None
