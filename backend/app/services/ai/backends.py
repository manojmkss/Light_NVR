import asyncio
import logging
import os
import time
from abc import ABC, abstractmethod

import cv2
import numpy as np

from app.services.ai.types import MODEL_DIR, DetectedObject
from app.vision import yolo
from app.vision.engine import make_session

logger = logging.getLogger(__name__)



class InferenceBackend(ABC):
    """Where inference physically happens.

    The whole AI layer talks to this and nothing else, which is what makes
    "run the models on a different machine" a config change rather than a
    rewrite: LocalOnnxBackend burns the NVR's own CPU, RemoteHttpBackend ships
    the frame to a box with a GPU. Same call, same return type.
    """

    @abstractmethod
    async def detect(self, jpeg: bytes, min_confidence: float) -> list[DetectedObject]:
        ...

    async def detect_frame(self, frame: np.ndarray, min_confidence: float) -> list[DetectedObject]:
        """Detect on a decoded BGR frame. Default: encode and use detect();
        the local backend overrides this to skip the JPEG round-trip."""
        ok, buf = await asyncio.to_thread(cv2.imencode, ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            return []
        return await self.detect(buf.tobytes(), min_confidence)

    # How many requests this backend can usefully run at once (scheduler slots).
    slots: int = 1

    @abstractmethod
    async def health(self) -> tuple[bool, str]:
        """(ok, human-readable detail) - drives the Test button in Settings."""

    async def close(self) -> None:
        return None


def _to_obj(b) -> DetectedObject:
    return DetectedObject(label=b.label, confidence=b.confidence, x=b.x, y=b.y, w=b.w, h=b.h)


def decode_yolo_output(
    output: np.ndarray, scale: float, dx: int, dy: int, orig_w: int, orig_h: int, min_confidence: float
) -> list[DetectedObject]:
    """Raw YOLO tensor -> normalised objects. The decoding itself lives in
    app.vision.yolo, shared with the ai-worker so the two can never drift."""
    return [_to_obj(b) for b in yolo.decode(output, scale, dx, dy, orig_w, orig_h, min_confidence)]


class LocalOnnxBackend(InferenceBackend):
    """ONNX Runtime in this process, on the NVR's own CPU.

    Only ever called on a motion-triggered frame, never per-frame - that gate
    is what makes YOLO viable on a mini-PC or Pi at all. Rough per-frame cost
    for yolov8n at 640x640: ~80-150ms on a 4-core x86 mini-PC, ~0.4-1s on a
    Pi 4/5. If that's too slow, the remote backend moves the work to a GPU box.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.model_path = os.path.join(MODEL_DIR, f"{model}.onnx")
        self._session = None
        self._input_name: str | None = None
        self._lock = asyncio.Lock()

    def _load(self) -> None:
        if self._session is not None:
            return
        if not os.path.exists(self.model_path):
            raise FileNotFoundError(
                f"Model '{self.model}' not found at {self.model_path}. "
                f"Run scripts/fetch-ai-models.sh to download it, or switch the AI backend to 'remote'."
            )
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - depends on image build
            raise RuntimeError("onnxruntime is not installed in this image") from exc

        # This runs alongside FFmpeg recorders and OpenCV decoders; letting ORT
        # grab every core would starve them and stutter live recording, which
        # matters far more than shaving milliseconds off a detection.
        self._session, self.provider = make_session(
            self.model_path, "cpu", threads=max(1, (os.cpu_count() or 2) // 2)
        )
        del ort  # imported above only to fail early with a clear message
        self._input_name = self._session.get_inputs()[0].name
        logger.info("AI: loaded local ONNX model %s", self.model_path)

    def _infer_sync(self, jpeg: bytes, min_confidence: float) -> list[DetectedObject]:
        buf = np.frombuffer(jpeg, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is None:
            return []
        return self._infer_array(img, min_confidence)

    def _infer_array(self, img: np.ndarray, min_confidence: float) -> list[DetectedObject]:
        self._load()
        orig_h, orig_w = img.shape[:2]

        canvas, scale, dx, dy = yolo.letterbox(img)
        outputs = self._session.run(None, {self._input_name: yolo.to_blob(canvas)})
        return decode_yolo_output(outputs[0], scale, dx, dy, orig_w, orig_h, min_confidence)

    async def detect(self, jpeg: bytes, min_confidence: float) -> list[DetectedObject]:
        # ORT inference is blocking C++; on the event loop it would stall every
        # live-view stream and API request for its whole duration. Concurrency
        # across cameras is the scheduler's job (one slot here); the lock is a
        # backstop for direct callers such as the Test button.
        async with self._lock:
            return await asyncio.to_thread(self._infer_sync, jpeg, min_confidence)

    async def detect_frame(self, frame: np.ndarray, min_confidence: float) -> list[DetectedObject]:
        async with self._lock:
            return await asyncio.to_thread(self._infer_array, frame, min_confidence)

    async def health(self) -> tuple[bool, str]:
        if not os.path.exists(self.model_path):
            return False, f"Model file missing: {self.model_path}. Run scripts/fetch-ai-models.sh."
        try:
            await asyncio.to_thread(self._load)
        except Exception as exc:
            return False, f"Could not load model: {exc}"
        return True, f"Local ONNX model '{self.model}' loaded (CPU)"


class RemoteHttpBackend(InferenceBackend):
    """Offloads inference to an ai-worker on another machine (typically one
    with a GPU). The NVR box then only decodes and JPEG-encodes, which it is
    already doing anyway for live view - so adding AI costs it ~nothing.
    """

    def __init__(self, base_url: str, api_key: str = "", model: str = "yolov8n") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.slots = 2  # a GPU worker overlaps network + inference fine
        self._client: "httpx.AsyncClient | None" = None
        self._client_lock = asyncio.Lock()

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key} if self.api_key else {}

    async def _get_client(self):
        """One reused client, not one per detection. Motion events arrive in
        bursts, and a fresh TCP+TLS handshake per frame is pure latency on the
        path between seeing motion and alerting on it."""
        import httpx

        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                # Short timeout on purpose: a slow or half-dead worker must
                # degrade to "no detections" rather than back the motion
                # pipeline up behind it.
                self._client = httpx.AsyncClient(timeout=15.0, headers=self._headers())
            return self._client

    async def detect(self, jpeg: bytes, min_confidence: float) -> list[DetectedObject]:
        client = await self._get_client()
        resp = await client.post(
            f"{self.base_url}/detect",
            files={"image": ("frame.jpg", jpeg, "image/jpeg")},
            data={"min_confidence": str(min_confidence), "model": self.model},
        )
        resp.raise_for_status()
        payload = resp.json()

        return [
            DetectedObject(
                label=d["label"],
                confidence=float(d["confidence"]),
                x=float(d["x"]),
                y=float(d["y"]),
                w=float(d["w"]),
                h=float(d["h"]),
            )
            for d in payload.get("detections", [])
        ]

    async def health(self) -> tuple[bool, str]:
        if not self.base_url:
            return False, "No remote AI worker URL configured"
        try:
            started = time.monotonic()
            client = await self._get_client()
            resp = await client.get(f"{self.base_url}/health")
            resp.raise_for_status()
            info = resp.json()
            took = int((time.monotonic() - started) * 1000)
            device = info.get("device", "unknown")
            return True, f"Remote worker reachable in {took}ms (device: {device})"
        except Exception as exc:
            if "ai-worker" in self.base_url and ("name resolution" in str(exc).lower() or "Name or service" in str(exc)):
                # The compose name only exists while the container runs.
                return False, ("This NVR's AI service isn't running (no 'ai-worker' container). Run the update "
                               "once - ./scripts/update-linux.sh - which sets it up for this machine's hardware; "
                               "until then choose 'This machine (CPU)' under Where it runs.")
            return False, f"Could not reach remote AI worker at {self.base_url}: {exc}"

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


def build_backend(settings) -> InferenceBackend:
    """Pick the backend from AISettings. Kept dead simple on purpose - this is
    the only place the local/remote choice is made."""
    if settings.backend == "remote":
        return RemoteHttpBackend(settings.remote_url, settings.remote_api_key, settings.detection_model)
    return LocalOnnxBackend(settings.detection_model)
