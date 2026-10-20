"""LightNVR AI worker ("LightNVR Vision") - the local AI service.

Runs object detection and face recognition over HTTP, either as a sidecar
next to the NVR (isolating AI from recording: a crash or out-of-memory here
can never stop a recording) or on another PC with a GPU.

ONNX Runtime only - no PyTorch - with one image per accelerator family:
  cpu (any x86/ARM), openvino (Intel iGPU/Arc), cuda (NVIDIA).
The provider actually in use is reported, never assumed.

API (all coordinates normalised 0..1 against the frame):
  GET  /health                  - liveness + device
  POST /detect                  - v1, kept for older NVRs
  GET  /v2/capabilities         - device, providers, slots, models, latencies
  POST /v2/detect               - image -> objects (down to min_confidence)
  POST /v2/faces                - image (+ person boxes) -> faces with embeddings
  POST /v2/benchmark            - time the loaded models
  GET  /v2/models               - which optional models are present
  POST /v2/models/{name}        - download one (checksum-verified)
"""

import asyncio
import json
import logging
import os
import time

import cv2
import numpy as np
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.vision import models as model_registry
from app.vision.engine import available_providers, device_label

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai-worker")

API_KEY = os.environ.get("AI_WORKER_API_KEY", "")
DEFAULT_MODEL = os.environ.get("AI_WORKER_MODEL", "yolov8n")
MODEL_DIR = os.environ.get("AI_WORKER_MODEL_DIR", "/models")
PROVIDER = os.environ.get("AI_WORKER_PROVIDER", "auto")  # auto | cuda | openvino | rocm | cpu
SLOTS = int(os.environ.get("AI_WORKER_SLOTS", "0") or 0)  # 0 = decide from the device
VARIANT = os.environ.get("AI_WORKER_VARIANT", "cpu")

app = FastAPI(title="LightNVR Vision", version="2.0.0")

_detectors: dict[str, object] = {}
_faces = None
_latency: dict[str, float] = {}
_lock = asyncio.Lock()


def require_key(x_api_key: str | None = Header(default=None)) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing X-API-Key")


def _detector(name: str):
    if name not in _detectors:
        from app.vision.yolo import YoloDetector

        path = os.path.join(MODEL_DIR, f"{name}.onnx")
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"Model '{name}' not found at {path} - run scripts/fetch-ai-models.sh {name} on this machine"
            )
        threads = None if PROVIDER != "cpu" else max(1, (os.cpu_count() or 2) // 2)
        _detectors[name] = YoloDetector(path, PROVIDER, threads)
        logger.info("Loaded %s on %s", name, _detectors[name].provider)
    return _detectors[name]


def _face_engine():
    global _faces
    if _faces is None:
        from app.vision.face import FaceEngine

        if not (model_registry.present("yunet", MODEL_DIR) and model_registry.present("sface", MODEL_DIR)):
            raise FileNotFoundError("Face models not downloaded - enable faces in LightNVR (Settings -> AI)")
        _faces = FaceEngine(model_registry.path_for("yunet", MODEL_DIR), model_registry.path_for("sface", MODEL_DIR))
    return _faces


def _provider() -> str:
    for d in _detectors.values():
        return d.provider
    return "CPUExecutionProvider" if PROVIDER in ("auto", "cpu") else PROVIDER


def _slots() -> int:
    if SLOTS:
        return SLOTS
    return 4 if device_label(_provider()) in ("cuda", "tensorrt", "rocm") else 2 if device_label(_provider()) == "openvino" else 1


async def _decode(image: UploadFile) -> np.ndarray:
    raw = await image.read()
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty image")
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a decodable image")
    return img


@app.on_event("startup")
async def startup() -> None:
    os.makedirs(MODEL_DIR, exist_ok=True)
    logger.info("LightNVR Vision (%s image) starting; ONNX providers available: %s", VARIANT, available_providers())
    try:
        await run_in_threadpool(_detector, DEFAULT_MODEL)
    except Exception as exc:
        logger.warning("Default model not loaded yet: %s", exc)
    if device_label(_provider()) == "cpu" and VARIANT != "cpu":
        logger.warning("The %s image is running on CPU - the accelerator isn't reachable from the container", VARIANT)


@app.get("/health")
async def health():
    return {"status": "ok", "device": device_label(_provider()), "provider": _provider(),
            "default_model": DEFAULT_MODEL, "loaded_models": sorted(_detectors)}


def _run_detect(img: np.ndarray, name: str, min_confidence: float) -> tuple[list, float]:
    started = time.perf_counter()
    boxes = _detector(name).detect(img, min_confidence)
    took = time.perf_counter() - started
    _latency[name] = took if name not in _latency else 0.8 * _latency[name] + 0.2 * took
    return [b.__dict__ for b in boxes], took


@app.post("/detect", dependencies=[Depends(require_key)])
@app.post("/v2/detect", dependencies=[Depends(require_key)])
async def detect(image: UploadFile = File(...), min_confidence: float = Form(default=0.5), model: str = Form(default="")):
    img = await _decode(image)
    name = model or DEFAULT_MODEL
    try:
        dets, took = await run_in_threadpool(_run_detect, img, name, min_confidence)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"detections": dets, "took_ms": int(took * 1000), "device": device_label(_provider()), "model": name}


