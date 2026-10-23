"""YOLO pre/post-processing (letterbox, NMS, output decoding).

Two output layouts are understood:
- YOLOv8/11: (1, 84, 8400) - candidates with (cx, cy, w, h) + 80 class scores,
  needs NMS;
- YOLO26 end-to-end: (1, N, 6) - (x1, y1, x2, y2, score, class), no NMS.
"""

from dataclasses import dataclass

import cv2
import numpy as np

INPUT_SIZE = 640
NMS_IOU = 0.45

COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
    "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
    "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
    "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
]


@dataclass
class Box:
    label: str
    confidence: float
    x: float  # normalised 0..1, top-left
    y: float
    w: float
    h: float


def letterbox(img: np.ndarray, size: int = INPUT_SIZE) -> tuple[np.ndarray, float, int, int]:
    """Resize preserving aspect ratio and pad to a square, as YOLO expects."""
    h, w = img.shape[:2]
    scale = min(size / w, size / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    dx, dy = (size - nw) // 2, (size - nh) // 2
    canvas[dy:dy + nh, dx:dx + nw] = resized
    return canvas, scale, dx, dy


def to_blob(canvas: np.ndarray) -> np.ndarray:
    blob = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return np.transpose(blob, (2, 0, 1))[np.newaxis, ...]


def nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list[int]:
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


def _box(cid: int, conf: float, x: float, y: float, w: float, h: float, orig_w: int, orig_h: int) -> Box:
    label = COCO_CLASSES[cid] if 0 <= cid < len(COCO_CLASSES) else str(cid)
    x0 = max(0.0, min(1.0, x / orig_w))
    y0 = max(0.0, min(1.0, y / orig_h))
    return Box(label, float(conf), x0, y0,
               max(0.0, min(1.0 - x0, w / orig_w)), max(0.0, min(1.0 - y0, h / orig_h)))


def decode(output: np.ndarray, scale: float, dx: int, dy: int, orig_w: int, orig_h: int,
           min_confidence: float) -> list[Box]:
    preds = np.squeeze(output)
    if preds.ndim != 2:
        return []
    if preds.shape[1] == 6 and preds.shape[0] != 6:  # YOLO26 end-to-end
        out = []
        for x1, y1, x2, y2, score, cls in preds:
            if score < min_confidence:
                continue
            ox1, oy1 = (x1 - dx) / scale, (y1 - dy) / scale
            ox2, oy2 = (x2 - dx) / scale, (y2 - dy) / scale
            out.append(_box(int(cls), float(score), ox1, oy1, ox2 - ox1, oy2 - oy1, orig_w, orig_h))
        return out
    if preds.shape[0] < preds.shape[1]:
        preds = preds.transpose()
    class_scores = preds[:, 4:]
    class_ids = np.argmax(class_scores, axis=1)
    confidences = class_scores[np.arange(len(class_ids)), class_ids]
    mask = confidences >= min_confidence
    if not np.any(mask):
        return []
    preds, class_ids, confidences = preds[mask], class_ids[mask], confidences[mask]
    cx, cy, bw, bh = preds[:, 0], preds[:, 1], preds[:, 2], preds[:, 3]
    boxes = np.stack([(cx - bw / 2 - dx) / scale, (cy - bh / 2 - dy) / scale, bw / scale, bh / scale], axis=1)
    return [_box(int(class_ids[i]), float(confidences[i]), *boxes[i], orig_w, orig_h)
            for i in nms(boxes, confidences, NMS_IOU)]


class YoloDetector:
    """One ONNX YOLO model on whichever provider engine.make_session picks."""

    def __init__(self, path: str, preference: str = "auto", threads: int | None = None, strict: bool = False) -> None:
        from app.vision.engine import make_session

        self.session, self.provider = make_session(path, preference, threads, strict)
        self.input_name = self.session.get_inputs()[0].name

    def detect(self, img: np.ndarray, min_confidence: float) -> list[Box]:
        orig_h, orig_w = img.shape[:2]
        canvas, scale, dx, dy = letterbox(img)
        outputs = self.session.run(None, {self.input_name: to_blob(canvas)})
        return decode(outputs[0], scale, dx, dy, orig_w, orig_h, min_confidence)
