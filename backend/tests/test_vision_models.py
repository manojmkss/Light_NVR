"""Checks against the real models, when they're available locally (the CI
test job has no models; run with LIGHTNVR_TEST_MODELS=/path/to/models)."""

import os

import cv2
import numpy as np
import pytest

MODELS = os.environ.get("LIGHTNVR_TEST_MODELS", "")
needs_models = pytest.mark.skipif(not MODELS or not os.path.isdir(MODELS), reason="no local models")


@needs_models
def test_yolo_finds_people_on_the_bus():
    from app.vision.yolo import YoloDetector

    det = YoloDetector(os.path.join(MODELS, "models", "yolov8n.onnx"), "cpu")
    img = cv2.imread(os.path.join(MODELS, "bus.jpg"))
    labels = [b.label for b in det.detect(img, 0.4)]
    assert labels.count("person") >= 3 and "bus" in labels


@needs_models
def test_faces_found_once_and_same_face_matches_itself():
    from app.vision import models as reg
    from app.vision.face import FaceEngine, cosine

    d = os.path.join(MODELS, "models")
    eng = FaceEngine(reg.path_for("yunet", d), reg.path_for("sface", d))
    img = cv2.imread(os.path.join(MODELS, "zidane.jpg"))
    whole = eng.find(img)
    boxed = eng.find(img, [[0.05, 0.2, 0.85, 0.8], [0.55, 0.0, 0.4, 1.0]])  # overlapping boxes
    assert len(whole) == 2 and len(boxed) <= 2
    # The same face, slightly shifted/resized, must still match strongly.
    face = max(whole, key=lambda f: f.quality)
    h, w = img.shape[:2]
    shifted = cv2.resize(img[5:, 5:], (w, h))
    again = max(eng.find(shifted), key=lambda f: f.quality)
    assert cosine(face.embedding, again.embedding) > 0.6
    # ...and the two different people don't.
    other = min(whole, key=lambda f: f.quality)
    assert cosine(whole[0].embedding, whole[1].embedding) < 0.4 or other.quality < 0.1


def test_manifest_has_checksums_and_licences():
    from app.vision.models import MANIFEST

    for name, m in MANIFEST.items():
        assert len(m["sha256"]) == 64 and m["license"] and m["url"].startswith("https://")


def test_engine_falls_back_to_cpu_for_unknown_provider():
    from app.vision.engine import choose_providers

    providers = choose_providers("tensorrt")
    assert providers[-1] == "CPUExecutionProvider"


def test_fake_frame_detect_shape_handles_empty_output():
    from app.vision.yolo import decode

    assert decode(np.zeros((1, 84, 8400), dtype=np.float32), 1.0, 0, 0, 640, 640, 0.5) == []
