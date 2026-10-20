"""Camera tamper detection: covered, blacked out, blurred, or turned away.

Checked on one frame every few seconds - cheap enough to run on every camera
all the time. A condition has to persist (HOLD_SECONDS) before it is reported,
so a car's headlights or a passing truck in front of the lens don't count;
and it is reported once until the picture is normal again.

Signals, against a slowly-updated reference of what the camera normally sees:
- dark / bright: almost no light (covered, painted, blinded) or saturated;
- blur: sharpness far below normal (sprayed, smeared, refocused);
- moved: the scene's structure no longer matches the reference (camera
  turned or knocked) - compared with a downsampled edge map.
"""

from dataclasses import dataclass

import cv2
import numpy as np

CHECK_EVERY = 5.0
HOLD_SECONDS = 30.0
WARMUP_SAMPLES = 6
REF_RATE = 0.02  # how fast the reference follows slow scene change (day/night)


@dataclass
class TamperState:
    kind: str | None = None  # dark | bright | blur | moved
    since: float = 0.0
    reported: bool = False


class TamperDetector:
    def __init__(self) -> None:
        self._last_check = -1e9
        self._ref_edges: np.ndarray | None = None
        self._ref_sharp: float | None = None
        self._ref_mean: float | None = None
        self._samples = 0
        self.state = TamperState()

    @staticmethod
    def _measure(frame: np.ndarray) -> tuple[float, float, np.ndarray]:
        small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        mean = float(gray.mean())
        sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        edges = cv2.Canny(gray, 60, 150).astype(np.float32) / 255.0
        edges = cv2.GaussianBlur(edges, (7, 7), 0)
        return mean, sharp, edges

    def _classify(self, mean: float, sharp: float, edges: np.ndarray) -> str | None:
        # Only a *sudden* black/white picture: night falling (or a camera with
        # no IR) darkens slowly and the reference follows it.
        if mean < 8 and (self._ref_mean or 0) > 40:
            return "dark"
        if mean > 247 and (self._ref_mean or 255) < 200:
            return "bright"
        if mean < 15:
            return None  # too dark to judge sharpness or structure
        if self._ref_sharp is not None and self._ref_sharp > 20 and sharp < 0.15 * self._ref_sharp:
            return "blur"
        if self._ref_edges is not None:
            a, b = self._ref_edges.ravel(), edges.ravel()
            if a.std() > 0.01 and b.std() > 0.01:
                corr = float(np.corrcoef(a, b)[0, 1])
                if corr < 0.25:
                    return "moved"
        return None

    def check(self, frame: np.ndarray, now: float) -> tuple[str, str] | None:
        """Feed frames freely; returns ("started", kind) once a condition has
        lasted HOLD_SECONDS, ("cleared", kind) when the picture is back to
        normal, else None."""
        if now - self._last_check < CHECK_EVERY:
            return None
        self._last_check = now
        mean, sharp, edges = self._measure(frame)
        self._samples += 1
        if self._samples <= WARMUP_SAMPLES:
            self._learn(mean, sharp, edges, fast=True)
            return None
        kind = self._classify(mean, sharp, edges)
        st = self.state
        if kind is None:
            self._learn(mean, sharp, edges)
            if st.kind is not None:
                was, reported = st.kind, st.reported
                self.state = TamperState()
                if reported:
                    return ("cleared", was)
            return None
        if st.kind != kind:
            self.state = TamperState(kind, now, False)
            return None
        if not st.reported and now - st.since >= HOLD_SECONDS:
            st.reported = True
            return ("started", kind)
        return None

    def _learn(self, mean: float, sharp: float, edges: np.ndarray, fast: bool = False) -> None:
        rate = 0.5 if fast else REF_RATE
        self._ref_mean = mean if self._ref_mean is None else (1 - rate) * self._ref_mean + rate * mean
        self._ref_sharp = sharp if self._ref_sharp is None else (1 - rate) * self._ref_sharp + rate * sharp
        self._ref_edges = edges if self._ref_edges is None else (1 - rate) * self._ref_edges + rate * edges

    def relearn(self) -> None:
        """The user moved the camera on purpose: accept the new view."""
        self._ref_edges = None
        self._ref_sharp = None
        self._ref_mean = None
        self._samples = 0
        self.state = TamperState()


MESSAGES = {
    "dark": "Camera '{name}' picture is black - covered, painted or unplugged lens?",
    "bright": "Camera '{name}' is blinded - a light shone into it?",
    "blur": "Camera '{name}' picture went blurry - smeared, sprayed or knocked out of focus?",
    "moved": "Camera '{name}' no longer points where it did - turned or knocked?",
}
