"""LightNVR AI worker - object detection over HTTP, meant to run on a machine
with a GPU (not the NVR box).

Why this exists: the NVR itself runs YOLO on CPU via ONNX Runtime, which is
fine for a few cameras because detection only ever fires on a motion event.
Once you have more cameras, a Pi, or want a bigger model, you want that work
somewhere else. Point Settings -> AI -> "Another PC with a GPU" at this and
the NVR stops doing inference entirely.

Deliberately NOT the same implementation as the NVR's local backend: this uses
ultralytics (torch), which does decode/NMS itself and gets CUDA for free. The
NVR keeps the lean ONNX path so a stock install never pulls in torch. Both
speak the same tiny HTTP contract, which is the only thing that has to match:

    GET  /health  -> {"status","device","models"}
    POST /detect  -> {"detections":[{"label","confidence","x","y","w","h"}]}

Coordinates are normalised 0..1 against the frame, matching the NVR's
Detection model (pixels would break the moment a substream resolution changed).
"""

import io
import logging
import os
import time

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, status
from PIL import Image

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai-worker")

# Shared secret. Optional, but this endpoint will happily decode any image
# posted to it, so on anything but a trusted LAN set it.
API_KEY = os.environ.get("AI_WORKER_API_KEY", "")
DEFAULT_MODEL = os.environ.get("AI_WORKER_MODEL", "yolov8n")
# Where ultralytics caches weights. Mount a volume here or every restart
# re-downloads them.
MODEL_DIR = os.environ.get("AI_WORKER_MODEL_DIR", "/models")

app = FastAPI(title="LightNVR AI Worker", version="1.0.0")

_models: dict[str, object] = {}
_device = "cpu"


def _resolve_device() -> str:
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        # Apple Silicon, for anyone running this on a Mac.
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def _get_model(name: str):
    """Loaded once per model name and cached: a YOLO load is ~1s, which would
    otherwise be paid on every single detection request."""
    if name not in _models:
        from ultralytics import YOLO

        path = os.path.join(MODEL_DIR, f"{name}.pt")
        # Falls back to the bare name so ultralytics fetches it itself on first
        # use; after that it's cached in MODEL_DIR.
        source = path if os.path.exists(path) else f"{name}.pt"
        logger.info("Loading model %s (device=%s)", source, _device)
        model = YOLO(source)
        model.to(_device)
        _models[name] = model
    return _models[name]


def require_key(x_api_key: str | None = Header(default=None)) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing X-API-Key")


@app.on_event("startup")
async def startup() -> None:
    global _device
    os.makedirs(MODEL_DIR, exist_ok=True)
    # ultralytics writes weights relative to cwd unless told otherwise.
    os.environ.setdefault("YOLO_CONFIG_DIR", MODEL_DIR)
    _device = _resolve_device()
    logger.info("AI worker starting on device=%s, default model=%s", _device, DEFAULT_MODEL)
    if _device == "cpu":
        logger.warning(
            "No GPU detected - this worker will run on CPU, which is no faster than the NVR doing it "
            "itself. Check your CUDA/driver setup if you expected a GPU."
        )
    try:
        _get_model(DEFAULT_MODEL)  # warm up so the first real detection isn't slow
    except Exception:
        logger.exception("Could not preload default model - it will be retried on first request")


@app.get("/health")
async def health():
    """Drives the "Check it works" button in the NVR's Settings -> AI."""
    return {
        "status": "ok",
        "device": _device,
        "default_model": DEFAULT_MODEL,
        "loaded_models": sorted(_models.keys()),
    }


@app.post("/detect", dependencies=[Depends(require_key)])
async def detect(
    image: UploadFile = File(...),
    min_confidence: float = Form(default=0.5),
    model: str = Form(default=""),
):
    raw = await image.read()
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty image")

    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Not a decodable image: {exc}")

    name = model or DEFAULT_MODEL
    try:
        yolo = _get_model(name)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Could not load model '{name}': {exc}"
        ) from exc

    width, height = img.size
    started = time.monotonic()
    # verbose=False or ultralytics prints a line per inference; at one call per
    # motion event across several cameras that's a lot of noise for no value.
    results = yolo.predict(img, conf=min_confidence, verbose=False, device=_device)

    detections = []
    for result in results:
        names = result.names
        for box in result.boxes:
            x1, y1, x2, y2 = (float(v) for v in box.xyxy[0].tolist())
            detections.append(
                {
                    "label": names[int(box.cls.item())],
                    "confidence": float(box.conf.item()),
                    # Normalised + clamped: a box on the frame edge can extend
                    # past it, and the NVR draws these directly as an overlay.
                    "x": max(0.0, min(1.0, x1 / width)),
                    "y": max(0.0, min(1.0, y1 / height)),
                    "w": max(0.0, min(1.0, (x2 - x1) / width)),
                    "h": max(0.0, min(1.0, (y2 - y1) / height)),
                }
            )

    took_ms = int((time.monotonic() - started) * 1000)
    logger.info("detect: %d object(s) in %dms (model=%s, device=%s)", len(detections), took_ms, name, _device)
    return {"detections": detections, "took_ms": took_ms, "device": _device, "model": name}
