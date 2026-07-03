#!/usr/bin/env bash
# LightNVR - automated Linux install.
#
# Installs Docker if it's missing, prepares the bind-mount directories,
# optionally opens LAN-only firewall rules, enables Docker on boot, and
# brings the stack up - the same steps documented by hand in
# docs/linux-production-install.md, scripted end to end.
#
# Usage:
#   sudo ./scripts/install-linux.sh [-y|--yes] [--skip-firewall] [--dry-run]
#
#   -y, --yes         Don't prompt before system-level changes (Docker
#                      install, firewall rules, enabling the docker service).
#   --skip-firewall    Never touch firewall rules, even if ufw is active.
#   --dry-run          Print what would happen; make no changes at all.
#   -h, --help          Show this help.
#
# Remote access (Tailscale / Cloudflare Tunnel) is deliberately NOT handled
# here - it's a post-install step in the app itself (Settings -> Remote
# Access), started as a subprocess the moment you enable it there. This
# script's job ends at "the app is running and reachable on the LAN."

set -euo pipefail

ASSUME_YES=false
SKIP_FIREWALL=false
DRY_RUN=false

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=true ;;
    --skip-firewall) SKIP_FIREWALL=true ;;
    --dry-run) DRY_RUN=true ;;
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

run() {
  # Executes (or, in dry-run mode, just prints) a privileged/state-changing step.
  if $DRY_RUN; then
    printf '\033[2m[DRY RUN] %s\033[0m\n' "$*"
  else
    "$@"
  fi
}

# ---------------------------------------------------------------------------
# 0. Preconditions
# ---------------------------------------------------------------------------

if [[ $EUID -ne 0 ]]; then
  die "Run this with sudo: sudo $0 $*"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -f docker-compose.yml ]]; then
  die "docker-compose.yml not found in $REPO_ROOT - run this from inside the cloned repo."
fi

ORIGINAL_USER="${SUDO_USER:-}"

log "Installing LightNVR from $REPO_ROOT"
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
# 2. Bind-mount directories
# ---------------------------------------------------------------------------
# Note: /data (the SQLite DB + Tailscale state) is a named Docker volume, not
# a bind-mount directory - see docker-compose.yml for why (WAL-mode locking
# is unreliable across Docker Desktop's Windows<->Linux file-sharing layer;
# Linux with the native driver doesn't strictly need this, but keeping one
# scheme for both platforms means an install is portable between them).
# Nothing to create here for it; Docker creates the volume itself.

log "Preparing data directories."
for dir in storage primary-storage backup-storage certs; do
  run mkdir -p "$REPO_ROOT/$dir"
done

# ---------------------------------------------------------------------------
# 3. Migrate an old bind-mounted ./data into the named volume, if present
# ---------------------------------------------------------------------------
# Handles upgrading an existing install that predates the named-volume
# change: if there's a database sitting in the old ./data/ bind-mount
# location and the named volume is missing or empty, offer to copy it
# forward so accounts/cameras survive the upgrade instead of silently
# starting fresh against an empty volume. The old ./data/ folder is left
# untouched either way - this only ever copies forward, never deletes.

VOLUME_NAME="lightnvr-data"
OLD_DATA_DB="$REPO_ROOT/data/lightnvr.db"

if [[ -f "$OLD_DATA_DB" ]] && ! $DRY_RUN; then
  volume_has_data=false
  if docker volume inspect "$VOLUME_NAME" >/dev/null 2>&1; then
    if docker run --rm -v "${VOLUME_NAME}:/check" alpine sh -c "[ -f /check/lightnvr.db ]" >/dev/null 2>&1; then
      volume_has_data=true
    fi
  fi

  if $volume_has_data; then
    log "Named volume '$VOLUME_NAME' already has a database - not touching the old ./data/ folder."
  elif confirm "Found an existing database at ./data/lightnvr.db (pre-upgrade layout). Migrate it into the new named volume before starting?"; then
    log "Migrating ./data/ into the '$VOLUME_NAME' volume..."
    docker volume create "$VOLUME_NAME" >/dev/null
    docker run --rm -v "$REPO_ROOT/data:/from:ro" -v "${VOLUME_NAME}:/to" alpine sh -c "cp -a /from/. /to/"
    log "Migrated. ./data/ is left in place as a safety copy - remove it yourself once you've confirmed everything works."
  else
    warn "Skipping migration - the backend will start with an EMPTY database until you migrate ./data/ manually."
  fi
elif [[ -f "$OLD_DATA_DB" ]] && $DRY_RUN; then
  log "[DRY RUN] Would check whether ./data/lightnvr.db needs migrating into the '$VOLUME_NAME' volume."
fi

# ---------------------------------------------------------------------------
# 4. Firewall (LAN-only - never opens anything to the internet)
# ---------------------------------------------------------------------------

if $SKIP_FIREWALL; then
  log "Skipping firewall setup (--skip-firewall)."
elif command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  if confirm "ufw is active. Add allow rules for ports 8080/8443 (web UI)?"; then
    run ufw allow 8080/tcp
    run ufw allow 8443/tcp
  fi
else
  log "ufw not installed or not active - leaving firewall untouched. See docs/linux-production-install.md (Network and firewall planning) if you use a different firewall."
fi

# ---------------------------------------------------------------------------
# 5. Bring the stack up
# ---------------------------------------------------------------------------

log "Building and starting LightNVR (this can take a few minutes on first run)."
if $DRY_RUN; then
  run docker compose up -d --build
else
  docker compose up -d --build
fi

# ---------------------------------------------------------------------------
# 7. Wait for it to come up
# ---------------------------------------------------------------------------

if ! $DRY_RUN; then
  log "Waiting for the backend to become healthy..."
  ready=false
  for _ in $(seq 1 40); do
    if curl -fsk https://localhost:8443/api/health >/dev/null 2>&1; then
      ready=true
      break
    fi
    sleep 3
  done
  $ready || warn "Health check didn't succeed within 2 minutes - check 'docker compose logs backend'."
fi

# ---------------------------------------------------------------------------
# 8. Summary
# ---------------------------------------------------------------------------

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[[ -z "$LAN_IP" ]] && LAN_IP="<this-machine's-LAN-IP>"

echo
log "Done."
echo "  Open:  https://${LAN_IP}:8443"
echo "  Your browser will warn about the self-signed certificate on first visit - that's expected."
echo "  The setup wizard creates your first admin account and walks through storage/cameras."
echo
echo "  The database lives in the '$VOLUME_NAME' Docker volume, not a plain folder - use the"
echo "  in-app Backup feature (Settings -> Backup) to back it up, not a direct file copy."
echo
echo "  Note: containers run as root inside, so files under storage/, primary-storage/,"
echo "  backup-storage/ will be root-owned on the host - that's expected, not a bug."
echo
echo "  For remote access away from home (no port-forwarding needed), see"
echo "  Settings -> Remote Access in the app once you're logged in (Tailscale or Cloudflare Tunnel)."
echo
echo "  Full reference: docs/linux-production-install.md"
