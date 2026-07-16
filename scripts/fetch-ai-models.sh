#!/usr/bin/env bash
# Downloads the ONNX model(s) the optional AI layer uses for local (CPU)
# inference, into the `lightnvr-data` Docker volume at /data/models.
#
# Why a script instead of an auto-download at runtime: pulling a model from
# the internet on first motion event would make a security product silently
# fetch and execute-adjacent a remote binary blob, at the least predictable
# moment, with no operator consent. This makes it an explicit, auditable step
# you run once - and it's skippable entirely if you use the "remote" backend
# (the GPU ai-worker ships its own models).
#
# Usage:
#   ./scripts/fetch-ai-models.sh              # default: yolov8n
#   ./scripts/fetch-ai-models.sh yolov8s      # a bigger/more accurate variant
#
# Models are exported from Ultralytics inside a throwaway container, so
# nothing is installed on your host and the export is reproducible.

set -euo pipefail

MODEL="${1:-yolov8n}"
VOLUME="lightnvr-data"

case "$MODEL" in
  yolov8n|yolov8s|yolov8m|yolo11n|yolo11s|yolo11m) ;;
  *)
    echo "Unsupported model '$MODEL'." >&2
    echo "Supported: yolov8n yolov8s yolov8m yolo11n yolo11s yolo11m" >&2
    exit 1
    ;;
esac

if ! docker volume inspect "$VOLUME" >/dev/null 2>&1; then
  echo "Docker volume '$VOLUME' not found - start LightNVR once first (docker compose up -d)." >&2
  exit 1
fi

echo "==> Exporting $MODEL to ONNX and installing it into the $VOLUME volume."
echo "    (First run pulls a ~2GB build image; the exported model itself is only a few MB.)"

docker run --rm \
  -v "${VOLUME}:/data" \
  -e MODEL="${MODEL}" \
  -e DEBIAN_FRONTEND=noninteractive \
  python:3.11-slim bash -c '
    set -e
    mkdir -p /data/models
    if [ -f "/data/models/${MODEL}.onnx" ]; then
      echo "Already present: /data/models/${MODEL}.onnx - nothing to do."
      exit 0
    fi
    echo "Installing build dependencies (a few minutes on first run)..."
    # ultralytics hard-depends on opencv-python (the GUI build), which needs
    # X11 libs this slim image lacks - without these the import dies with
    # "libxcb.so.1: cannot open shared object file". Same two packages the
    # backend image installs for exactly this reason.
    #
    # DEBIAN_FRONTEND (set on the container above) stops debconf trying to open
    # an interactive dialog it can never have in a non-TTY container and then
    # dumping a wall of "unable to initialize frontend" fallback chatter to
    # stderr. It always recovered on its own, but it reads like a crash.
    # stderr is redirected too since apt still warns about its own CLI not
    # being a stable interface.
    apt-get update -qq >/dev/null 2>&1
    apt-get install -y -qq --no-install-recommends libgl1 libglib2.0-0 >/dev/null 2>&1
    echo "Installing ultralytics (this is the slow part)..."
    pip install --no-cache-dir --quiet ultralytics onnx onnxruntime
    echo "Exporting ${MODEL} to ONNX..."
    cd /tmp
    python - <<PY
import shutil
from ultralytics import YOLO
m = YOLO("/tmp/${MODEL}.pt")
# opset 12 keeps it loadable by the pinned onnxruntime in the backend image.
out = m.export(format="onnx", opset=12, simplify=False, dynamic=False, imgsz=640)
shutil.copy(out, "/data/models/${MODEL}.onnx")
PY
    echo "Installed /data/models/${MODEL}.onnx"
  '

# Trust but verify - a zero exit above does not prove the file reached the volume.
# Echoes a sentinel rather than leaning on `test`'s exit code, which doesn't
# reliably survive the trip back out of `docker run`.
FOUND="$(docker run --rm -v "${VOLUME}:/data" alpine sh -c "[ -f /data/models/${MODEL}.onnx ] && echo FOUND || echo MISSING")"
if [ "${FOUND}" != "FOUND" ]; then
  echo "Export reported success but /data/models/${MODEL}.onnx isn't in the volume." >&2
  exit 1
fi

echo
echo "==> Done. Now enable it in the web UI:"
echo "    Settings -> AI -> Use AI -> Where it runs: This machine (CPU)"
echo "    Model: ${MODEL}, then press 'Check it works'."
