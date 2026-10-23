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
import base64
import hmac
import json
import logging
import os
import threading
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

MAX_IMAGE_BYTES = 20 * 1024 * 1024
# A single inference taking longer than this means the runtime/driver is hung
# (a GPU reset, a deadlock): exit so Docker's restart policy brings a fresh
# worker, instead of every later request queueing behind it forever.
HUNG_INFERENCE_SECONDS = float(os.environ.get("AI_WORKER_HUNG_SECONDS", "120"))
_inflight: dict[int, float] = {}
_inflight_lock = threading.Lock()


def _watchdog() -> None:
    while True:
        time.sleep(10)
        with _inflight_lock:
            oldest = min(_inflight.values(), default=None)
        if oldest is not None and time.monotonic() - oldest > HUNG_INFERENCE_SECONDS:
            logger.critical("Inference stuck for over %ss - restarting the worker", int(HUNG_INFERENCE_SECONDS))
            os._exit(75)


threading.Thread(target=_watchdog, name="watchdog", daemon=True).start()


def _tracked(fn, *args):
    """Run a blocking model call, registered with the hang watchdog."""
    key = threading.get_ident()
    with _inflight_lock:
        _inflight[key] = time.monotonic()
    try:
        return fn(*args)
    finally:
        with _inflight_lock:
            _inflight.pop(key, None)


def require_key(x_api_key: str | None = Header(default=None)) -> None:
    if API_KEY and not hmac.compare_digest((x_api_key or "").encode(), API_KEY.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing X-API-Key")


# ── which device runs the models (Settings -> AI -> AI engine) ───────────────
#
# The install/update scripts give this container the GPU when the machine has
# one; which device is *used* is chosen here, at runtime, from the NVR's web
# page: models are reloaded on the new device (no restart), and a device is
# only kept after it has really run a detection. "auto" picks the fastest
# device that works; a GPU that starts failing hands over to the CPU by itself.

DEVICES = ("cpu", "intel_gpu", "nvidia_gpu")
DEVICE_NAMES = {"cpu": "CPU", "intel_gpu": "Intel GPU", "nvidia_gpu": "NVIDIA GPU"}
ENGINE_FILE = os.environ.get("AI_WORKER_ENGINE_FILE", os.path.join(os.path.dirname(MODEL_DIR.rstrip("/")) or "/data",
                                                                    "ai-engine.json"))
LEGACY_PROVIDER = {"cpu": "cpu", "openvino": "intel_gpu", "cuda": "nvidia_gpu"}
GPU_FAILS_BEFORE_FALLBACK = 3

_engine = {"choice": "auto", "active": "cpu", "note": "", "results": {}}
_gpu_fails = 0
_engine_lock = threading.Lock()


def _has_dri() -> bool:
    try:
        return any(n.startswith("renderD") for n in os.listdir("/dev/dri"))
    except OSError:
        return False


_names: dict[str, str] = {}


def _device_name(device: str) -> str:
    if device not in _names:
        _names[device] = _lookup_name(device)
    return _names[device]


def _lookup_name(device: str) -> str:
    """A readable name for the card, where the runtime can tell us one."""
    if device == "intel_gpu":
        try:
            import openvino as ov

            return ov.Core().get_property("GPU", "FULL_DEVICE_NAME")
        except Exception:
            return "Intel GPU"
    if device == "nvidia_gpu":
        try:
            import subprocess

            out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True,
                                 text=True, timeout=5).stdout.strip().splitlines()
            return out[0] if out else "NVIDIA GPU"
        except Exception:
            return "NVIDIA GPU"
    return f"CPU ({os.cpu_count()} cores)"