@app.post("/v2/faces", dependencies=[Depends(require_key)])
async def faces(image: UploadFile = File(...), boxes: str = Form(default=""), min_face: int = Form(default=40)):
    """`boxes`: JSON [[x, y, w, h], ...] person boxes (normalised). Empty =
    search the whole image (used for enrolment photos)."""
    img = await _decode(image)
    try:
        person_boxes = json.loads(boxes) if boxes else None
        engine = await run_in_threadpool(_face_engine)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Bad boxes: {exc}") from exc
    found = await run_in_threadpool(engine.find, img, person_boxes, min_face)
    return {"faces": [f.to_dict() for f in found]}


@app.get("/v2/capabilities")
async def capabilities():
    return {
        "variant": VARIANT,
        "device": device_label(_provider()),
        "provider": _provider(),
        "available_providers": available_providers(),
        "slots": _slots(),
        "models": sorted(_detectors),
        "optional_models": model_registry.status(MODEL_DIR),
        "latency_ms": {k: round(v * 1000, 1) for k, v in _latency.items()},
        "cpu_count": os.cpu_count(),
    }


@app.post("/v2/benchmark", dependencies=[Depends(require_key)])
async def benchmark(runs: int = Form(default=12), model: str = Form(default="")):
    name = model or DEFAULT_MODEL
    frame = np.random.default_rng(0).integers(0, 255, (360, 640, 3), dtype=np.uint8)
    try:
        await run_in_threadpool(_run_detect, frame, name, 0.5)
        times = []
        for _ in range(max(1, min(runs, 50))):
            _, took = await run_in_threadpool(_run_detect, frame, name, 0.5)
            times.append(took)
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}
    times.sort()
    return {"ok": True, "runs": len(times), "p50_ms": round(times[len(times) // 2] * 1000, 1),
            "p95_ms": round(times[min(len(times) - 1, int(len(times) * 0.95))] * 1000, 1),
            "device": device_label(_provider()), "provider": _provider()}


@app.get("/v2/models")
async def list_models():
    return {"models": model_registry.status(MODEL_DIR)}


@app.post("/v2/models/{name}", dependencies=[Depends(require_key)])
async def fetch_model(name: str):
    if name not in model_registry.MANIFEST:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown model")
    async with _lock:
        try:
            path = await run_in_threadpool(model_registry.download, name, MODEL_DIR)
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Download failed: {exc}") from exc
    global _faces
    _faces = None
    return {"downloaded": name, "path": path}
