#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "--apply" || -n "${2:-}" ]]; then
  echo "Usage: sudo $0 --apply"
  echo "Applies the pinned OpenHands 1.62.0 rootless bind-mount compatibility patch."
  exit 2
fi

if [[ "$EUID" -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 1
fi

for required in awk chmod chown cut getent grep id install patch runuser sed systemctl; do
  command -v "$required" >/dev/null 2>&1 || {
    echo "Required command is missing: $required" >&2
    exit 1
  }
done

agent_user="ai-agent"
agent_group="$(id -gn "$agent_user")"
agent_uid="$(id -u "$agent_user")"
agent_home="$(getent passwd "$agent_user" | cut -d: -f6)"
subuid_start="$(awk -F: -v user="$agent_user" '$1 == user { print $2; exit }' /etc/subuid)"
[[ "$subuid_start" =~ ^[0-9]+$ ]] || {
  echo "No subordinate UID range exists for $agent_user." >&2
  exit 1
}
mapped_runtime_uid=$((subuid_start + agent_uid - 1))
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_env="$agent_home/.config/ai-platform/openhands.env"
runtime_dir="/run/user/$agent_uid"
compat_share="$agent_home/.local/share/ai-platform"
compat_workdir="/mnt/Storage1/ai-platform/workspaces/openhands"

install -d -m 0755 -o "$agent_user" -g "$agent_group" "$compat_share"
install -m 0755 -o "$agent_user" -g "$agent_group" \
  "$repo_root/scripts/patch-openhands-rootless-runtime.sh" \
  "$agent_home/.local/bin/patch-openhands-rootless-runtime"
install -m 0644 -o "$agent_user" -g "$agent_group" \
  "$repo_root/patches/openhands-agent-server-1.62.0-rootless-acl.patch" \
  "$compat_share/openhands-agent-server-1.62.0-rootless-acl.patch"

if grep -q '^OH_ROOTLESS_RUNTIME_HOST_UID=' "$runtime_env"; then
  sed -i "s/^OH_ROOTLESS_RUNTIME_HOST_UID=.*/OH_ROOTLESS_RUNTIME_HOST_UID=${mapped_runtime_uid}/" "$runtime_env"
else
  printf 'OH_ROOTLESS_RUNTIME_HOST_UID=%s\n' "$mapped_runtime_uid" >>"$runtime_env"
fi
if grep -q '^OH_CONVERSATION_IMAGE=' "$runtime_env"; then
  sed -i 's|^OH_CONVERSATION_IMAGE=.*|OH_CONVERSATION_IMAGE=ghcr.io/openhands/agent-server:1.62.0-python|' "$runtime_env"
else
  printf '%s\n' 'OH_CONVERSATION_IMAGE=ghcr.io/openhands/agent-server:1.62.0-python' >>"$runtime_env"
fi
chown "$agent_user:$agent_group" "$runtime_env"
chmod 0600 "$runtime_env"

cd "$compat_workdir"
runuser -u "$agent_user" -- env \
  HOME="$agent_home" \
  PATH="$agent_home/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  uvx --python='>=3.12,<3.15' \
    --from openhands-agent-server==1.62.0 \
    --with openhands-sdk==1.62.0 \
    --with openhands-tools==1.62.0 \
    --with openhands-workspace==1.62.0 \
    --with 'posthog>=6,<7' \
    agent-server --help >/dev/null
runuser -u "$agent_user" -- env \
  HOME="$agent_home" \
  PATH="$agent_home/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  patch-openhands-rootless-runtime

install -m 0644 -o "$agent_user" -g "$agent_group" \
  "$repo_root/config/systemd/agent-canvas.service" \
  "$agent_home/.config/systemd/user/agent-canvas.service"

runuser -u "$agent_user" -- env \
  HOME="$agent_home" \
  XDG_RUNTIME_DIR="$runtime_dir" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime_dir/bus" \
  systemctl --user daemon-reload
runuser -u "$agent_user" -- env \
  HOME="$agent_home" \
  XDG_RUNTIME_DIR="$runtime_dir" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime_dir/bus" \
  systemctl --user restart agent-canvas.service

echo "Applied OpenHands 1.62.0 rootless runtime compatibility for host UID $mapped_runtime_uid."
