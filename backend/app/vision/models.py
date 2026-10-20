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
}


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
    os.makedirs(model_dir, exist_ok=True)
    dest = path_for(name, model_dir)
    if os.path.exists(dest) and (not entry.get("sha256") or sha256_of(dest) == entry["sha256"]):
        return dest
    tmp = dest + ".part"
    logger.info("Downloading model %s from %s", name, entry["url"])
    req = urllib.request.Request(entry["url"], headers={"User-Agent": "LightNVR"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as out:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    digest = sha256_of(tmp)
    if entry.get("sha256") and digest != entry["sha256"]:
        os.remove(tmp)
        raise RuntimeError(f"Checksum mismatch for {name}: got {digest}")
    os.replace(tmp, dest)
    return dest


def status(model_dir: str) -> list[dict]:
    return [
        {"name": n, "file": e["file"], "present": present(n, model_dir), "size_mb": e["size_mb"],
         "license": e["license"], "task": e["task"]}
        for n, e in MANIFEST.items()
    ]
