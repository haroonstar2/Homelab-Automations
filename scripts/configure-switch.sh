#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<EOF
Usage: sudo $0 --apply --env-file /path/to/.env \\
  --server-name NAME --admin-email EMAIL --public-url HTTPS_ORIGIN

Generates missing Switch secrets, records its permanent identity, and validates
the pinned Compose configuration. It does not start Switch.
EOF
}

if [[ "${1:-}" != "--apply" || "${2:-}" != "--env-file" || -z "${3:-}" || \
      "${4:-}" != "--server-name" || -z "${5:-}" || \
      "${6:-}" != "--admin-email" || -z "${7:-}" || \
      "${8:-}" != "--public-url" || -z "${9:-}" || -n "${10:-}" ]]; then
  usage
  exit 2
fi

if [[ "$EUID" -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 1
fi

env_file="$3"
server_name="$5"
admin_email="$7"
public_url="${9%/}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
compose_file="$repo_root/compose/switch.yml"

for required in chmod docker grep install mktemp openssl sed stat; do
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

if [[ ! "$server_name" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ || \
      "$server_name" == *..* ]]; then
  echo "Server name must be a stable DNS-style identifier without a scheme or port." >&2
  exit 1
fi
if [[ ! "$admin_email" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]]; then
  echo "Admin email is invalid." >&2
  exit 1
fi
if [[ ! "$public_url" =~ ^https://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?$ ]]; then
  echo "Public URL must be an HTTPS origin with no path, query, or fragment." >&2
  exit 1
fi

read_value() {
  local name="$1"
  local file="$2"
  local count
  count="$(grep -c "^${name}=" "$file" || true)"
  if [[ "$count" -gt 1 ]]; then
    echo "Environment file contains duplicate $name entries." >&2
    exit 1
  fi
  sed -n "s/^${name}=//p" "$file"
}

existing_server_name="$(read_value SWITCH_SERVER_NAME "$env_file")"
if [[ -n "$existing_server_name" && "$existing_server_name" != "$server_name" ]]; then
  echo "Refusing to change permanent SWITCH_SERVER_NAME from '$existing_server_name'." >&2
  exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"; unset existing_server_name' EXIT
chmod 0700 "$work"
candidate="$work/platform.env"
install -m 0600 "$env_file" "$candidate"

set_value() {
  local name="$1"
  local value="$2"
  local file="$3"
  local escaped="$value"
  escaped="${escaped//\\/\\\\}"
  escaped="${escaped//&/\\&}"
  escaped="${escaped//|/\\|}"
  if grep -q "^${name}=" "$file"; then
    sed -i "s|^${name}=.*|${name}=${escaped}|" "$file"
  else
    printf '%s=%s\n' "$name" "$value" >>"$file"
  fi
}

generated=()
generate_if_empty() {
  local name="$1"
  local value="$2"
  if [[ -z "$(read_value "$name" "$candidate")" ]]; then
    set_value "$name" "$value" "$candidate"
    generated+=("$name")
  fi
}

set_value SWITCH_SERVER_NAME "$server_name" "$candidate"
set_value SWITCH_ADMIN_EMAIL "$admin_email" "$candidate"
set_value SWITCH_FRONTEND_BASE_URL "$public_url" "$candidate"
set_value SWITCH_GATEWAY_PUBLIC_URL "$public_url" "$candidate"

generate_if_empty SWITCH_DB_PASSWORD "$(openssl rand -hex 32)"
generate_if_empty SWITCH_DB_OWNER_PASSWORD "$(openssl rand -hex 32)"
generate_if_empty SWITCH_AGENT_REGISTRATION_TOKEN "$(openssl rand -hex 32)"
generate_if_empty SWITCH_SECRET_KEYS "initial:$(openssl rand -hex 32)"
generate_if_empty SWITCH_CONTROLLER_TOKEN_SECRET "$(openssl rand -hex 32)"
generate_if_empty SWITCH_ADMIN_PASSWORD "$(openssl rand -hex 24)"

for secret_name in \
  SWITCH_DB_PASSWORD \
  SWITCH_DB_OWNER_PASSWORD \
  SWITCH_AGENT_REGISTRATION_TOKEN \
  SWITCH_CONTROLLER_TOKEN_SECRET; do
  secret_value="$(read_value "$secret_name" "$candidate")"
  if [[ ${#secret_value} -lt 32 ]]; then
    echo "$secret_name must contain at least 32 characters." >&2
    exit 1
  fi
done
admin_password="$(read_value SWITCH_ADMIN_PASSWORD "$candidate")"
if [[ ${#admin_password} -lt 16 ]]; then
  echo "SWITCH_ADMIN_PASSWORD must contain at least 16 characters." >&2
  exit 1
fi
secret_keys="$(read_value SWITCH_SECRET_KEYS "$candidate")"
IFS=',' read -r -a key_entries <<<"$secret_keys"
for entry in "${key_entries[@]}"; do
  key_id="${entry%%:*}"
  key_secret="${entry#*:}"
  if [[ "$entry" != *:* || ! "$key_id" =~ ^[A-Za-z0-9._-]{1,32}$ || \
        ${#key_secret} -lt 32 ]]; then
    echo "SWITCH_SECRET_KEYS must contain id:secret entries with 32+ character secrets." >&2
    exit 1
  fi
done

docker compose --env-file "$candidate" -f "$compose_file" config --quiet

owner_uid="$(stat -c '%u' "$env_file")"
owner_gid="$(stat -c '%g' "$env_file")"
install -m 0600 -o "$owner_uid" -g "$owner_gid" "$candidate" "$env_file"

echo "Switch configuration validated without starting services."
echo "Permanent server name: $server_name"
echo "Gateway and frontend origin: $public_url"
if [[ ${#generated[@]} -gt 0 ]]; then
  echo "Generated and stored missing secrets: ${generated[*]}"
else
  echo "All existing secrets were preserved."
fi
echo "Admin password remains only in the mode-0600 environment file."
