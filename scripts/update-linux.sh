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
# Docker is run directly when your user can reach the Docker socket (i.e. it's
# in the docker group, which install-linux.sh arranges) and automatically via
# sudo when it can't - so this works whether or not that group membership has
# been set up or applied to your current login.

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
SELF="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"
[[ -f docker-compose.yml ]] || die "docker-compose.yml not found in $REPO_ROOT - run this from inside the cloned repo."

ENV_FILE="$REPO_ROOT/.env"
# shellcheck source=lib-env.sh
source "$SCRIPT_DIR/lib-env.sh"

command -v git >/dev/null 2>&1 || die "git is not installed."
git rev-parse --git-dir >/dev/null 2>&1 || die "This isn't a git checkout - update only works on a 'git clone' install, not a tarball download."

# ---------------------------------------------------------------------------
# How to invoke Docker
# ---------------------------------------------------------------------------
# Talking to the Docker socket needs either membership of the `docker` group or
# root. Rather than assume the former and fail on the socket - which surfaces as
# a confusing permission error part-way through an update - work out which one
# applies now and use it for every Docker call below.

command -v docker >/dev/null 2>&1 || die "Docker is not installed. See docs/linux-production-install.md."

DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  command -v sudo >/dev/null 2>&1 || die \
    "Can't reach the Docker daemon, and sudo isn't available. Run this as root, or add your user to the 'docker' group: usermod -aG docker $USER (then log out and back in)."
  DOCKER=(sudo docker)
  if ! sudo -n true 2>/dev/null; then
    log "Your user can't reach the Docker socket, so Docker will run via sudo (you may be prompted for your password)."
    log "To avoid the prompt next time: sudo usermod -aG docker $USER   (then log out and back in)"
  fi
  "${DOCKER[@]}" info >/dev/null 2>&1 || die \
    "Still can't reach the Docker daemon, even with sudo. Is it running? Try: sudo systemctl start docker"
fi

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
# --tags: without it a checkout never learns the release tags, can't tell it
# is on a release, and falls back to building every image from source.
run git fetch --tags origin "$branch"

head_before="$(git rev-parse HEAD)"
behind="$(git rev-list --count "HEAD..origin/$branch" 2>/dev/null || echo 0)"
if [[ "$behind" == "0" ]] && ! $DRY_RUN; then
  log "Already up to date. Rebuilding anyway to pick up any local image changes."
else
  log "$behind new commit(s) to apply."
  confirm "Pull and rebuild now?" || die "Aborted."
  run git merge --ff-only "origin/$branch" \
    || die "Can't fast-forward (local history diverged). Sort it out manually, then re-run."
fi

# If the pull updated any of the update machinery, restart with the new copy
# before doing anything else.
#
# Watching the whole scripts/ directory, not just this file, is deliberate:
# lib-env.sh is *sourced*, so its functions are already resident in memory by
# the time the pull lands. A fix shipped in there would sit on disk unused for
# the rest of the run - which is precisely how a broken env_get kept aborting
# the update immediately after the merge, on the very run that pulled its own
# fix. Bash also reads a script lazily, so continuing after this file changed
# underneath us risks executing a mangled mix of old and new lines.
#
# Re-exec'ing keeps `update-linux.sh` a genuine one-command update - nobody
# should have to run it twice, or `git pull` first, to get a working updater.
SCRIPTS_REL="${SCRIPT_DIR#"$REPO_ROOT"/}"
if [[ -z "${LIGHTNVR_UPDATE_REEXEC:-}" ]] && ! $DRY_RUN \
   && ! git diff --quiet "$head_before" HEAD -- "$SCRIPTS_REL" 2>/dev/null; then
  log "The update scripts changed in this pull - restarting with the new versions..."
  export LIGHTNVR_UPDATE_REEXEC=1
  exec bash "$SELF" "$@"
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
# 3b. AI service for this machine's hardware (a GPU added since the last run
#     is connected here; the web page then switches to it)
# ---------------------------------------------------------------------------
if $DRY_RUN; then
  log "[DRY RUN] Would detect GPUs and set the AI service's image/devices in .env."
  log "[DRY RUN] Would add the describer (Ollama) if this machine has >= 4 cores and 8 GB RAM."
