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
  python:3.11-slim bash -c "
    set -e
    mkdir -p /data/models
    if [ -f /data/models/${MODEL}.onnx ]; then
      echo 'Already present: /data/models/${MODEL}.onnx - nothing to do.'
      exit 0
    fi
    pip install --no-cache-dir --quiet ultralytics onnx onnxruntime
    cd /tmp
    python - <<'PY'
from ultralytics import YOLO
m = YOLO('${MODEL}.pt')
# opset 12 keeps it loadable by the pinned onnxruntime in the backend image.
m.export(format='onnx', opset=12, simplify=False, dynamic=False, imgsz=640)
PY
    cp /tmp/${MODEL}.onnx /data/models/${MODEL}.onnx
    echo 'Installed /data/models/${MODEL}.onnx'
  "

echo
echo "==> Done. Now enable it in the web UI:"
echo "    Settings -> AI -> enable, backend = Local (CPU), model = ${MODEL}, then Test."
