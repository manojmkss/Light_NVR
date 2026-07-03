#!/usr/bin/env bash
# Regenerates github-upload/ - a clean, gitignored export of exactly what's
# committed at HEAD (via `git archive`), suitable for a fresh `git init` +
# push, or for a plain drag-and-drop upload through GitHub's web UI. Never
# includes uncommitted changes, untracked files, or anything gitignored
# (runtime data, .env, certs, local tool config) - it can only contain what
# git itself already tracks.
#
# Usage: ./scripts/make-github-upload.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_DIR="$REPO_ROOT/github-upload"

cd "$REPO_ROOT"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Note: there are uncommitted changes - they will NOT be included below." >&2
  echo "Commit first if they should be part of the export." >&2
  echo >&2
fi

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
git archive HEAD | tar -x -C "$OUT_DIR"

echo "Exported $(git rev-parse --short HEAD) ($(git ls-files | wc -l | tr -d ' ') files) to $OUT_DIR"