def device_options() -> list[dict]:
    """Every device, whether this container can use it, and if not, why - in
    words a user can act on."""
    providers = available_providers()
    if not providers:
        # The runtime itself didn't load - say so, rather than blaming the GPU.
        try:
            import onnxruntime  # noqa: F401

            why = "the AI runtime reports no devices"
        except Exception as exc:
            why = f"the AI runtime couldn't load ({str(exc)[:160]})"
        reason = f"Nothing can run: {why}. Update to the latest LightNVR; if it persists, report it."
        return [{"id": d, "label": DEVICE_NAMES[d], "name": DEVICE_NAMES[d], "available": False,
                 "reason": reason, "result": _engine["results"].get(d)} for d in DEVICES]
    out = [{"id": "cpu", "available": True, "reason": None}]
    if "OpenVINOExecutionProvider" not in providers:
        intel = (f"This AI service runs the {VARIANT} image, which has no Intel GPU engine. If the machine has an "
                 "Intel GPU, run the update - it switches images by itself.")
    elif not _has_dri():
        intel = ("No Intel GPU is connected to the AI service. If this machine has one, run the update (it connects "
                 "it); on a VM, pass the GPU through first.")
    else:
        intel = None
    out.append({"id": "intel_gpu", "available": intel is None, "reason": intel})
    nvidia = None if "CUDAExecutionProvider" in providers else (
        "Needs an NVIDIA GPU with NVIDIA's driver and Container Toolkit on the host; then run the update.")
    out.append({"id": "nvidia_gpu", "available": nvidia is None, "reason": nvidia})
    for d in out:
        d["label"] = DEVICE_NAMES[d["id"]]
        d["name"] = _device_name(d["id"]) if d["available"] else DEVICE_NAMES[d["id"]]
        d["result"] = _engine["results"].get(d["id"])
    return out


def _model_path(name: str) -> str:
    path = os.path.join(MODEL_DIR, f"{name}.onnx")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Model '{name}' not found at {path} - run scripts/fetch-ai-models.sh {name} on this machine"
        )
    return path


