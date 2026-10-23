# LightNVR Vision (ai-worker). Build from the REPOSITORY ROOT - it shares the
# model code in backend/app/vision with the NVR:
#
#   docker build -f ai-worker/Dockerfile -t lightnvr-ai-worker .                         # cpu
#   docker build -f ai-worker/Dockerfile --build-arg VARIANT=openvino -t ... .           # Intel GPU
#   docker build -f ai-worker/Dockerfile --build-arg VARIANT=cuda \
#                --build-arg BASE_IMAGE=nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04 -t ... .   # NVIDIA
#
# Prebuilt: ghcr.io/manojmkss/light_nvr-ai-worker:<version>-cpu | -openvino | -cuda
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ARG VARIANT=cpu
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    AI_WORKER_MODEL_DIR=/data/models \
    AI_WORKER_VARIANT=${VARIANT}

# curl: healthcheck. libglib/libgl: OpenCV. The CUDA base is Ubuntu without
# Python, so it gets python3 + pip too. The OpenVINO variant needs Intel's
# compute runtime for the iGPU (the CPU path works without it).
RUN apt-get update && apt-get install -y --no-install-recommends curl libglib2.0-0 libgl1 \
    && if ! command -v python3 >/dev/null 2>&1 || ! python3 -m pip --version >/dev/null 2>&1; then \
         apt-get install -y --no-install-recommends python3 python3-pip && ln -sf /usr/bin/python3 /usr/bin/python; \
       fi \
    && if [ "$VARIANT" = "openvino" ]; then \
         apt-get install -y --no-install-recommends intel-opencl-icd ocl-icd-libopencl1 || true; \
       fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY ai-worker/requirements*.txt ai-worker/clear_execstack.py ./
# clear_execstack: some ONNX Runtime wheels (the OpenVINO one) are flagged as
# needing an executable stack, which newer kernels refuse to load - the AI
# service then couldn't run at all. The import check fails the build instead
# of shipping an image whose AI can't start.
RUN python3 -m pip install --no-cache-dir -r requirements.txt \
    && python3 -m pip install --no-cache-dir -r requirements-${VARIANT}.txt \
    && python3 clear_execstack.py "$(python3 -c 'import site; print(site.getsitepackages()[0])')" \
    && python3 -c "import onnxruntime as o; print('onnxruntime providers:', o.get_available_providers())"

# The shared vision package keeps its import path (app.vision).
COPY backend/app/vision /app/app/vision
RUN touch /app/app/__init__.py
COPY ai-worker/app.py /app/worker.py

RUN mkdir -p /data/models
EXPOSE 8811

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=60s \
    CMD curl -f http://localhost:8811/health || exit 1

CMD ["python3", "-m", "uvicorn", "worker:app", "--host", "0.0.0.0", "--port", "8811"]
