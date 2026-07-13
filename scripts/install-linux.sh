#!/usr/bin/env bash
# LightNVR - automated Linux install / (re)configure.
#
# Installs Docker if missing, then writes all machine-specific configuration
# to a gitignored .env file - a dedicated storage disk and (optionally) the
# network settings that make ONVIF "Scan network" work - so the tracked
# docker-compose.yml never needs hand-editing and `git pull` stays a clean
# fast-forward on every upgrade (see scripts/update-linux.sh). Re-runnable at
# any time to reconfigure.
#
# Usage:
#   sudo ./scripts/install-linux.sh [options]
#
#   -y, --yes               Accept detected/default answers, no prompts.
#   --storage-path=PATH     Put recordings on this host path (a mounted disk).
#   --enable-scan           Configure LAN discovery (macvlan) for ONVIF scan.
#   --scan-ip=IP            IP for the scan interface (else auto-picked+pinged).
#   --skip-firewall         Never touch firewall rules.
#   --dry-run               Print what would happen; change nothing.
#   -h, --help              Show this help.
#
# Remote access (Tailscale / Cloudflare Tunnel) is deliberately NOT handled
# here - it's a post-install step in the app itself (Settings -> Remote
# Access). This script's job ends at "the app is running and reachable."

set -euo pipefail

ASSUME_YES=false
SKIP_FIREWALL=false
DRY_RUN=false
ENABLE_SCAN=false
STORAGE_PATH=""
SCAN_IP=""

usage() { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; }

for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=true ;;
    --enable-scan) ENABLE_SCAN=true ;;
    --skip-firewall) SKIP_FIREWALL=true ;;
    --dry-run) DRY_RUN=true ;;
    --storage-path=*) STORAGE_PATH="${arg#*=}" ;;
    --scan-ip=*) SCAN_IP="${arg#*=}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; usage; exit 1 ;;
  esac
done

log()  { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$1" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$1" >&2; exit 1; }

