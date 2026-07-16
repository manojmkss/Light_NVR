# Default build is CPU-only so `docker compose up` works on any machine.
# For an NVIDIA GPU, build with:
#   docker compose build --build-arg BASE_IMAGE=pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime
# and uncomment the `deploy.resources` GPU block in docker-compose.yml.
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AI_WORKER_MODEL_DIR=/models \
    YOLO_CONFIG_DIR=/models

# libgl/libglib: ultralytics imports OpenCV, which needs them even in a
# headless container. curl is for the healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# The CUDA base image already ships torch; installing ultralytics would drag in
# a second (CPU) copy and silently shadow it, so torch is left to the base image
# when it's already there.
RUN if python -c "import torch" 2>/dev/null; then \
        grep -v '^ultralytics' requirements.txt > /tmp/req.txt && \
        pip install --no-cache-dir -r /tmp/req.txt && \
        pip install --no-cache-dir --no-deps ultralytics==8.3.0 && \
        pip install --no-cache-dir pyyaml requests scipy matplotlib pandas seaborn psutil py-cpuinfo opencv-python-headless ; \
    else \
        pip install --no-cache-dir -r requirements.txt ; \
    fi

COPY app.py .

RUN mkdir -p /models

EXPOSE 8811

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=60s \
    CMD curl -f http://localhost:8811/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8811"]
