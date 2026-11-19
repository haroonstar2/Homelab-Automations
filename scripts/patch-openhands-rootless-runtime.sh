#!/usr/bin/env bash
set -euo pipefail

agent_version="1.62.0"
agent_home="${HOME:?HOME must be set}"
patch_file="$agent_home/.local/share/ai-platform/openhands-agent-server-${agent_version}-rootless-acl.patch"
cache_root="$agent_home/.cache/uv/archive-v0"

for required in find grep patch; do
  command -v "$required" >/dev/null 2>&1 || {
    echo "Required command is missing: $required" >&2
    exit 1
  }
done

[[ -r "$patch_file" ]] || {
  echo "OpenHands rootless compatibility patch is missing: $patch_file" >&2
  exit 1
}

patched=0
while IFS= read -r registry; do
  package_root="${registry%/openhands/agent_server/docker_runtime/registry.py}"
  if [[ ! -d "$package_root/openhands_agent_server-${agent_version}.dist-info" ]]; then
    continue
  fi
  if grep -q 'OH_ROOTLESS_RUNTIME_HOST_UID' "$registry"; then
    patched=$((patched + 1))
    continue
  fi
  if ! grep -q 'directory.mkdir(parents=True, mode=0o700, exist_ok=True)' "$registry"; then
    echo "Pinned OpenHands source does not match the reviewed patch context: $registry" >&2
    exit 1
  fi
  patch --batch --forward --directory="$package_root" -p0 <"$patch_file"
  grep -q 'OH_ROOTLESS_RUNTIME_HOST_UID' "$registry" || {
    echo "OpenHands rootless compatibility patch did not apply: $registry" >&2
    exit 1
  }
  patched=$((patched + 1))
done < <(find "$cache_root" -type f \
  -path '*/openhands/agent_server/docker_runtime/registry.py' -print)

[[ "$patched" -gt 0 ]] || {
  echo "Pinned OpenHands agent-server ${agent_version} was not found in the uv cache." >&2
  exit 1
}

echo "Verified OpenHands ${agent_version} rootless runtime compatibility patch."
