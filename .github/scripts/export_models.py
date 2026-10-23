"""Export the ready-made detection models LightNVR offers (Settings -> AI ->
AI engine -> Detection model) and write their manifest.

Each model is exported once here, in CI, instead of on every user's machine:
the export needs PyTorch + Ultralytics (~2 GB), the NVR only needs the
resulting ONNX file. Every export is checked with LightNVR's own decoder on a
real photo before it is published, and its SHA-256 goes into manifest.json,
which is then pinned in backend/app/vision/models.py.

    python .github/scripts/export_models.py out/ yolo11n yolo11s ...
"""

import hashlib
import json
import os
import shutil
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

import cv2  # noqa: E402
from ultralytics import YOLO  # noqa: E402

from app.vision.yolo import YoloDetector  # noqa: E402

PHOTO = "https://ultralytics.com/images/bus.jpg"  # 4 people and a bus


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    out_dir, names = sys.argv[1], sys.argv[2:]
    os.makedirs(out_dir, exist_ok=True)
    photo = os.path.join(out_dir, "bus.jpg")
    urllib.request.urlretrieve(PHOTO, photo)
    img = cv2.imread(photo)
    manifest = {}
    for name in names:
        # opset 12 keeps the file loadable by older onnxruntime builds too.
        exported = YOLO(f"{name}.pt").export(format="onnx", opset=12, simplify=False, dynamic=False, imgsz=640)
        dest = os.path.join(out_dir, f"{name}.onnx")
        shutil.copy(exported, dest)
        boxes = YoloDetector(dest, "cpu").detect(img, 0.4)
        people = sum(b.label == "person" for b in boxes)
        print(f"{name}: {len(boxes)} objects, {people} people")
        if people < 3:
            raise SystemExit(f"{name}: the exported model doesn't decode correctly ({people} people in bus.jpg)")
        manifest[name] = {"file": f"{name}.onnx", "sha256": sha256(dest),
                          "size_mb": round(os.path.getsize(dest) / 1e6, 1), "people_in_test_photo": people}
    os.remove(photo)
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
