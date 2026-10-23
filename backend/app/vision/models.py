"""Where each optional model comes from, its licence, and its checksum.

Models are never bundled in the images and never fetched behind the user's
back: they are downloaded only when a feature is switched on (with the
licence shown first), into the models volume, and verified against the
SHA-256 here before use.
"""

import hashlib
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

MANIFEST: dict[str, dict] = {
    "yunet": {
        "file": "face_detection_yunet_2023mar.onnx",
        "url": "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx",
        "sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        "size_mb": 0.2,
        "license": "MIT (OpenCV Zoo YuNet)",
        "task": "face detection",
    },
    "sface": {
        "file": "face_recognition_sface_2021dec.onnx",
        "url": "https://huggingface.co/opencv/face_recognition_sface/resolve/main/face_recognition_sface_2021dec.onnx",
        "sha256": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        "size_mb": 37,
        "license": "Apache-2.0 (OpenCV Zoo SFace)",
        "task": "face recognition",
    },
    # Optional second face recogniser, tried side by side with SFace on the
    # user's own confirmed faces before anything switches (People page).
    "adaface": {
        "file": "adaface_ir_18.onnx",
        "url": "https://github.com/yakhyo/adaface-onnx/releases/download/weights/adaface_ir_18.onnx",
        "sha256": "6b6a35772fb636cdd4fa86520c1a259d0c41472a76f70f802b351837a00d9870",
        "size_mb": 92,
        "license": "MIT code (AdaFace, CVPR 2022; ONNX export yakhyo/adaface-onnx). The weights were trained on "
                   "WebFace4M, a research-use dataset: fine for your own home, not for selling a product.",
        "task": "face recognition (built for low-quality faces)",
    },
}


# Ready-made object-detection models (Settings -> AI -> AI engine -> Detection
# model), exported once in CI (.github/workflows/ai-models.yml) and published
# as release assets, so nobody needs the 2 GB exporter on their own machine.
# "accuracy" is the model's COCO mAP50-95 as published by Ultralytics: higher
# finds more people and fewer phantoms, especially small/far-away ones
# (YOLO26 figures are its end-to-end, NMS-free ones - the exported form).
DETECTOR_RELEASE = "https://github.com/manojmkss/Light_NVR/releases/download/ai-models-1"
DETECTOR_LICENSE = "AGPL-3.0 (Ultralytics YOLO) - the same licence as LightNVR"
DETECTORS: dict[str, dict] = {
    "yolo26n": {"label": "Fast", "family": "YOLO26", "accuracy": 40.1, "size_mb": 9.9,
                "sha256": "8c681d75f508fad6ec9861bed382b2c56ffb1a210e61e8ee779e736a2bfbe613",
                "about": "Light enough for a CPU. Clearly better than the original model at the same speed."},
    "yolo26s": {"label": "Balanced", "family": "YOLO26", "accuracy": 47.8, "size_mb": 38.2,
                "sha256": "5777c519ef53e9378db1e7e23359edd4025268cfa08e3af57ff9e585161a5fbe",
                "about": "Finds far-away and partly hidden people much more reliably. Best choice with a GPU."},
    "yolo26m": {"label": "Accurate", "family": "YOLO26", "accuracy": 52.5, "size_mb": 81.9,
                "sha256": "ba84712d61934f07bdad82e8b23654e278b991e264d7ed71ca34fbfea0a53c3e",
                "about": "The most accurate. Needs a GPU, or a fast CPU watching only a few cameras."},
    "yolov8n": {"label": "Original", "family": "YOLOv8", "accuracy": 37.3, "size_mb": 12.8,
                "sha256": "e4b65e02f8ce7ad246e5c0fa5ee5a2500531ba20e275f90a18717cf2d3e33691",
                "about": "What LightNVR used until now. Misses the most."},
}


def detector_file(name: str) -> str:
    return f"{name}.onnx"


def path_for(name: str, model_dir: str) -> str:
    return os.path.join(model_dir, MANIFEST[name]["file"])


def present(name: str, model_dir: str) -> bool:
    return os.path.exists(path_for(name, model_dir))


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(name: str, model_dir: str, timeout: float = 120.0) -> str:
    """Fetch one model into model_dir (atomically), verifying its checksum.
    Raises on any mismatch - a wrong file is never left in place."""
    entry = MANIFEST[name]
    return _fetch(entry["url"], path_for(name, model_dir), entry.get("sha256", ""), timeout)


def download_detector(name: str, model_dir: str, timeout: float = 600.0) -> str:
    """A ready-made detection model, checked against the checksum pinned here
    (never one read from the download site itself)."""
    entry = DETECTORS[name]
    if not entry["sha256"]:
        raise RuntimeError(f"{name} has no published checksum in this version")
    return _fetch(f"{DETECTOR_RELEASE}/{detector_file(name)}", os.path.join(model_dir, detector_file(name)),
                  entry["sha256"], timeout)


def _fetch(url: str, dest: str, sha256: str, timeout: float) -> str:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest) and (not sha256 or sha256_of(dest) == sha256):
        return dest
    tmp = dest + ".part"
    logger.info("Downloading model %s from %s", os.path.basename(dest), url)
    req = urllib.request.Request(url, headers={"User-Agent": "LightNVR"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as out:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    digest = sha256_of(tmp)
    if sha256 and digest != sha256:
        os.remove(tmp)
        raise RuntimeError(f"Checksum mismatch for {os.path.basename(dest)}: got {digest}")
    os.replace(tmp, dest)
    return dest


def status(model_dir: str) -> list[dict]:
    return [
        {"name": n, "file": e["file"], "present": present(n, model_dir), "size_mb": e["size_mb"],
         "license": e["license"], "task": e["task"]}
        for n, e in MANIFEST.items()
    ]