def _new_detector(name: str, device: str, strict: bool):
    from app.vision.yolo import YoloDetector

    threads = max(1, (os.cpu_count() or 2) // 2) if device == "cpu" else None
    return YoloDetector(_model_path(name), device, threads, strict=strict)


def _really_on(det, device: str) -> bool:
    """The session runs on the device asked for (not a quiet CPU fallback)."""
    want = {"cpu": "CPUExecutionProvider", "intel_gpu": "OpenVINOExecutionProvider",
            "nvidia_gpu": "CUDAExecutionProvider"}[device]
    return det.provider == want


def measure(device: str, runs: int = 10) -> dict:
    """Load the default model on `device` and time real detections. First
    GPU runs compile the model, so they're warm-up, not counted."""
    frame = np.random.default_rng(0).integers(0, 255, (360, 640, 3), dtype=np.uint8)
    try:
        det = _new_detector(DEFAULT_MODEL, device, strict=device != "cpu")
        if not _really_on(det, device):
            raise RuntimeError({
                "intel_gpu": "Intel's engine couldn't use the graphics device - it isn't an Intel GPU, or its "
                             "driver isn't working in this machine",
                "nvidia_gpu": "CUDA couldn't use the GPU - check the NVIDIA driver on the host",
            }.get(device, f"ran on {det.provider} instead"))
        for _ in range(3):
            det.detect(frame, 0.5)
        times = []
        for _ in range(runs):
            t0 = time.perf_counter()
            det.detect(frame, 0.5)
            times.append(time.perf_counter() - t0)
        times.sort()
        result = {"ok": True, "p50_ms": round(times[len(times) // 2] * 1000, 1), "at": time.time()}
    except FileNotFoundError:
        raise
    except Exception as exc:
        result = {"ok": False, "error": str(exc)[:300], "at": time.time()}
    _engine["results"][device] = result
    return result


def _save_engine() -> None:
    try:
        with open(ENGINE_FILE, "w", encoding="utf-8") as f:
            json.dump({"choice": _engine["choice"]}, f)
    except OSError:
        logger.warning("Could not save the AI engine choice to %s", ENGINE_FILE)


def apply_engine(choice: str) -> dict:
    """Use `choice` (auto | cpu | intel_gpu | nvidia_gpu) from now on. A GPU
    is only kept after it has run real detections; otherwise the CPU stays
    in use and the reason is reported."""
    global _gpu_fails
    with _engine_lock:
        usable = [d["id"] for d in device_options() if d["available"]]
        note = ""
        if choice == "auto":
            cpu = measure("cpu")
            best, best_ms = "cpu", cpu.get("p50_ms") or float("inf")
            for dev in (d for d in usable if d != "cpu"):
                r = measure(dev)
                if r["ok"] and r["p50_ms"] < 0.8 * best_ms:  # clearly faster, or not worth it
                    best, best_ms = dev, r["p50_ms"]
            target = best
        elif choice in usable:
            target = choice
            if choice != "cpu":
                r = measure(choice)
                if not r["ok"]:
                    note = f"{DEVICE_NAMES[choice]} couldn't run the model ({r['error']}) - staying on the CPU."
                    target = "cpu"
        else:
            reason = next((d["reason"] for d in device_options() if d["id"] == choice), "unknown device")
            note = f"{DEVICE_NAMES.get(choice, choice)} isn't usable: {reason}"
            target = "cpu"
        _engine.update(choice=choice, active=target, note=note)
        _gpu_fails = 0
        _detectors.clear()
        _save_engine()
    logger.info("AI engine: choice %s -> running on %s%s", choice, target, f" ({note})" if note else "")
    return engine_status()


def engine_status() -> dict:
    return {"choice": _engine["choice"], "active": _engine["active"], "note": _engine["note"],
            "variant": VARIANT, "devices": device_options()}


def _detector(name: str):
    if name not in _detectors:
        _detectors[name] = _new_detector(name, _engine["active"], strict=False)
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
    return {"cpu": "CPUExecutionProvider", "intel_gpu": "OpenVINOExecutionProvider",
            "nvidia_gpu": "CUDAExecutionProvider"}[_engine["active"]]


def _slots() -> int:
    if SLOTS:
        return SLOTS
    return 4 if device_label(_provider()) in ("cuda", "tensorrt", "rocm") else 2 if device_label(_provider()) == "openvino" else 1


async def _decode(image: UploadFile) -> np.ndarray:
    raw = await image.read(MAX_IMAGE_BYTES + 1)
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Image too large")
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
    choice = LEGACY_PROVIDER.get(PROVIDER, "auto")
    try:
        with open(ENGINE_FILE, encoding="utf-8") as f:
            choice = json.load(f).get("choice", choice)
    except (OSError, ValueError):
        pass
    try:
        await run_in_threadpool(apply_engine, choice if choice in ("auto",) + DEVICES else "auto")
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
    global _gpu_fails
    started = time.perf_counter()
    try:
        boxes = _detector(name).detect(img, min_confidence)
        _gpu_fails = 0
    except FileNotFoundError:
        raise
    except Exception as exc:
        if _engine["active"] == "cpu":
            raise
        # A GPU that keeps failing (driver reset, out of GPU memory) hands over
        # to the CPU instead of leaving detection broken.
        _gpu_fails += 1
        if _gpu_fails < GPU_FAILS_BEFORE_FALLBACK:
            raise
        logger.error("%s keeps failing (%s) - switching to the CPU", DEVICE_NAMES[_engine["active"]], exc)
        with _engine_lock:
            _engine.update(active="cpu", note=f"{DEVICE_NAMES[_engine['active']]} stopped working ({str(exc)[:120]}) "
                                               "- using the CPU. Use 'Test again' to retry it.")
            _detectors.clear()
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
        dets, took = await run_in_threadpool(_tracked, _run_detect, img, name, min_confidence)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"detections": dets, "took_ms": int(took * 1000), "device": device_label(_provider()), "model": name}


@app.post("/v2/faces", dependencies=[Depends(require_key)])
async def faces(image: UploadFile = File(...), boxes: str = Form(default=""), min_face: int = Form(default=40),
                crops: bool = Form(default=False)):
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
    found = await run_in_threadpool(_tracked, engine.find, img, person_boxes, min_face, crops)
    out = []
    for f in found:
        d = f.to_dict()
        if crops and f.crop is not None:
            # The aligned face: what a second recogniser (AdaFace) needs, and a
            # better "Who is this?" picture than a plain box cut.
            ok, buf = cv2.imencode(".jpg", f.crop, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            if ok:
                d["crop"] = base64.b64encode(buf.tobytes()).decode()
        out.append(d)
    return {"faces": out}


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
        "engine": {"choice": _engine["choice"], "active": _engine["active"], "note": _engine["note"]},
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


@app.get("/v2/engine")
async def get_engine():
    return await run_in_threadpool(engine_status)


@app.post("/v2/engine", dependencies=[Depends(require_key)])
async def set_engine(device: str = Form(...)):
    if device not in ("auto",) + DEVICES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="device: auto|cpu|intel_gpu|nvidia_gpu")
    async with _lock:
        return await run_in_threadpool(apply_engine, device)


@app.post("/v2/engine/benchmark", dependencies=[Depends(require_key)])
async def benchmark_engine():
    """Time every usable device (the one in use keeps running meanwhile)."""
    async with _lock:
        def run_all():
            for d in device_options():
                if d["available"]:
                    measure(d["id"])
            return engine_status()

        try:
            return await run_in_threadpool(run_all)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


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
