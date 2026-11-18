#!/usr/bin/env bash
set -euo pipefail

: "${RESTIC_REPOSITORY:?set RESTIC_REPOSITORY}"
: "${RESTIC_PASSWORD_FILE:?set RESTIC_PASSWORD_FILE}"

restore_root="$(mktemp -d)"
cleanup() {
  if [[ -n "${restore_root:-}" && "$restore_root" == /tmp/* ]]; then
    rm -rf -- "$restore_root"
  fi
}
trap cleanup EXIT

chmod 0700 "$restore_root"
restic restore latest --tag ai-platform --target "$restore_root"
dump_path="$(find "$restore_root" -type f -name switch.sql.gz -print -quit)"
test -n "$dump_path" && test -s "$dump_path" || {
  echo "Restored Switch database dump was not found." >&2
  exit 1
}
gzip -t "$dump_path"
echo "Disposable restore verified at $restore_root"

