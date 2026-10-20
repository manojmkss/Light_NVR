import numpy as np

from app.services.tamper import HOLD_SECONDS, TamperDetector


def scene(seed=0):
    rng = np.random.default_rng(seed)
    img = np.zeros((360, 640, 3), np.uint8)
    for _ in range(40):  # some structure: rectangles of varied grey
        x, y = rng.integers(0, 600), rng.integers(0, 330)
        img[y:y + rng.integers(10, 80), x:x + rng.integers(10, 120)] = rng.integers(40, 220)
    return img


def feed(det, frame, start, seconds):
    out = []
    t = start
    while t < start + seconds:
        r = det.check(frame, t)
        if r:
            out.append((t, r))
        t += 1.0
    return out, t


def test_covered_lens_reported_once_after_hold_then_cleared():
    det = TamperDetector()
    normal = scene()
    _, t = feed(det, normal, 0, 60)
    black = np.zeros_like(normal)
    events, t = feed(det, black, t, HOLD_SECONDS + 20)
    assert [e[1] for e in events] == [("started", "dark")]
    events, t = feed(det, normal, t, 20)
    assert [e[1] for e in events] == [("cleared", "dark")]


def test_short_blackout_is_ignored():
    det = TamperDetector()
    normal = scene()
    _, t = feed(det, normal, 0, 60)
    events, t = feed(det, np.zeros_like(normal), t, 15)
    assert events == []


def test_turned_camera_reported_as_moved():
    det = TamperDetector()
    _, t = feed(det, scene(0), 0, 60)
    events, _ = feed(det, scene(7), t, HOLD_SECONDS + 20)  # a different view
    assert [e[1][1] for e in events] == ["moved"]


def test_gradual_nightfall_is_not_tampering():
    det = TamperDetector()
    base = scene().astype(np.float32)
    t = 0.0
    events = []
    for i in range(900):  # 75 minutes of slow darkening, checked every 5s
        frame = np.clip(base * max(0.0, 1.0 - i / 700.0), 0, 255).astype(np.uint8)
        r = det.check(frame, t)
        if r:
            events.append(r)
        t += 5.0
    assert events == []


def test_blur_reported():
    import cv2

    det = TamperDetector()
    normal = scene()
    _, t = feed(det, normal, 0, 60)
    blurred = cv2.GaussianBlur(normal, (61, 61), 30)
    events, _ = feed(det, blurred, t, HOLD_SECONDS + 20)
    assert events and events[0][1][1] in ("blur", "moved")
