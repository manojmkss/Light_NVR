#!/usr/bin/env bash
# LightNVR - one-command updater.
#
# Pulls the latest code from GitHub and rebuilds the stack, preserving all
# your local configuration. This works cleanly precisely because every
# machine-specific setting lives in the gitignored .env (written by
# install-linux.sh), never in the tracked docker-compose.yml - so the pull is
# always a plain fast-forward with nothing to merge.
#
# Usage:
#   ./scripts/update-linux.sh [--yes] [--dry-run]
#
#   -y, --yes    Don't pause for confirmation before pulling/rebuilding.
#   --dry-run    Show what would happen; change nothing.
#   -h, --help   Show this help.
#
# Docker itself is run without sudo here (assumes your user is in the docker
# group, which install-linux.sh arranges). If you get a permission error on
# the docker socket, either re-login so the group applies, or run with sudo.

set -euo pipefail

ASSUME_YES=false
DRY_RUN=false

usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; }

for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=true ;;
    --dry-run) DRY_RUN=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; usage; exit 1 ;;
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
[[ -f docker-compose.yml ]] || die "docker-compose.yml not found in $REPO_ROOT - run this from inside the cloned repo."

ENV_FILE="$REPO_ROOT/.env"
# shellcheck source=lib-env.sh
source "$SCRIPT_DIR/lib-env.sh"

command -v git >/dev/null 2>&1 || die "git is not installed."
git rev-parse --git-dir >/dev/null 2>&1 || die "This isn't a git checkout - update only works on a 'git clone' install, not a tarball download."

# ---------------------------------------------------------------------------
# 1. Make sure the working tree is clean enough to fast-forward
# ---------------------------------------------------------------------------
# The only tracked file older installs were ever told to edit is
# docker-compose.yml (the storage path). If that's the situation, migrate it
# onto the .env scheme automatically so the pull can go through - that's a
# one-time thing for boxes set up before the .env-driven layout.

old_primary="$(detect_handedited_primary_path "$REPO_ROOT")"
if [[ -n "$old_primary" ]]; then
  log "Your docker-compose.yml was hand-edited to store recordings at: $old_primary"
  log "Moving that into .env so updates stay clean (one-time migration)."
  if ! $DRY_RUN; then
    env_set PRIMARY_STORAGE_PATH "$old_primary"
    git checkout -- docker-compose.yml
    log "Restored docker-compose.yml to the tracked version; path preserved in .env."
  fi
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  warn "You have local changes to tracked files that would block a clean update:"
  git status --short | grep -vE '^\?\?' | sed 's/^/    /'
  die "Resolve or revert them (git checkout -- <file>), then re-run. Your .env is gitignored and safe."
fi

# ---------------------------------------------------------------------------
# 2. Pull
# ---------------------------------------------------------------------------

branch="$(git rev-parse --abbrev-ref HEAD)"
log "Fetching latest on '$branch'..."
run git fetch origin "$branch"

behind="$(git rev-list --count "HEAD..origin/$branch" 2>/dev/null || echo 0)"
if [[ "$behind" == "0" ]] && ! $DRY_RUN; then
  log "Already up to date. Rebuilding anyway to pick up any local image changes."
else
  log "$behind new commit(s) to apply."
  confirm "Pull and rebuild now?" || die "Aborted."
  run git merge --ff-only "origin/$branch" \
    || die "Can't fast-forward (local history diverged). Sort it out manually, then re-run."
fi

# ---------------------------------------------------------------------------
# 3. Warn about any newly-required env vars the pull introduced
# ---------------------------------------------------------------------------
# Compose files use ${VAR:?...} for anything mandatory. If a pull adds a new
# one that isn't in .env yet, `docker compose` would fail - surface it clearly
# rather than as a raw interpolation error. Only the ACTIVE compose files are
# scanned (per COMPOSE_FILE in .env), so an override a user hasn't enabled -
# e.g. the macvlan file - doesn't produce false warnings about vars it needs.

active_files="$(env_get COMPOSE_FILE)"
[[ -n "$active_files" ]] || active_files="docker-compose.yml"
IFS=':' read -r -a _files <<< "$active_files"

missing=""
while IFS= read -r var; do
  [[ -n "$var" ]] || continue
  if [[ -z "$(env_get "$var")" ]] && [[ -z "${!var:-}" ]]; then
    missing="$missing $var"
  fi
done < <(for f in "${_files[@]}"; do [[ -f "$f" ]] && cat "$f"; done \
         | grep -ohE '\$\{[A-Z_]+:\?' | sed -E 's/.*\$\{([A-Z_]+):\?.*/\1/' | sort -u)

if [[ -n "$missing" ]]; then
  warn "This version needs new .env value(s):$missing"
  warn "Set them (see .env.example) or re-run ./scripts/install-linux.sh, then update again."
fi

# ---------------------------------------------------------------------------
# 4. Rebuild + restart  (.env's COMPOSE_FILE, if set, layers in overrides)
# ---------------------------------------------------------------------------

# Pull the updated prebuilt images (matching the code just pulled); fall back
# to building from source if they aren't available.
log "Fetching updated images and restarting..."
if $DRY_RUN; then
  run docker compose pull
  run docker compose up -d
elif docker compose pull; then
  docker compose up -d
else
  warn "Prebuilt images not available - building from source instead (slower)."
  docker compose up -d --build
fi

if ! $DRY_RUN; then
  log "Waiting for the backend to become healthy..."
  ready=false
  for _ in $(seq 1 40); do
    if curl -fsk https://localhost:8443/api/health >/dev/null 2>&1; then ready=true; break; fi
    sleep 3
  done
  $ready && log "Update complete - backend healthy." \
         || warn "Health check didn't pass within 2 minutes - check 'docker compose logs backend'."
fi

echo
log "Tip: browsers cache the app (PWA). If the UI looks unchanged after an"
log "update, hard-refresh once with Ctrl+Shift+R."
