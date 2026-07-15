from dataclasses import dataclass

# Where locally-run models live. This is inside the `lightnvr-data` named
# volume, so a model downloaded once survives container rebuilds and isn't
# re-fetched on every upgrade.
MODEL_DIR = "/data/models"


@dataclass
class DetectedObject:
    """One object found in one frame. Coordinates are normalised 0..1 against
    the frame (see Detection model for why not pixels)."""

    label: str
    confidence: float  # 0..1
    x: float
    y: float
    w: float
    h: float


# The 80 COCO classes YOLOv8/YOLO11 are trained on, in model output order -
# the model emits an index, this turns it into a name. Only a handful matter
# for an NVR (person/vehicles/animals), which is what the class filter in
# AISettings is for, but the full list has to be here for the index mapping
# to line up.
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

# Classes an NVR operator plausibly cares about, offered in the Settings UI
# rather than making them scroll all 80.
SUGGESTED_CLASSES = [
    "person", "car", "truck", "bus", "motorcycle", "bicycle", "dog", "cat", "bird",
    "backpack", "handbag", "suitcase",
]

# "package" isn't a COCO class. Approximated from the carried-item classes
# above, which is honest about what the model can actually tell you: a stock
# YOLO cannot recognise a parcel on a doorstep, only a suitcase/backpack-like
# shape. A purpose-trained model is the real answer if that matters.
PACKAGE_PROXY_CLASSES = {"backpack", "handbag", "suitcase"}
