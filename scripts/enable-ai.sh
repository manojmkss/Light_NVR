#!/usr/bin/env bash
# LightNVR - set up the local AI service (LightNVR Vision) on this machine.
#
# Detects what this machine can actually do (CPU / Intel GPU / NVIDIA GPU),
# starts the matching ai-worker image next to the NVR, and PROVES it with a
# real benchmark - a GPU that shows up but can't run inference (common for an
# Intel iGPU passed through to an ESXi VM) is caught here and the CPU image is
# used instead. Everything it changes lives in .env, so update-linux.sh keeps
# working exactly as before.
#
# Usage:
#   ./scripts/enable-ai.sh [--variant auto|cpu|openvino|cuda] [--cpus N] [--yes] [--dry-run]
#   ./scripts/enable-ai.sh --disable
#
# Afterwards, in the web UI: Settings -> AI -> Where it runs -> "This NVR's AI service".

set -euo pipefail

VARIANT="auto"
DISABLE=false
ASSUME_YES=false
DRY_RUN=false
CPUS=""
usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --variant) VARIANT="$2"; shift 2 ;;
    --variant=*) VARIANT="${1#*=}"; shift ;;
    --cpus) CPUS="$2"; shift 2 ;;
    --disable) DISABLE=true; shift ;;
    -y|--yes) ASSUME_YES=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

log()  { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$1" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$1" >&2; exit 1; }
confirm() { $ASSUME_YES && return 0; read -r -p "$1 [y/N] " r; [[ "$r" =~ ^[Yy]$ ]]; }
run() { if $DRY_RUN; then printf '\033[2m[DRY RUN] %s\033[0m\n' "$*"; else "$@"; fi; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"
ENV_FILE="$REPO_ROOT/.env"
# shellcheck source=lib-env.sh
source "$SCRIPT_DIR/lib-env.sh"

DOCKER=(docker)
docker info >/dev/null 2>&1 || DOCKER=(sudo docker)

AI_FILES=(docker-compose.ai.yml docker-compose.ai.intel.yml docker-compose.ai.nvidia.yml)

compose_files_without_ai() {
  local current; current="$(env_get COMPOSE_FILE)"
  [ -n "$current" ] || current="docker-compose.yml"
  local out=() f
  IFS=':' read -r -a parts <<< "$current"
  for f in "${parts[@]}"; do
    case " ${AI_FILES[*]} " in *" $f "*) ;; *) out+=("$f") ;; esac
  done
  (IFS=':'; echo "${out[*]}")
}

if $DISABLE; then
  base="$(compose_files_without_ai)"
  log "Removing the AI service from this NVR."
  if ! $DRY_RUN; then
    "${DOCKER[@]}" compose stop ai-worker >/dev/null 2>&1 || true
    "${DOCKER[@]}" compose rm -f ai-worker >/dev/null 2>&1 || true
    if [ "$base" = "docker-compose.yml" ]; then env_unset COMPOSE_FILE; else env_set COMPOSE_FILE "$base"; fi
    for k in AI_WORKER_VARIANT AI_CPUS AI_MEM_LIMIT RENDER_GID AI_WORKER_PROVIDER; do env_unset "$k"; done
  fi
  log "Done. In the web UI switch Settings -> AI -> Where it runs back to 'This machine (CPU)' or turn AI off."
  exit 0
fi

# ---------------------------------------------------------------------------
# 1. What is this machine?
# ---------------------------------------------------------------------------
ARCH="$(uname -m)"
CORES="$(nproc 2>/dev/null || echo 2)"
RAM_MB="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
HAS_DRI=false; ls /dev/dri/renderD* >/dev/null 2>&1 && HAS_DRI=true
INTEL_GPU=false; AMD_GPU=false
if command -v lspci >/dev/null 2>&1; then
  lspci 2>/dev/null | grep -Ei 'vga|3d|display' | grep -qi intel && INTEL_GPU=true
  lspci 2>/dev/null | grep -Ei 'vga|3d|display' | grep -Eqi 'amd|ati' && AMD_GPU=true
fi
HAS_NVIDIA=false; command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1 && HAS_NVIDIA=true
NVIDIA_TOOLKIT=false
if "${DOCKER[@]}" info 2>/dev/null | grep -qi nvidia || command -v nvidia-ctk >/dev/null 2>&1; then NVIDIA_TOOLKIT=true; fi
RENDER_GID="$(getent group render 2>/dev/null | cut -d: -f3 || true)"

log "This machine: $ARCH, $CORES cores, ${RAM_MB} MB RAM"
$HAS_NVIDIA && log "  NVIDIA GPU: $(nvidia-smi -L 2>/dev/null | head -1)$($NVIDIA_TOOLKIT || echo ' (NVIDIA Container Toolkit NOT installed)')"
$INTEL_GPU && log "  Intel GPU: present$($HAS_DRI || echo ' (but /dev/dri is missing - not usable)')"
$AMD_GPU && log "  AMD GPU: present (ROCm isn't supported yet - the CPU image will be used)"

if [ "$VARIANT" = "auto" ]; then
  if [ "$ARCH" != "x86_64" ]; then
    VARIANT="cpu"
  elif $HAS_NVIDIA && $NVIDIA_TOOLKIT; then
    VARIANT="cuda"
  elif $INTEL_GPU && $HAS_DRI; then
    VARIANT="openvino"
  else
    VARIANT="cpu"
  fi
