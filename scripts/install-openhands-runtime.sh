#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "--apply" || -n "${2:-}" ]]; then
  echo "Usage: sudo $0 --apply"
  echo "Installs the pinned Agent Canvas host runtime for ai-agent."
  exit 2
fi

if [[ "$EUID" -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 1
fi

agent_user="ai-agent"
agent_group="$(id -gn "$agent_user")"
agent_uid="$(id -u "$agent_user")"
agent_home="$(getent passwd "$agent_user" | cut -d: -f6)"
container_uid="$agent_uid"
subuid_start="$(awk -F: -v user="$agent_user" '$1 == user { print $2; exit }' /etc/subuid)"
[[ "$subuid_start" =~ ^[0-9]+$ ]] || {
  echo "No subordinate UID range exists for $agent_user." >&2
  exit 1
}
mapped_container_uid=$((subuid_start + container_uid - 1))
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
platform_root="/mnt/Storage1/ai-platform"

node_version="24.22.3"
node_archive="node-v${node_version}-linux-x64.tar.xz"
node_sha256="4f2b175a6d53a5c617a80dba2676238a0688cef28c6da4eba35c7d844e66fcfb"
node_url="https://nodejs.org/dist/v${node_version}/${node_archive}"
node_root="$agent_home/.local/lib/node-v${node_version}-linux-x64"

uv_version="0.13.9"
uv_archive="uv-x86_64-unknown-linux-gnu.tar.gz"
uv_sha256="a7fbaff2b9a271d85c1bdb540941090fd1ce011797b4abeb48e4dcd39c2471d5"
uv_url="https://github.com/astral-sh/uv/releases/download/${uv_version}/${uv_archive}"

canvas_version="1.41.0"
canvas_package="@openhands/agent-canvas@${canvas_version}"
state_root="$platform_root/openhands/state"
workspace_root="$platform_root/workspaces/openhands"
runtime_env="$agent_home/.config/ai-platform/openhands.env"
user_unit_dir="$agent_home/.config/systemd/user"
compat_share="$agent_home/.local/share/ai-platform"

for required in awk curl grep patch tar xz sha256sum openssl setfacl; do
  command -v "$required" >/dev/null 2>&1 || {
    echo "Required command is missing: $required" >&2
    exit 1
  }
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

curl --fail --location --retry 3 --output "$work/$node_archive" "$node_url"
echo "$node_sha256  $work/$node_archive" | sha256sum --check --status || {
  echo "Node archive checksum verification failed." >&2
  exit 1
}
tar -xJf "$work/$node_archive" -C "$work"

curl --fail --location --retry 3 --output "$work/$uv_archive" "$uv_url"
echo "$uv_sha256  $work/$uv_archive" | sha256sum --check --status || {
  echo "uv archive checksum verification failed." >&2
  exit 1
}
tar -xzf "$work/$uv_archive" -C "$work"

install -d -m 0755 -o "$agent_user" -g "$agent_group" \
  "$agent_home/.local/lib" "$agent_home/.local/bin"
if [[ ! -d "$node_root" ]]; then
  mv "$work/node-v${node_version}-linux-x64" "$node_root"
fi
chown -R "$agent_user:$agent_group" "$node_root"

for command_name in node npm npx corepack; do
  ln -sfn "$node_root/bin/$command_name" "$agent_home/.local/bin/$command_name"
done
uv_dir="$work/uv-x86_64-unknown-linux-gnu"
install -m 0755 -o "$agent_user" -g "$agent_group" "$uv_dir/uv" "$agent_home/.local/bin/uv"
install -m 0755 -o "$agent_user" -g "$agent_group" "$uv_dir/uvx" "$agent_home/.local/bin/uvx"

runuser -u "$agent_user" -- env \
  HOME="$agent_home" \
  PATH="$agent_home/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  npm install --global --prefix "$agent_home/.local" "$canvas_package"

install -d -m 0750 -o "$agent_user" -g "$agent_group" "$state_root" "$workspace_root"
setfacl -m "u:${agent_user}:--x" "$platform_root" "$platform_root/openhands"
setfacl -m \
  "u:${mapped_container_uid}:rwx,m::rwx,d:u:${mapped_container_uid}:rwx,d:m::rwx" \
  "$state_root" "$workspace_root"

install -d -m 0700 -o "$agent_user" -g "$agent_group" \
  "$agent_home/.config/ai-platform" "$user_unit_dir"
install -d -m 0755 -o "$agent_user" -g "$agent_group" "$compat_share"
if [[ ! -s "$runtime_env" ]]; then
  umask 077
  runtime_temp="$work/openhands.env"
  {
    printf 'LOCAL_BACKEND_API_KEY=%s\n' "$(openssl rand -hex 32)"
    printf 'OH_SECRET_KEY=%s\n' "$(openssl rand -hex 32)"
    printf 'AUTOMATION_KV_SECRET=%s\n' "$(openssl rand -hex 32)"
    printf 'DOCKER_HOST=unix:///run/user/%s/docker.sock\n' "$agent_uid"
    printf 'OH_CONVERSATION_RUNTIME=docker\n'
    printf 'OH_CANVAS_SAFE_STATE_DIR=%s/agent-canvas\n' "$state_root"
    printf 'ALLOW_SHORT_CONTEXT_WINDOWS=true\n'
    printf 'AGENT_CANVAS_DISABLE_TELEMETRY=1\n'
  } >"$runtime_temp"
  install -m 0600 -o "$agent_user" -g "$agent_group" "$runtime_temp" "$runtime_env"
fi
if ! grep -q '^OH_ROOTLESS_RUNTIME_HOST_UID=' "$runtime_env"; then
  printf 'OH_ROOTLESS_RUNTIME_HOST_UID=%s\n' "$mapped_container_uid" >>"$runtime_env"
fi
chown "$agent_user:$agent_group" "$runtime_env"
chmod 0600 "$runtime_env"

install -m 0755 -o "$agent_user" -g "$agent_group" \
  "$repo_root/scripts/patch-openhands-rootless-runtime.sh" \
  "$agent_home/.local/bin/patch-openhands-rootless-runtime"
install -m 0644 -o "$agent_user" -g "$agent_group" \
  "$repo_root/patches/openhands-agent-server-1.62.0-rootless-acl.patch" \
  "$compat_share/openhands-agent-server-1.62.0-rootless-acl.patch"

cd "$workspace_root"
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
  "$repo_root/config/systemd/agent-canvas.service" "$user_unit_dir/agent-canvas.service"

runuser -u "$agent_user" -- env \
  HOME="$agent_home" \
  PATH="$agent_home/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  agent-canvas --version
runuser -u "$agent_user" -- env \
  HOME="$agent_home" \
  PATH="$agent_home/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  uv --version

runtime_dir="/run/user/$agent_uid"
runuser -u "$agent_user" -- env \
  HOME="$agent_home" \
  XDG_RUNTIME_DIR="$runtime_dir" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime_dir/bus" \
  systemctl --user daemon-reload
runuser -u "$agent_user" -- env \
  HOME="$agent_home" \
  XDG_RUNTIME_DIR="$runtime_dir" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime_dir/bus" \
  systemctl --user enable agent-canvas.service
runuser -u "$agent_user" -- env \
  HOME="$agent_home" \
  XDG_RUNTIME_DIR="$runtime_dir" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime_dir/bus" \
  systemctl --user restart agent-canvas.service

echo "Installed and started Agent Canvas $canvas_version for $agent_user."
