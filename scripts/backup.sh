#!/usr/bin/env bash
set -euo pipefail

: "${RESTIC_REPOSITORY:?set RESTIC_REPOSITORY}"
: "${RESTIC_PASSWORD_FILE:?set RESTIC_PASSWORD_FILE}"

platform_root="${AI_PLATFORM_ROOT:-/mnt/Storage1/ai-platform}"
stack_root="${STACK_ROOT:-/opt/ai-platform}"
temp_root="$(mktemp -d)"

cleanup() {
  if [[ -n "${temp_root:-}" && "$temp_root" == /tmp/* ]]; then
    rm -rf -- "$temp_root"
  fi
}
trap cleanup EXIT

chmod 0700 "$temp_root"
docker exec ai-platform-switch-postgres-1 pg_dumpall \
  -U "${SWITCH_DB_OWNER_USER:-postgres}" | gzip -9 > "$temp_root/switch.sql.gz"

restic backup \
  "$stack_root" \
  "$platform_root/switch" \
  "$platform_root/openhands/state" \
  "$platform_root/open-webui" \
  "$platform_root/n8n" \
  "$temp_root/switch.sql.gz" \
  --exclude "$platform_root/workspaces" \
  --tag ai-platform

restic check --read-data-subset=5%

