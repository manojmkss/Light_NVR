import asyncio
import logging
import os
import time
from abc import ABC, abstractmethod

import cv2
import numpy as np

from app.services.ai.types import COCO_CLASSES, MODEL_DIR, DetectedObject

logger = logging.getLogger(__name__)

# YOLOv8/YOLO11 are trained at 640x640; feeding anything else silently costs
# accuracy, so the letterbox below always lands on this regardless of the
# camera's substream size.
_INPUT_SIZE = 640
_NMS_IOU = 0.45


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

    @abstractmethod
    async def health(self) -> tuple[bool, str]:
        """(ok, human-readable detail) - drives the Test button in Settings."""

    async def close(self) -> None:
        return None


def _letterbox(img: np.ndarray) -> tuple[np.ndarray, float, int, int]:
    """Resize preserving aspect ratio and pad to a square, the way YOLO expects.
    A plain resize would stretch the image and shift every box; returns the
    scale/pad so boxes can be mapped back to the original frame.
    """
    h, w = img.shape[:2]
    scale = min(_INPUT_SIZE / w, _INPUT_SIZE / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((_INPUT_SIZE, _INPUT_SIZE, 3), 114, dtype=np.uint8)
    dx, dy = (_INPUT_SIZE - nw) // 2, (_INPUT_SIZE - nh) // 2
    canvas[dy:dy + nh, dx:dx + nw] = resized
    return canvas, scale, dx, dy


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list[int]:
    """Greedy non-max suppression. YOLO emits many overlapping candidates for
    one object; without this a single person becomes six detections and six
    alerts."""
    if len(boxes) == 0:
        return []
    x1, y1 = boxes[:, 0], boxes[:, 1]
    x2, y2 = boxes[:, 0] + boxes[:, 2], boxes[:, 1] + boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]

    keep: list[int] = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= iou_threshold]
    return keep


def decode_yolo_output(
    output: np.ndarray, scale: float, dx: int, dy: int, orig_w: int, orig_h: int, min_confidence: float
) -> list[DetectedObject]:
    """Turn a raw YOLOv8/11 tensor into normalised, de-duplicated objects.

    Output is (1, 84, 8400): 8400 candidate boxes, each with 4 box values
    (cx, cy, w, h in letterboxed pixels) followed by 80 per-class scores.
    Shared by both backends so a local and a remote run can't drift apart.
    """
    preds = np.squeeze(output)            # (84, 8400)
    if preds.ndim != 2:
        return []
    if preds.shape[0] < preds.shape[1]:   # (84, 8400) -> (8400, 84)
        preds = preds.transpose()

    class_scores = preds[:, 4:]
    class_ids = np.argmax(class_scores, axis=1)
    confidences = class_scores[np.arange(len(class_ids)), class_ids]

    mask = confidences >= min_confidence
    if not np.any(mask):
        return []
    preds, class_ids, confidences = preds[mask], class_ids[mask], confidences[mask]

    # cx,cy,w,h (letterboxed) -> x,y,w,h in original-frame pixels
    cx, cy, bw, bh = preds[:, 0], preds[:, 1], preds[:, 2], preds[:, 3]
    x = (cx - bw / 2 - dx) / scale
    y = (cy - bh / 2 - dy) / scale
    w = bw / scale
    h = bh / scale
    boxes = np.stack([x, y, w, h], axis=1)

    results: list[DetectedObject] = []
    for i in _nms(boxes, confidences, _NMS_IOU):
        bx, by, bw_, bh_ = boxes[i]
        cid = int(class_ids[i])
        label = COCO_CLASSES[cid] if 0 <= cid < len(COCO_CLASSES) else str(cid)
        results.append(
            DetectedObject(
                label=label,
                confidence=float(confidences[i]),
                # Clamped: boxes on a frame edge can extend past it, and a
                # negative/over-1 box breaks the UI overlay that draws them.
                x=max(0.0, min(1.0, float(bx) / orig_w)),
                y=max(0.0, min(1.0, float(by) / orig_h)),
                w=max(0.0, min(1.0, float(bw_) / orig_w)),
                h=max(0.0, min(1.0, float(bh_) / orig_h)),
            )
        )
    return results


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

        opts = ort.SessionOptions()
        # This runs alongside FFmpeg recorders and OpenCV decoders; letting ORT
        # grab every core would starve them and stutter live recording, which
        # matters far more than shaving milliseconds off a detection.
        opts.intra_op_num_threads = max(1, (os.cpu_count() or 2) // 2)
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(self.model_path, opts, providers=["CPUExecutionProvider"])
        self._input_name = self._session.get_inputs()[0].name
        logger.info("AI: loaded local ONNX model %s", self.model_path)

    def _infer_sync(self, jpeg: bytes, min_confidence: float) -> list[DetectedObject]:
        self._load()
        buf = np.frombuffer(jpeg, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is None:
            return []
        orig_h, orig_w = img.shape[:2]

        canvas, scale, dx, dy = _letterbox(img)
        blob = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        blob = np.transpose(blob, (2, 0, 1))[np.newaxis, ...]  # NCHW

        outputs = self._session.run(None, {self._input_name: blob})
        return decode_yolo_output(outputs[0], scale, dx, dy, orig_w, orig_h, min_confidence)

    async def detect(self, jpeg: bytes, min_confidence: float) -> list[DetectedObject]:
        # ORT inference is blocking C++; on the event loop it would stall every
        # live-view stream and API request for its whole duration. The lock
        # keeps concurrent motion on several cameras from running N inferences
        # at once and pinning every core.
        async with self._lock:
            return await asyncio.to_thread(self._infer_sync, jpeg, min_confidence)

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