confirm() {
  local prompt="$1"
  $ASSUME_YES && return 0
  read -r -p "$prompt [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

ask() {
  # ask PROMPT DEFAULT - echo the user's answer, or DEFAULT if they just hit
  # enter (or -y is in effect).
  local prompt="$1" default="$2" reply
  if $ASSUME_YES; then echo "$default"; return; fi
  read -r -p "$prompt [$default] " reply
  echo "${reply:-$default}"
}

run() {
  if $DRY_RUN; then printf '\033[2m[DRY RUN] %s\033[0m\n' "$*"; else "$@"; fi
}

# ---------------------------------------------------------------------------
# 0. Preconditions
# ---------------------------------------------------------------------------

[[ $EUID -eq 0 ]] || die "Run this with sudo: sudo $0 $*"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"
[[ -f docker-compose.yml ]] || die "docker-compose.yml not found in $REPO_ROOT - run this from inside the cloned repo."

ENV_FILE="$REPO_ROOT/.env"
# shellcheck source=lib-env.sh
source "$SCRIPT_DIR/lib-env.sh"

ORIGINAL_USER="${SUDO_USER:-}"
VOLUME_NAME="lightnvr-data"

log "Installing/configuring LightNVR from $REPO_ROOT"
$DRY_RUN && warn "Dry run - no changes will actually be made."

# ---------------------------------------------------------------------------
# 1. Docker
# ---------------------------------------------------------------------------

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  log "Docker + Compose plugin already installed - skipping."
else
  log "Docker Engine (with the Compose plugin) is not installed."
  if confirm "Install it now via Docker's official convenience script (get.docker.com)?"; then
    run bash -c "curl -fsSL https://get.docker.com | sh"
  else
    die "Docker is required. Install it yourself, then re-run this script."
  fi
fi

if [[ -n "$ORIGINAL_USER" ]] && ! id -nG "$ORIGINAL_USER" | grep -qw docker; then
  log "Adding $ORIGINAL_USER to the docker group (lets you run docker without sudo later)."
  run usermod -aG docker "$ORIGINAL_USER"
  warn "$ORIGINAL_USER needs to log out and back in for that group change to take effect."
fi

run systemctl enable docker
run systemctl start docker

# ---------------------------------------------------------------------------
# 2. Migrate a hand-edited docker-compose.yml onto the .env scheme
# ---------------------------------------------------------------------------
# Older installs were documented to hand-edit the storage line in the tracked
# docker-compose.yml. That breaks `git pull`. If we detect such an edit, lift
# the custom path into .env and restore the tracked file so future updates are
# clean. Only ever touches the storage bind-mount line; anything else that
# differs is left for the user to resolve.

if ! $DRY_RUN; then
  old_primary="$(detect_handedited_primary_path "$REPO_ROOT")"
  if [[ -n "$old_primary" ]]; then
    log "Found a hand-edited storage path in docker-compose.yml: $old_primary"
    if confirm "Move it into .env (PRIMARY_STORAGE_PATH) and restore the tracked file so 'git pull' stays clean?"; then
      [[ -z "$STORAGE_PATH" ]] && STORAGE_PATH="$old_primary"
      git -C "$REPO_ROOT" checkout -- docker-compose.yml
      log "Restored docker-compose.yml to the tracked version."
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 3. Storage location
# ---------------------------------------------------------------------------
# Recordings go to PRIMARY_STORAGE_PATH on the host (default: an in-repo
# folder). Pointing it at a dedicated disk is just an .env value now.

current_storage="$(env_get PRIMARY_STORAGE_PATH)"

if [[ -z "$STORAGE_PATH" ]] && ! $ASSUME_YES; then
  echo
  log "Storage: where should recordings be written on this host?"
  echo "  Detected disks/mounts:"
  lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT 2>/dev/null | sed 's/^/    /' || true
  echo "  Enter a path on a dedicated disk (e.g. /mnt/nvr-storage), or leave"
  echo "  blank to keep recordings in the default in-repo folder."
  STORAGE_PATH="$(ask "  Storage path" "${current_storage}")"
fi

if [[ -n "$STORAGE_PATH" ]]; then
  if [[ ! -d "$STORAGE_PATH" ]]; then
    warn "$STORAGE_PATH does not exist yet."
    confirm "Create it?" && run mkdir -p "$STORAGE_PATH"
  fi
  if [[ -d "$STORAGE_PATH" ]] && ! is_real_mountpoint "$STORAGE_PATH"; then
    warn "$STORAGE_PATH is NOT a separate mounted disk - it's a folder on the OS disk."
    warn "If you intended a dedicated drive, mount it there first (see docs §6),"
    warn "otherwise recordings will fill your OS disk. Continuing anyway."
  fi
  run env_set PRIMARY_STORAGE_PATH "$STORAGE_PATH"
  log "Recordings will be stored at: $STORAGE_PATH"
else
  log "Storage: using the default in-repo folder (./primary-storage)."
fi

# Ensure whatever the base compose file might fall back to still exists.
log "Preparing data directories."
for dir in storage primary-storage backup-storage certs; do
  run mkdir -p "$REPO_ROOT/$dir"
done

# ---------------------------------------------------------------------------
# 4. Migrate an old bind-mounted ./data into the named volume, if present
# ---------------------------------------------------------------------------

OLD_DATA_DB="$REPO_ROOT/data/lightnvr.db"
if [[ -f "$OLD_DATA_DB" ]] && ! $DRY_RUN; then
  volume_has_data=false
  if docker volume inspect "$VOLUME_NAME" >/dev/null 2>&1 \
     && docker run --rm -v "${VOLUME_NAME}:/check" alpine sh -c "[ -f /check/lightnvr.db ]" >/dev/null 2>&1; then
    volume_has_data=true
  fi
  if $volume_has_data; then
    log "Named volume '$VOLUME_NAME' already has a database - not touching the old ./data/ folder."
  elif confirm "Found an existing database at ./data/lightnvr.db (pre-upgrade layout). Migrate it into the named volume?"; then
    docker volume create "$VOLUME_NAME" >/dev/null
    docker run --rm -v "$REPO_ROOT/data:/from:ro" -v "${VOLUME_NAME}:/to" alpine sh -c "cp -a /from/. /to/"
    log "Migrated. ./data/ is left as a safety copy - remove it once you've confirmed everything works."
  fi
fi

# ---------------------------------------------------------------------------
# 5. Network scan (optional) - macvlan via .env + COMPOSE_FILE
# ---------------------------------------------------------------------------
# Gives the backend a second, LAN-facing interface so ONVIF WS-Discovery
# multicast reaches cameras. All auto-detected; the user just confirms.

if ! $ENABLE_SCAN && ! $ASSUME_YES; then
  echo
  echo "  ONVIF \"Scan network\" needs the backend on your LAN directly (macvlan)."
  echo "  Skip this and you can still add cameras by IP (fully auto-detected)."
  echo "  Requires a WIRED connection; on a VM the host NIC may need promiscuous"
  echo "  mode allowed (see docs §5)."
  confirm "  Enable automatic camera scanning?" && ENABLE_SCAN=true
fi

if $ENABLE_SCAN; then
  iface="$(detect_default_iface)"
  gateway="$(detect_gateway)"
  subnet="$(detect_subnet "$iface")"
  [[ -n "$iface" && -n "$gateway" && -n "$subnet" ]] \
    || die "Could not auto-detect network (iface/gateway/subnet). Set them manually in .env - see .env.example."

  if [[ -z "$SCAN_IP" ]]; then
    log "Looking for a free IP on your LAN for the scan interface..."
    SCAN_IP="$(find_free_ip "$gateway" || true)"
    [[ -n "$SCAN_IP" ]] || die "Couldn't auto-find a free IP. Re-run with --scan-ip=<free-ip-on-your-LAN>."
  fi

  log "Network scan config: iface=$iface subnet=$subnet gateway=$gateway backend-ip=$SCAN_IP"
  if $DRY_RUN; then
    log "[DRY RUN] Would write LAN_SCAN_* and COMPOSE_FILE to .env."
  else
    env_set LAN_SCAN_INTERFACE "$iface"
    env_set LAN_SCAN_SUBNET "$subnet"
    env_set LAN_SCAN_GATEWAY "$gateway"
    env_set LAN_SCAN_BACKEND_IP "$SCAN_IP"
    # COMPOSE_FILE makes plain `docker compose up` load the macvlan override
    # transparently. ':' is the separator on Linux.
    env_set COMPOSE_FILE "docker-compose.yml:docker-compose.macvlan.yml"
  fi
  log "Camera scanning enabled. If Scan still finds nothing on a VM, check the"
  log "hypervisor's promiscuous-mode setting for this VM's NIC (docs §5)."
fi

# ---------------------------------------------------------------------------
# 6. Firewall (LAN-only - never opens anything to the internet)
# ---------------------------------------------------------------------------

if $SKIP_FIREWALL; then
  log "Skipping firewall setup (--skip-firewall)."
elif command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  if confirm "ufw is active. Add allow rules for ports 8080/8443 (web UI)?"; then
    run ufw allow 8080/tcp
    run ufw allow 8443/tcp
  fi
else
  log "ufw not installed or not active - leaving firewall untouched (see docs §5)."
fi

# ---------------------------------------------------------------------------
# 7. Bring the stack up  (plain `docker compose` - .env's COMPOSE_FILE, if set,
#    pulls in the macvlan override automatically, so no -f juggling here)
# ---------------------------------------------------------------------------

log "Building and starting LightNVR (first run can take a few minutes)."
run docker compose up -d --build

# ---------------------------------------------------------------------------
# 8. Wait for health
# ---------------------------------------------------------------------------

if ! $DRY_RUN; then
  log "Waiting for the backend to become healthy..."
  ready=false
  for _ in $(seq 1 40); do
    if curl -fsk https://localhost:8443/api/health >/dev/null 2>&1; then ready=true; break; fi
    sleep 3
  done
  $ready || warn "Health check didn't succeed within 2 minutes - check 'docker compose logs backend'."
fi

# ---------------------------------------------------------------------------
# 9. Summary
# ---------------------------------------------------------------------------

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[[ -z "$LAN_IP" ]] && LAN_IP="<this-machine's-LAN-IP>"

echo
log "Done."
echo "  Open:  https://${LAN_IP}:8443"
echo "  Accept the self-signed-cert warning on first visit, then the setup wizard"
echo "  creates your admin account and walks through storage/cameras."
echo
echo "  All machine-specific config is in .env (gitignored). The tracked"
echo "  docker-compose.yml is untouched, so upgrades are clean:"
echo "      ./scripts/update-linux.sh"
echo
[[ -n "$STORAGE_PATH" ]] && echo "  Recordings: $STORAGE_PATH"
$ENABLE_SCAN && echo "  Camera scanning: enabled (backend LAN IP $SCAN_IP)"
echo "  Remote access away from home: Settings -> Remote Access (Tailscale / Cloudflare)."
echo "  Full reference: docs/linux-production-install.md"
