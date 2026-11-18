#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: sudo $0 --apply --env-file /path/to/platform.env"
  echo "Optionally set OPENCODE_ARCHIVE to a previously downloaded release archive."
}

if [[ "${1:-}" != "--apply" || "${2:-}" != "--env-file" || -z "${3:-}" || -n "${4:-}" ]]; then
  usage
  exit 2
fi

if [[ "$EUID" -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 1
fi

agent_user="ai-agent"
env_file="$3"
version="2.2.7"
archive_sha256="89a6cee227b54bc011179c528a5e63176032462b1a5e21723b616801cddc2ca7"
archive_url="https://github.com/anomalyco/opencode/releases/download/v${version}/opencode-linux-x64.tar.gz"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
agent_home="$(getent passwd "$agent_user" | cut -d: -f6)"
agent_group="$(id -gn "$agent_user")"

test -n "$agent_home" || {
  echo "Dedicated agent identity is missing: $agent_user" >&2
  exit 1
}
test -r "$env_file" || {
  echo "Environment file is not readable: $env_file" >&2
  exit 1
}
test -r "$repo_root/config/opencode/opencode.jsonc" || {
  echo "OpenCode configuration is missing from the repository." >&2
  exit 1
}

work="$(mktemp -d)"
secret_file="$(mktemp)"
trap 'rm -rf "$work" "$secret_file"' EXIT

archive="${OPENCODE_ARCHIVE:-$work/opencode-linux-x64.tar.gz}"
if [[ -z "${OPENCODE_ARCHIVE:-}" ]]; then
  curl --fail --location --retry 3 --output "$archive" "$archive_url"
fi

echo "$archive_sha256  $archive" | sha256sum --check --status || {
  echo "OpenCode archive checksum verification failed." >&2
  exit 1
}

tar -xzf "$archive" -C "$work"
test -x "$work/opencode" || chmod 0755 "$work/opencode"
reported_version="$($work/opencode --version)"
[[ "$reported_version" == "opencode v${version}" ]] || {
  echo "Unexpected OpenCode version: $reported_version" >&2
  exit 1
}

litellm_key="$(sed -n 's/^LITELLM_MASTER_KEY=//p' "$env_file")"
[[ "$litellm_key" == sk-* ]] || {
  echo "LITELLM_MASTER_KEY is missing or malformed." >&2
  exit 1
}
umask 077
printf 'LITELLM_API_KEY=%s\n' "$litellm_key" >"$secret_file"
unset litellm_key

install -d -m 0700 -o "$agent_user" -g "$agent_group" \
  "$agent_home/.local/bin" \
  "$agent_home/.config/opencode" \
  "$agent_home/.config/ai-platform"
install -m 0755 -o "$agent_user" -g "$agent_group" \
  "$work/opencode" "$agent_home/.local/bin/opencode"
install -m 0600 -o "$agent_user" -g "$agent_group" \
  "$repo_root/config/opencode/opencode.jsonc" \
  "$agent_home/.config/opencode/opencode.jsonc"
install -m 0600 -o "$agent_user" -g "$agent_group" \
  "$secret_file" "$agent_home/.config/ai-platform/litellm.env"

echo "Installed $reported_version for $agent_user from a verified release archive."

