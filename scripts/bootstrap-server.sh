#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "--apply" ]]; then
  echo "Usage: sudo $0 --apply"
  echo "Creates dedicated identities and /mnt/Storage1/ai-platform directories."
  exit 2
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 1
fi

service_user="ai-platform"
agent_user="ai-agent"
platform_root="/mnt/Storage1/ai-platform"
operator_user="${SUDO_USER:-}"

for account in "$service_user" "$agent_user"; do
  if ! id "$account" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash "$account"
  fi
done

if [[ -n "$operator_user" && "$operator_user" != root ]] && id "$operator_user" >/dev/null 2>&1; then
  usermod -aG "$service_user" "$operator_user"
fi

install -d -m 0750 -o "$service_user" -g "$service_user" \
  "$platform_root" \
  "$platform_root/ollama" \
  "$platform_root/open-webui" \
  "$platform_root/searxng-cache" \
  "$platform_root/switch" \
  "$platform_root/switch/postgres" \
  "$platform_root/openhands/state" \
  "$platform_root/n8n" \
  "$platform_root/backups"

# Caddy runs as uid 0 with all capabilities except NET_BIND_SERVICE dropped.
# Owning only its two persistent roots lets it write without DAC_OVERRIDE.
install -d -m 0770 -o root -g "$service_user" \
  "$platform_root/caddy/data" \
  "$platform_root/caddy/config"

install -d -m 0750 -o "$agent_user" -g "$agent_user" \
  "$platform_root/workspaces" \
  "$platform_root/workspaces/openhands" \
  "$platform_root/workspaces/switch"

# The released OpenHands Docker image uses uid 10001. Grant only its explicit
# state and project roots; never make either directory world-writable.
if command -v setfacl >/dev/null 2>&1; then
  # The agent may traverse the platform root to its owned workspace subtree,
  # but cannot list that root or read sibling service/backup directories.
  setfacl -m "u:${agent_user}:--x" "$platform_root"
  setfacl -m u:10001:rwx "$platform_root/openhands/state" "$platform_root/workspaces/openhands"
else
  echo "setfacl is required for the documented OpenHands container uid 10001." >&2
  exit 1
fi

loginctl enable-linger "$agent_user"
agent_uid="$(id -u "$agent_user")"
systemctl start "user@${agent_uid}.service"

if command -v dockerd-rootless-setuptool.sh >/dev/null 2>&1; then
  runuser -l "$agent_user" -c "XDG_RUNTIME_DIR=/run/user/$agent_uid DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$agent_uid/bus dockerd-rootless-setuptool.sh install"
  runuser -l "$agent_user" -c "XDG_RUNTIME_DIR=/run/user/$agent_uid DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$agent_uid/bus systemctl --user enable --now docker.service"
else
  echo "Rootless Docker helper is missing; install docker-ce-rootless-extras, then rerun." >&2
  exit 1
fi

if id -nG "$agent_user" | tr ' ' '\n' | grep -qx docker; then
  echo "$agent_user must not belong to the rootful docker group." >&2
  exit 1
fi

echo "Bootstrap complete. Run tests/isolation/verify-rootless-boundary.sh next."