else
  configure_ai_service
  if [[ -n "${AI_VARIANT_CHOSEN:-}" ]]; then
    gpu_note="no GPU"
    $HW_DRI && gpu_note="Intel GPU connected"
    [[ "$AI_VARIANT_CHOSEN" == "cuda" ]] && gpu_note="NVIDIA GPU connected"
    if $HW_INTEL && ! $HW_DRI; then gpu_note="Intel GPU present but its driver isn't loaded - see Settings -> AI -> AI engine"; fi
    if $HW_NVIDIA && ! $HW_NVIDIA_TOOLKIT; then gpu_note="NVIDIA GPU present but the NVIDIA Container Toolkit is missing"; fi
    log "AI service: ${AI_VARIANT_CHOSEN} image ($gpu_note). Choose CPU/GPU in Settings -> AI -> AI engine."
  fi
  configure_describer
  case "$DESCRIBER_STATE" in
    on) log "Descriptions: can run on this machine (set them up in Settings -> AI -> Describe what's happening)." ;;
    too_small) log "Descriptions: not added here ($DESCRIBER_WHY) - use another PC with Ollama instead." ;;
  esac
fi

# ---------------------------------------------------------------------------
# 4. Rebuild + restart  (.env's COMPOSE_FILE, if set, layers in overrides)
# ---------------------------------------------------------------------------

# Get the running containers onto the code we just checked out - whichever way
# that has to happen. Prebuilt images are only ever published for `v*` release
# tags, so an image can match this checkout only when HEAD *is* such a tag.
# Blindly pulling would otherwise "succeed" against a stale `:latest` and
# silently restart the OLD build over the new source, with no error to notice.
pinned="$(env_get LIGHTNVR_VERSION)"
tag_at_head="$(git tag --points-at HEAD 2>/dev/null | grep -E '^v[0-9]' | sort -V | tail -1 || true)"

if [[ -n "$pinned" ]]; then
  # Explicitly pinned in .env - the user is asking for that exact version.
  log "Fetching prebuilt images for pinned version $pinned..."
  run "${DOCKER[@]}" compose pull
  run "${DOCKER[@]}" compose up -d
elif [[ -n "$tag_at_head" ]]; then
  # On a release commit: pull that version by name rather than the moving
  # `latest`, so what starts is exactly what this checkout says it is.
  export LIGHTNVR_VERSION="${tag_at_head#v}"
  log "Now at release $tag_at_head - fetching its prebuilt images..."
  if $DRY_RUN; then
    run "${DOCKER[@]}" compose pull
    run "${DOCKER[@]}" compose up -d
  elif "${DOCKER[@]}" compose pull; then
    "${DOCKER[@]}" compose up -d
  else
    warn "Prebuilt images for $tag_at_head aren't published (yet) - building from source instead."
    "${DOCKER[@]}" compose up -d --build
  fi
else
  # Newer commits than the last release: no published image contains this code,
  # so building is the only way to actually run what was just pulled.
  latest_tag="$(git describe --tags --abbrev=0 2>/dev/null || echo 'the last release')"
  log "This checkout has changes newer than $latest_tag, so no prebuilt image matches it."
  log "Building from source (slower, but it's the code you just pulled)..."
  run "${DOCKER[@]}" compose up -d --build
fi

if ! $DRY_RUN; then
  log "Waiting for the backend to become healthy..."
  ready=false
  for _ in $(seq 1 40); do
    if curl -fsk https://localhost:8443/api/health >/dev/null 2>&1; then ready=true; break; fi
    sleep 3
  done
  $ready && log "Update complete - backend healthy." \
         || warn "Health check didn't pass within 2 minutes - check '${DOCKER[*]} compose logs backend'."
  if $ready; then write_hardware_report; fi
fi

echo
log "Tip: browsers cache the app (PWA). If the UI looks unchanged after an"
log "update, reload the page once - the app now activates updates immediately."
