#!/usr/bin/env bash
# Shared helpers for install-linux.sh and update-linux.sh.
#
# The design principle these support: all machine-specific configuration lives
# in the gitignored .env file, never in the tracked docker-compose.yml. That
# keeps `git pull` a clean fast-forward on every upgrade, which is what makes a
# one-command updater possible. These helpers read/write .env idempotently and
# auto-detect the network/storage facts a user would otherwise have to look up
# and type by hand.
#
# Sourced, not executed. Expects ENV_FILE to be set by the caller.

# --- .env read/write (idempotent) ------------------------------------------

# env_set KEY VALUE - replace KEY's line in $ENV_FILE, or append it if absent.
# Uses | as the sed delimiter so values containing / (paths) and : (compose
# file lists) pass through untouched.
env_set() {
  local key="$1" value="$2"
  touch "$ENV_FILE"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

# env_get KEY - print KEY's current value from $ENV_FILE (empty if unset).
#
# Always succeeds: an absent key is a normal answer for a getter, not an error.
# The trailing `|| true` is load-bearing. Callers write `x="$(env_get KEY)"`,
# and an assignment from a command substitution takes that command's exit
# status - so under the `set -euo pipefail` these scripts run with, grep's
# exit 1 for "no match" aborts the whole script, silently, with no message at
# all. That is exactly how an update could stop dead right after pulling and
# never rebuild anything.
env_get() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  grep -E "^${key}=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true
}

# env_unset KEY - remove KEY's line from $ENV_FILE entirely.
env_unset() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  sed -i "/^${key}=/d" "$ENV_FILE"
}

# --- network auto-detection -------------------------------------------------
# All derived from the kernel routing/address tables - no external tools
# (ipcalc/python) and no arithmetic needed: the connected-route entry already
# carries the network address in CIDR form.

detect_default_iface() { ip route 2>/dev/null | awk '/^default/ {print $5; exit}'; }
detect_gateway()       { ip route 2>/dev/null | awk '/^default/ {print $3; exit}'; }

# detect_subnet IFACE - the interface's connected network in CIDR form, e.g.
# 192.168.68.0/22, taken straight from the kernel route table.
detect_subnet() {
  local iface="$1"
  ip route show dev "$iface" 2>/dev/null | awk '/proto kernel/ {print $1; exit}'
}

# find_free_ip GATEWAY - probe a few high host addresses in the gateway's /24
# neighbourhood (which is inside any wider subnet too) and echo the first that
# doesn't answer a ping. These high addresses are the least likely to collide
# with a router's DHCP pool. Returns non-zero if all probed addresses replied.
find_free_ip() {
  local gateway="$1"
  local base="${gateway%.*}"   # 192.168.68.1 -> 192.168.68
  local last
  for last in 250 251 249 248 252 247 246 245; do
    local cand="${base}.${last}"
    if ! ping -c1 -W1 "$cand" >/dev/null 2>&1; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

# --- storage helpers --------------------------------------------------------

# is_real_mountpoint PATH - true only if PATH is an actual mount (a separate
# filesystem), not just a directory on the root disk. This is the check that
# catches the classic failure: pointing storage at a path where the intended
# disk was never actually mounted, so recordings silently land on the OS disk.
is_real_mountpoint() { mountpoint -q "$1" 2>/dev/null; }

# --- migration off the old hand-edited-compose scheme -----------------------

# detect_handedited_primary_path REPO_ROOT - if docker-compose.yml has an
# uncommitted edit pointing the primary bind mount at an absolute host path
# (the pre-.env way of using a dedicated disk), echo that path. Empty
# otherwise. Deliberately matches only a leading-slash absolute path, so the
# new ${PRIMARY_STORAGE_PATH:-...} form and the ./default never trigger it.
detect_handedited_primary_path() {
  local repo="$1"
  command -v git >/dev/null 2>&1 || return 0
  git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 || return 0
  git -C "$repo" diff --quiet -- docker-compose.yml 2>/dev/null && return 0
  git -C "$repo" diff -- docker-compose.yml 2>/dev/null \
    | sed -n 's|^+[[:space:]]*-[[:space:]]*\(/[^:]*\):/mnt/primary.*|\1|p' | head -1
}

# --- AI engine hardware (every install and update) -------------------------
#
# The AI service (ai-worker) is part of every Linux install and idles until AI
# is turned on in the web UI. Which image it runs, and whether it can reach a
# GPU, can only be set when its container is created - so the install/update
# scripts set it, from what this machine has *now*. That is what lets the web
# page switch between CPU and GPU on its own (Settings -> AI -> AI engine):
# adding a GPU later is picked up by the normal update.
#
#   AI_WORKER=off            in .env: no AI service at all
#   AI_WORKER_VARIANT_AUTO=off   keep the image chosen with enable-ai.sh --variant

AI_COMPOSE_FILES="docker-compose.ai.yml docker-compose.ai.intel.yml docker-compose.ai.nvidia.yml"

# compose_without_ai - the COMPOSE_FILE list minus the AI service's files.
compose_without_ai() {
  local current out="" f
  current="$(env_get COMPOSE_FILE)"
  [[ -n "$current" ]] || current="docker-compose.yml"
  local IFS=':'
  for f in $current; do
    case " $AI_COMPOSE_FILES " in *" $f "*) ;; *) out="${out:+$out:}$f" ;; esac
  done
  echo "$out"
}

