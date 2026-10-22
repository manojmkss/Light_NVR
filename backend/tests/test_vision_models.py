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


needs_adaface = pytest.mark.skipif(
    not MODELS or not os.path.exists(os.path.join(MODELS, "models", "adaface_ir_18.onnx")), reason="no AdaFace model"
)


@needs_adaface
def test_adaface_separates_people_and_realigns_saved_pictures():
    from app.vision import models as reg
    from app.vision.face import AdaFace, FaceEngine, realign

    d = os.path.join(MODELS, "models")
    eng = FaceEngine(reg.path_for("yunet", d), reg.path_for("sface", d))
    ada = AdaFace(reg.path_for("adaface", d))
    img = cv2.imread(os.path.join(MODELS, "zidane.jpg"))
    a, b = sorted(eng.find(img, keep_crops=True), key=lambda f: f.x)[:2]
    soft = cv2.resize(cv2.resize(img, (img.shape[1] // 3, img.shape[0] // 3)), (img.shape[1], img.shape[0]))
    a2 = min(eng.find(soft, keep_crops=True), key=lambda f: abs(f.x - a.x))
    ea, eb, ea2 = ada.embed(a.crop), ada.embed(b.crop), ada.embed(a2.crop)
    assert float(ea @ ea2) > 0.6  # same person, CCTV-soft copy
    assert float(ea @ eb) < 0.2  # different people
    # A plain (unaligned) box cut, like older AI workers saved, is re-aligned.
    H, W = img.shape[:2]
    box = img[int(a.y * H):int((a.y + a.h) * H), int(a.x * W):int((a.x + a.w) * W)]
    again = realign(eng, cv2.resize(box, (112, 112)))
    assert again is not None and float(ada.embed(again) @ ea) > 0.6
