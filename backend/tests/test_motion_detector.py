"""MotionDetector behaviour on synthetic frames.

Each test drives the detector at ~10 fps of fake time, the way the live
decoder does, and checks the event state it reports.
"""

import numpy as np
import pytest

from app.services.motion_detector import MotionDetector, sensitivity_to_pct

W, H = 640, 360
FPS = 10


def scene(seed: int = 0) -> np.ndarray:
    """A textured, static background (so blur/diff behave like a real image)."""
    rng = np.random.default_rng(seed)
    base = rng.integers(60, 120, size=(H // 8, W // 8, 3), dtype=np.uint8)
    return np.kron(base, np.ones((8, 8, 1), dtype=np.uint8))


def with_noise(img: np.ndarray, rng: np.random.Generator, amount: int = 6) -> np.ndarray:
    noise = rng.integers(-amount, amount + 1, size=img.shape)
    return np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def draw_box(img: np.ndarray, x: int, y: int, w: int = 60, h: int = 120, value: int = 230) -> np.ndarray:
    out = img.copy()
    out[y : y + h, x : x + w] = value
    return out


def run(det: MotionDetector, frames, t0: float = 0.0):
    """Feed frames at FPS; return list of (t, sample) for analysed frames."""
    out = []
    for i, f in enumerate(frames):
        t = t0 + i / FPS
        s = det.process(f, t)
        if s is not None:
            out.append((t, s))
    return out


def static_frames(seconds: float, rng, bg=None):
    bg = scene() if bg is None else bg
    return [with_noise(bg, rng) for _ in range(int(seconds * FPS))]


def walking_frames(seconds: float, rng, bg=None, speed_px: int = 12, y: int = 120):
    bg = scene() if bg is None else bg
    frames = []
    for i in range(int(seconds * FPS)):
        x = 20 + (i * speed_px) % (W - 100)
        frames.append(with_noise(draw_box(bg, x, y), rng))
    return frames


def started_at(samples):
    return next((t for t, s in samples if s.started), None)


def stopped_at(samples):
    return next((t for t, s in samples if s.stopped), None)


def test_sensitivity_mapping_is_monotonic_and_bounded():
    assert sensitivity_to_pct(1) == pytest.approx(3.0)
    assert sensitivity_to_pct(100) == pytest.approx(0.15)
    assert sensitivity_to_pct(1) > sensitivity_to_pct(50) > sensitivity_to_pct(100)


def test_static_noisy_scene_never_triggers():
    rng = np.random.default_rng(1)
    det = MotionDetector(sensitivity=80)
    samples = run(det, static_frames(20, rng))
    assert not any(s.started for _, s in samples)


def test_walking_person_triggers_after_warmup():
    rng = np.random.default_rng(2)
    det = MotionDetector(sensitivity=50)
    frames = static_frames(4, rng) + walking_frames(4, rng)
    samples = run(det, frames)
    t = started_at(samples)
    assert t is not None, "walking object should start an event"
    assert 4.0 <= t <= 5.5, f"should trigger soon after the walk begins (got {t:.2f}s)"
    hit = next(s for _, s in samples if s.started)
    assert hit.bbox is not None


def test_motion_during_warmup_does_not_trigger():
    rng = np.random.default_rng(3)
    det = MotionDetector(sensitivity=50)
    samples = run(det, walking_frames(2.5, rng))  # entirely inside the 3s warmup
    assert started_at(samples) is None


def test_single_frame_blip_is_ignored():
    rng = np.random.default_rng(4)
    det = MotionDetector(sensitivity=60)
    bg = scene()
    frames = static_frames(4, rng, bg)
    frames.append(with_noise(draw_box(bg, 200, 100, 80, 80), rng))  # one glitch frame
    frames += static_frames(4, rng, bg)
    assert started_at(run(det, frames)) is None


def test_whole_scene_lighting_change_is_rejected():
    rng = np.random.default_rng(5)
    det = MotionDetector(sensitivity=80)
    bg = scene()
    bright = np.clip(bg.astype(np.int16) + 90, 0, 255).astype(np.uint8)  # lights on / IR switch
    frames = static_frames(4, rng, bg) + static_frames(6, rng, bright)
    samples = run(det, frames)
    assert started_at(samples) is None
    assert any(s.rejected for _, s in samples)


def test_person_who_stops_keeps_event_open_then_it_ends_after_they_leave():
    rng = np.random.default_rng(6)
    det = MotionDetector(sensitivity=50)
    bg = scene()
    walk = walking_frames(3, rng, bg)
    standing = [with_noise(draw_box(bg, 300, 120), rng) for _ in range(int(15 * FPS))]  # no frame diff
    gone = static_frames(8, rng, bg)
    samples = run(det, static_frames(4, rng, bg) + walk + standing + gone)

    t_start = started_at(samples)
    t_stop = stopped_at(samples)
    assert t_start is not None
    leave_t = 4 + 3 + 15
    assert t_stop is not None, "event should end once the person leaves"
    assert t_stop >= leave_t, f"event ended at {t_stop:.1f}s while the person was still standing there"
    assert t_stop <= leave_t + 6, f"event dragged on to {t_stop:.1f}s after they left at {leave_t}s"


def test_event_ends_a_few_seconds_after_motion_stops():
    rng = np.random.default_rng(7)
    det = MotionDetector(sensitivity=50)
    bg = scene()
    samples = run(det, static_frames(4, rng, bg) + walking_frames(4, rng, bg) + static_frames(10, rng, bg))
    t_stop = stopped_at(samples)
    assert t_stop is not None
    assert 8 + 2.5 <= t_stop <= 8 + 6


def test_exclude_zone_ignores_motion_inside_it():
    rng = np.random.default_rng(8)
    zones = [{"kind": "exclude", "points": [[0, 0], [1, 0], [1, 0.6], [0, 0.6]]}]  # top 60% ignored
    det = MotionDetector(sensitivity=60, zones=zones)
    bg = scene()
    samples = run(det, static_frames(4, rng, bg) + walking_frames(5, rng, bg, y=20))  # box at y 20-140
    assert started_at(samples) is None


def test_external_hold_keeps_event_open():
    rng = np.random.default_rng(9)
    det = MotionDetector(sensitivity=50)
    bg = scene()
    samples = run(det, static_frames(4, rng, bg) + walking_frames(3, rng, bg))
    assert det.active
    det.hold(until=7 + 20)
    after = run(det, static_frames(15, rng, bg), t0=7)
    assert not any(s.stopped for _, s in after), "AI hold should keep the event open"