# detect_ai_hardware - sets HW_ARCH HW_INTEL HW_DRI HW_RENDER_GID HW_NVIDIA
# HW_NVIDIA_TOOLKIT HW_GPUS (one "vendor|name" per line) HW_VIRT.
detect_ai_hardware() {
  local dk=("${DOCKER[@]:-docker}")
  HW_ARCH="$(uname -m)"
  HW_GPUS=""; HW_INTEL=false; HW_DRI=false; HW_RENDER_GID=""
  HW_NVIDIA=false; HW_NVIDIA_TOOLKIT=false
  HW_VIRT="$(systemd-detect-virt 2>/dev/null || true)"
  if command -v lspci >/dev/null 2>&1; then
    HW_GPUS="$(lspci 2>/dev/null | grep -Ei 'vga|3d|display' | sed -E 's/^[^ ]+ [^:]+: //' || true)"
    echo "$HW_GPUS" | grep -qi intel && HW_INTEL=true
  elif [[ -d "${LIGHTNVR_SYS_DRM:-/sys/class/drm}" ]] && grep -qs 0x8086 "${LIGHTNVR_SYS_DRM:-/sys/class/drm}"/card*/device/vendor; then
    HW_INTEL=true; HW_GPUS="Intel GPU"
  fi
  local node
  node="$(ls "${LIGHTNVR_DEV_DRI:-/dev/dri}"/renderD* 2>/dev/null | head -1 || true)"
  if [[ -n "$node" ]]; then HW_DRI=true; HW_RENDER_GID="$(stat -c %g "$node" 2>/dev/null || true)"; fi
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then HW_NVIDIA=true; fi
  if "${dk[@]}" info 2>/dev/null | grep -qi 'nvidia'; then HW_NVIDIA_TOOLKIT=true; fi
}

# configure_ai_service - write COMPOSE_FILE / AI_* for this machine's hardware.
configure_ai_service() {
  detect_ai_hardware
  local base variant files
  base="$(compose_without_ai)"
  if [[ "$(env_get AI_WORKER)" == "off" ]]; then
    if [[ "$base" == "docker-compose.yml" ]]; then env_unset COMPOSE_FILE; else env_set COMPOSE_FILE "$base"; fi
    return 0
  fi
  variant="$(env_get AI_WORKER_VARIANT)"
  if [[ "$(env_get AI_WORKER_VARIANT_AUTO)" != "off" || -z "$variant" ]]; then
    if [[ "$HW_ARCH" != "x86_64" ]]; then variant="cpu"
    elif $HW_NVIDIA && $HW_NVIDIA_TOOLKIT; then variant="cuda"
    else variant="openvino"  # Intel's engine; runs on the CPU too, so any x86 PC can switch later
    fi
  fi
  files="$base:docker-compose.ai.yml"
  if [[ "$variant" == "openvino" ]] && $HW_DRI; then
    files="$files:docker-compose.ai.intel.yml"
    env_set RENDER_GID "${HW_RENDER_GID:-109}"
  fi
  [[ "$variant" == "cuda" ]] && files="$files:docker-compose.ai.nvidia.yml"
  env_set COMPOSE_FILE "$files"
  env_set AI_WORKER_VARIANT "$variant"
  if [[ "$variant" == "cuda" ]]; then env_set AI_WORKER_BASE_IMAGE "nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04"
  else env_unset AI_WORKER_BASE_IMAGE; fi
  local cores ram
  cores="$(nproc 2>/dev/null || echo 2)"
  ram="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
  [[ -n "$(env_get AI_CPUS)" ]] || env_set AI_CPUS "$(( cores > 2 ? cores / 2 : 1 ))"
  [[ -n "$(env_get AI_MEM_LIMIT)" ]] || env_set AI_MEM_LIMIT "$([[ "$ram" -ge 8000 ]] && echo 3g || echo 1536m)"
  AI_VARIANT_CHOSEN="$variant"
}

# write_hardware_report - what the scripts saw, for the AI engine card to
# explain problems ("an Intel GPU is present but its driver isn't loaded").
# Needs the backend running (written into its data volume).
write_hardware_report() {
  local dk=("${DOCKER[@]:-docker}") gpus_json="" line
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    line="${line//\/}"; line="${line//\"/}"
    gpus_json="${gpus_json:+$gpus_json,}\"$line\""
  done <<< "$HW_GPUS"
  printf '{"detected_at":"%s","arch":"%s","virt":"%s","gpus":[%s],"intel_gpu":%s,"dri":%s,"nvidia_gpu":%s,"nvidia_toolkit":%s,"ai_worker":"%s","variant":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$HW_ARCH" "${HW_VIRT:-none}" "$gpus_json" "$HW_INTEL" "$HW_DRI" \
    "$HW_NVIDIA" "$HW_NVIDIA_TOOLKIT" "$([[ "$(env_get AI_WORKER)" == "off" ]] && echo off || echo on)" \
    "$(env_get AI_WORKER_VARIANT)" \
    | "${dk[@]}" compose exec -T backend sh -c 'cat > /data/hardware.json' 2>/dev/null || true
}
