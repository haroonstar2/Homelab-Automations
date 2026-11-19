#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: sudo $0 --apply --env-file /path/to/.env"
  echo "Starts the pinned Switch stack on rootful Docker and verifies it through Caddy."
}

if [[ "${1:-}" != "--apply" || "${2:-}" != "--env-file" || -z "${3:-}" || -n "${4:-}" ]]; then
  usage
  exit 2
fi

if [[ "$EUID" -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 1
fi

env_file="$3"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
compose_file="$repo_root/compose/switch.yml"
docker_context="default"
frontend_network="ai-platform-switch-frontend"
ready_url="http://127.0.0.1:18000/health/ready"

for required in curl docker grep sed seq sleep stat; do
  command -v "$required" >/dev/null 2>&1 || {
    echo "Required command is missing: $required" >&2
    exit 1
  }
done

[[ -f "$env_file" && -r "$env_file" ]] || {
  echo "Cannot read platform environment file: $env_file" >&2
  exit 1
}
[[ -r "$compose_file" ]] || {
  echo "Cannot read Switch Compose file: $compose_file" >&2
  exit 1
}

env_mode="$(stat -c '%a' "$env_file")"
if (( (8#$env_mode & 8#077) != 0 )); then
  echo "Environment file must not be accessible by group or other users (mode is $env_mode)." >&2
  exit 1
fi

read_value() {
  local name="$1"
  local count
  count="$(grep -c "^${name}=" "$env_file" || true)"
  if [[ "$count" -ne 1 ]]; then
    echo "Environment file must contain exactly one $name entry." >&2
    exit 1
  fi
  sed -n "s/^${name}=//p" "$env_file"
}

version="$(read_value SWITCH_VERSION)"
server_name="$(read_value SWITCH_SERVER_NAME)"
admin_email="$(read_value SWITCH_ADMIN_EMAIL)"
public_url="$(read_value SWITCH_GATEWAY_PUBLIC_URL)"
frontend_url="$(read_value SWITCH_FRONTEND_BASE_URL)"

[[ "$version" == "0.32.1" ]] || {
  echo "Refusing unreviewed Switch version: $version" >&2
  exit 1
}
[[ -n "$server_name" ]] || {
  echo "SWITCH_SERVER_NAME must be configured before deployment." >&2
  exit 1
}
[[ -n "$admin_email" ]] || {
  echo "SWITCH_ADMIN_EMAIL must be configured before deployment." >&2
  exit 1
}
[[ "$public_url" =~ ^https:// ]] || {
  echo "SWITCH_GATEWAY_PUBLIC_URL must be HTTPS." >&2
  exit 1
}
[[ "$frontend_url" == "$public_url" ]] || {
  echo "Switch frontend and gateway origins must match for this single-origin deployment." >&2
  exit 1
}

for secret_name in \
  SWITCH_DB_PASSWORD \
  SWITCH_DB_OWNER_PASSWORD \
  SWITCH_AGENT_REGISTRATION_TOKEN \
  SWITCH_SECRET_KEYS \
  SWITCH_CONTROLLER_TOKEN_SECRET \
  SWITCH_ADMIN_PASSWORD; do
  [[ -n "$(read_value "$secret_name")" ]] || {
    echo "$secret_name is empty; run configure-switch.sh first." >&2
    exit 1
  }
done

docker --context "$docker_context" info >/dev/null
docker --context "$docker_context" network inspect "$frontend_network" >/dev/null

network_members="$(docker --context "$docker_context" network inspect \
  "$frontend_network" --format '{{range .Containers}}{{println .Name}}{{end}}')"
if ! grep -qx 'ai-platform-core-caddy-1' <<<"$network_members"; then
  echo "Caddy is not attached to $frontend_network." >&2
  exit 1
fi

compose=(docker --context "$docker_context" compose --env-file "$env_file" -f "$compose_file")
"${compose[@]}" config --quiet
"${compose[@]}" pull postgres switch
"${compose[@]}" up -d postgres init-db switch

switch_id="$("${compose[@]}" ps -q switch)"
[[ -n "$switch_id" ]] || {
  echo "Switch container was not created." >&2
  exit 1
}

for _ in $(seq 1 60); do
  health="$(docker --context "$docker_context" inspect \
    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
    "$switch_id")"
  case "$health" in
    healthy)
      break
      ;;
    exited|dead|unhealthy)
      echo "Switch entered terminal state: $health" >&2
      "${compose[@]}" logs --tail 100 switch >&2
      exit 1
      ;;
  esac
  sleep 3
done

health="$(docker --context "$docker_context" inspect \
  --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
  "$switch_id")"
[[ "$health" == "healthy" ]] || {
  echo "Switch did not become healthy within 180 seconds (state: $health)." >&2
  "${compose[@]}" logs --tail 100 switch >&2
  exit 1
}

if docker --context "$docker_context" inspect \
    --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}' \
    "$switch_id" | grep -Eq 'docker\.sock|/mnt/Storage1($|[[:space:]])|/home($|[[:space:]])'; then
  echo "Switch has an unsafe host mount." >&2
  exit 1
fi

curl --fail --silent --show-error --max-time 10 "$ready_url" >/dev/null

echo "Switch deployment gate passed."
echo "Version: $version"
echo "Permanent server name: $server_name"
echo "Container health: healthy"
echo "Caddy route: $ready_url"
echo "Docker engine: rootful context '$docker_context'"
echo "Discord credentials: deferred; legacy bot remains untouched"
echo "Tailscale Serve has not been changed."
