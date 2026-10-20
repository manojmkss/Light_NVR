"""Cheap appearance attributes - no model needed.

The dominant colour of a person's upper and lower body, from their clearest
frame, so "person in red" / "blue car" can be searched. Deliberately coarse
(11 colour names): camera white balance and IR at night make anything finer
meaningless. Greyscale (IR) frames report no colour rather than a wrong one.
"""

import cv2
import numpy as np

HUES = [  # (name, low, high) on OpenCV's 0-180 hue scale
    ("red", 0, 8), ("orange", 8, 20), ("yellow", 20, 33), ("green", 33, 85),
    ("blue", 85, 128), ("purple", 128, 150), ("pink", 150, 170), ("red", 170, 181),
]


def is_greyscale(img: np.ndarray) -> bool:
    if img.ndim < 3 or img.shape[2] < 3:
        return True
    b, g, r = cv2.split(img.astype(np.int16))
    return float(np.mean(np.abs(b - g)) + np.mean(np.abs(g - r))) < 6.0


def dominant_colour(region: np.ndarray, frame_is_grey: bool = False) -> str | None:
    """`frame_is_grey`: the WHOLE frame is greyscale (IR night mode) - then no
    colour can be claimed. (A white shirt on a colour camera is "white".)"""
    if region.size == 0 or frame_is_grey:
        return None
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0].ravel(), hsv[..., 1].ravel(), hsv[..., 2].ravel()
    counts: dict[str, int] = {}
    dark = v < 50
    grey = (s < 45) & ~dark
    counts["black"] = int(dark.sum())
    white = grey & (v > 190)
    counts["white"] = int(white.sum())
    counts["grey"] = int((grey & ~white).sum())
    chroma = ~dark & ~grey
    for name, lo, hi in HUES:
        counts[name] = counts.get(name, 0) + int((chroma & (h >= lo) & (h < hi)).sum())
    best = max(counts.items(), key=lambda kv: kv[1])
    return best[0] if best[1] > 0.15 * len(h) else None


def person_colours(frame: np.ndarray, box: tuple) -> dict:
    """{"upper": colour, "lower": colour} for a person box (normalised)."""
    H, W = frame.shape[:2]
    x, y, w, h = box
    x1, x2 = int(max(0, (x + w * 0.2) * W)), int(min(W, (x + w * 0.8) * W))

    def band(top: float, bottom: float):
        y1, y2 = int(max(0, (y + h * top) * H)), int(min(H, (y + h * bottom) * H))
        return frame[y1:y2, x1:x2] if y2 > y1 and x2 > x1 else np.zeros((0, 0, 3), np.uint8)

    grey = is_greyscale(frame)
    out = {"upper": dominant_colour(band(0.2, 0.5), grey), "lower": dominant_colour(band(0.55, 0.9), grey)}
    return {k: v for k, v in out.items() if v}


def object_colour(frame: np.ndarray, box: tuple) -> dict:
    H, W = frame.shape[:2]
    x, y, w, h = box
    crop = frame[int(max(0, (y + h * 0.2) * H)):int(min(H, (y + h * 0.8) * H)),
                 int(max(0, (x + w * 0.15) * W)):int(min(W, (x + w * 0.85) * W))]
    c = dominant_colour(crop, is_greyscale(frame))
    return {"colour": c} if c else {}


COLOURS = ["red", "orange", "yellow", "green", "blue", "purple", "pink", "white", "grey", "black"]