fi
case "$VARIANT" in cpu|openvino|cuda) ;; *) die "Unknown variant '$VARIANT' (cpu|openvino|cuda)";; esac
if [ "$VARIANT" = "cuda" ] && ! $NVIDIA_TOOLKIT; then
  die "The cuda image needs the NVIDIA Container Toolkit: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
fi
if [ "$ARCH" != "x86_64" ] && [ "$VARIANT" != "cpu" ]; then
  die "Only the cpu image is built for $ARCH."
fi

[ -n "$CPUS" ] || CPUS=$(( CORES > 2 ? CORES / 2 : 1 ))
MEM_LIMIT="1536m"; [ "$RAM_MB" -ge 8000 ] && MEM_LIMIT="3g"

log "Plan: run the '$VARIANT' AI image as a sidecar, limited to $CPUS CPU(s) and $MEM_LIMIT RAM."
confirm "Go ahead?" || die "Aborted."

apply_variant() {
  local v="$1" files
  files="$(compose_files_without_ai):docker-compose.ai.yml"
  [ "$v" = "openvino" ] && files="$files:docker-compose.ai.intel.yml"
  [ "$v" = "cuda" ] && files="$files:docker-compose.ai.nvidia.yml"
  env_set COMPOSE_FILE "$files"
  env_set AI_WORKER_VARIANT "$v"
  env_set AI_CPUS "$CPUS"
  env_set AI_MEM_LIMIT "$MEM_LIMIT"
  if [ "$v" = "openvino" ]; then env_set RENDER_GID "${RENDER_GID:-109}"; fi
  if [ "$v" = "cuda" ]; then env_set AI_WORKER_BASE_IMAGE "nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04"; else env_unset AI_WORKER_BASE_IMAGE; fi
}

start_worker() {
  # Released images match a v* checkout; otherwise build from this source.
  local tag; tag="$(git tag --points-at HEAD 2>/dev/null | grep -E '^v[0-9]' | sort -V | tail -1 || true)"
  if [ -n "$tag" ]; then
    export LIGHTNVR_VERSION="${tag#v}"
    "${DOCKER[@]}" compose pull ai-worker || "${DOCKER[@]}" compose build ai-worker
  else
    "${DOCKER[@]}" compose build ai-worker
  fi
  "${DOCKER[@]}" compose up -d ai-worker
}

benchmark() {
  local out i
  for i in $(seq 1 30); do
    if "${DOCKER[@]}" compose exec -T ai-worker curl -fs http://localhost:8811/health >/dev/null 2>&1; then break; fi
    sleep 2
  done
  out="$("${DOCKER[@]}" compose exec -T ai-worker curl -fs -X POST -F runs=12 http://localhost:8811/v2/benchmark 2>/dev/null || true)"
  echo "$out"
}

if $DRY_RUN; then
  log "[DRY RUN] would write COMPOSE_FILE/AI_* to .env, start the $VARIANT image and benchmark it."
  exit 0
fi

apply_variant "$VARIANT"
log "Starting the AI service ($VARIANT)..."
start_worker

MODEL="$("${DOCKER[@]}" compose exec -T ai-worker sh -c 'echo $AI_WORKER_MODEL' 2>/dev/null | tr -d '\r' || echo yolov8n)"
if ! "${DOCKER[@]}" compose exec -T ai-worker test -f "/data/models/${MODEL}.onnx"; then
  warn "The detection model '${MODEL}' isn't downloaded yet."
  if confirm "Download it now (runs scripts/fetch-ai-models.sh ${MODEL})?"; then
    "$SCRIPT_DIR/fetch-ai-models.sh" "$MODEL"
    "${DOCKER[@]}" compose restart ai-worker
  else
    warn "Run ./scripts/fetch-ai-models.sh ${MODEL} later, then re-run this script to benchmark."
    exit 0
  fi
fi

log "Measuring real detections..."
RESULT="$(benchmark)"
DEVICE="$(echo "$RESULT" | sed -n 's/.*"device": *"\([a-z]*\)".*/\1/p')"
P50="$(echo "$RESULT" | sed -n 's/.*"p50_ms": *\([0-9.]*\).*/\1/p')"
if [ -z "$P50" ]; then
  warn "Benchmark failed: ${RESULT:-no answer}"
  if [ "$VARIANT" != "cpu" ]; then
    warn "Falling back to the cpu image."
    VARIANT="cpu"; apply_variant cpu; start_worker; RESULT="$(benchmark)"
    P50="$(echo "$RESULT" | sed -n 's/.*"p50_ms": *\([0-9.]*\).*/\1/p')"
    DEVICE="cpu"
  fi
  [ -n "$P50" ] || die "The AI service doesn't answer - see: ${DOCKER[*]} compose logs ai-worker"
elif [ "$VARIANT" != "cpu" ] && [ "$DEVICE" = "cpu" ]; then
  warn "The $VARIANT image started, but inference is running on the CPU (the GPU isn't usable from the container)."
  warn "Using the cpu image instead - it's the same speed without the larger download."
  VARIANT="cpu"; apply_variant cpu; start_worker; RESULT="$(benchmark)"
  P50="$(echo "$RESULT" | sed -n 's/.*"p50_ms": *\([0-9.]*\).*/\1/p')"; DEVICE="cpu"
fi

log "AI service ready: $VARIANT image, inference on ${DEVICE:-cpu}, ${P50} ms per check."
echo
log "Last step, in the web UI: Settings -> AI -> Where it runs -> 'This NVR's AI service',"
log "Save, then 'Measure this hardware' for the features it can sustain."
