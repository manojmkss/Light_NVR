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
env_get() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  grep -E "^${key}=" "$ENV_FILE" | tail -1 | cut -d= -f2-
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
