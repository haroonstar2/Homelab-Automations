#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "--apply" ]]; then
  echo "Usage: sudo $0 --apply [source] [destination]"
  echo "Copies and verifies the active legacy Ollama store without modifying it."
  exit 2
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 1
fi

source_store="${2:-/mnt/Storage1/data/ollama}"
destination_store="${3:-/mnt/Storage1/ai-platform/ollama}"

if [[ ! -d "$source_store/models" ]]; then
  echo "Source does not look like an Ollama store: $source_store" >&2
  exit 1
fi

if [[ "$source_store" == "$destination_store" ]]; then
  echo "Source and destination must differ." >&2
  exit 1
fi

case "$destination_store" in
  /mnt/Storage1/ai-platform/ollama) ;;
  *)
    echo "Refusing destination outside the dedicated platform Ollama path." >&2
    exit 1
    ;;
esac

command -v rsync >/dev/null 2>&1 || {
  echo "rsync is required." >&2
  exit 1
}

install -d -m 0750 "$destination_store"
rsync --archive --hard-links --numeric-ids --info=stats2 \
  "$source_store/" "$destination_store/"

verification="$(mktemp)"
trap 'rm -f "$verification"' EXIT
rsync --archive --hard-links --numeric-ids --checksum --dry-run --itemize-changes \
  "$source_store/" "$destination_store/" >"$verification"

if [[ -s "$verification" ]]; then
  echo "Verification failed; source and destination differ:" >&2
  cat "$verification" >&2
  exit 1
fi

date -u +%FT%TZ >"$destination_store/.seeded-from-legacy"
chmod 0640 "$destination_store/.seeded-from-legacy"
echo "Verified Ollama store copy: $source_store -> $destination_store"
