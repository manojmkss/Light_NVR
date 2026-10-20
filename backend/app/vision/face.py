"""Faces: OpenCV's YuNet detector + SFace recogniser (both Apache-2.0, both
built into OpenCV 4.x - no extra Python dependency).

Faces are looked for only inside person boxes (their upper part), at full
resolution, and every face gets a quality score so that blurry, tiny or
side-on faces never get to decide who someone is.
"""

import threading
from dataclasses import dataclass, field

import cv2
import numpy as np

MIN_FACE_PX = 40
DETECT_SCORE = 0.85


@dataclass
class Face:
    x: float  # normalised to the full frame
    y: float
    w: float
    h: float
    score: float
    quality: float  # 0..1: detection score x sharpness x frontalness x size
    embedding: np.ndarray = field(repr=False, default=None)
    crop: np.ndarray | None = field(repr=False, default=None)  # aligned 112x112 face

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h, "score": round(self.score, 3),
                "quality": round(self.quality, 3), "embedding": [round(float(v), 6) for v in self.embedding]}


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=np.float32).ravel()
    b = np.asarray(b, dtype=np.float32).ravel()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return float(np.dot(a, b) / (na * nb)) if na and nb else 0.0


def _sharpness(gray: np.ndarray) -> float:
    """Variance of the Laplacian, squashed to 0..1 (~1 for a crisp face)."""
    v = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    return min(1.0, v / 150.0)


def _frontalness(lm: np.ndarray) -> float:
    """1 for a face looking at the camera, towards 0 when turned: the nose
    sits midway between the eyes for a frontal face."""
    (rx, ry), (lx, ly), (nx, ny) = lm[0], lm[1], lm[2]
    eye_dist = abs(lx - rx)
    if eye_dist < 1:
        return 0.0
    mid = (lx + rx) / 2
    return float(max(0.0, 1.0 - abs(nx - mid) / (eye_dist / 2)))


class FaceEngine:
    def __init__(self, yunet_path: str, sface_path: str, score_threshold: float = DETECT_SCORE) -> None:
        self._det = cv2.FaceDetectorYN.create(yunet_path, "", (320, 320), score_threshold, 0.3, 5000)
        self._rec = cv2.FaceRecognizerSF.create(sface_path, "")
        self._lock = threading.Lock()  # OpenCV dnn objects aren't thread-safe

    def _detect_in(self, img: np.ndarray) -> np.ndarray:
        h, w = img.shape[:2]
        self._det.setInputSize((w, h))
        _, faces = self._det.detect(img)
        return faces if faces is not None else np.zeros((0, 15), dtype=np.float32)

    def find(self, img: np.ndarray, person_boxes: list | None = None, min_face: int = MIN_FACE_PX,
             keep_crops: bool = False) -> list[Face]:
        """Faces in a BGR frame. With person boxes (normalised x, y, w, h),
        only their upper half is searched - faster, and no faces on posters."""
        H, W = img.shape[:2]
        regions = []
        if person_boxes:
            for bx, by, bw, bh in person_boxes:
                # Upper half of the person, widened a little: the box and the
                # full-res frame can be a fraction of a second apart.
                x1 = int(max(0, (bx - bw * 0.15) * W))
                x2 = int(min(W, (bx + bw * 1.15) * W))
                y1 = int(max(0, (by - bh * 0.1) * H))
                y2 = int(min(H, (by + bh * 0.55) * H))
                if x2 - x1 >= min_face and y2 - y1 >= min_face:
                    regions.append((x1, y1, img[y1:y2, x1:x2]))
        else:
            regions.append((0, 0, img))

        out: list[Face] = []
        with self._lock:
            for ox, oy, crop in regions:
                for f in self._detect_in(crop):
                    fx, fy, fw, fh = f[:4]
                    if min(fw, fh) < min_face:
                        continue
                    aligned = self._rec.alignCrop(crop, f)
                    feat = self._rec.feature(aligned).ravel().astype(np.float32)
                    n = np.linalg.norm(feat)
                    feat = feat / n if n else feat
                    lm = f[4:14].reshape(5, 2)
                    gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
                    size = min(1.0, min(fw, fh) / 112.0)
                    quality = float(f[14]) * _sharpness(gray) * _frontalness(lm) * (0.5 + 0.5 * size)
                    out.append(Face(
                        x=(ox + fx) / W, y=(oy + fy) / H, w=fw / W, h=fh / H, score=float(f[14]),
                        quality=quality, embedding=feat, crop=aligned if keep_crops else None,
                    ))
        return _dedupe(out)


def _iou(a: Face, b: Face) -> float:
    ix = max(0.0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    iy = max(0.0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    inter = ix * iy
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


def _dedupe(faces: list[Face]) -> list[Face]:
    """Overlapping person boxes find the same face more than once."""
    kept: list[Face] = []
    for f in sorted(faces, key=lambda x: -x.score):
        if all(_iou(f, k) < 0.5 for k in kept):
            kept.append(f)
    return kept
