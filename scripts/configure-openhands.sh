#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: sudo $0 --apply --env-file /path/to/.env"
  echo "Configures Agent Canvas to use the local authenticated LiteLLM proxy."
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
agent_user="ai-agent"
agent_home="$(getent passwd "$agent_user" | cut -d: -f6)"
openhands_env="$agent_home/.config/ai-platform/openhands.env"
canvas_url="http://127.0.0.1:18002"
litellm_url="http://127.0.0.1:14000"
provider_name="Local LiteLLM"
profile_name="local-code"

for required in curl jq sed mktemp; do
  command -v "$required" >/dev/null 2>&1 || {
    echo "Required command is missing: $required" >&2
    exit 1
  }
done

[[ -r "$env_file" ]] || {
  echo "Cannot read platform environment file: $env_file" >&2
  exit 1
}
[[ -r "$openhands_env" ]] || {
  echo "Cannot read OpenHands environment file: $openhands_env" >&2
  exit 1
}

read_value() {
  local name="$1"
  local file="$2"
  sed -n "s/^${name}=//p" "$file" | tail -n 1
}

litellm_key="$(read_value LITELLM_MASTER_KEY "$env_file")"
backend_key="$(read_value LOCAL_BACKEND_API_KEY "$openhands_env")"
[[ "$litellm_key" == sk-* ]] || {
  echo "LITELLM_MASTER_KEY is missing or invalid." >&2
  exit 1
}
[[ ${#backend_key} -ge 32 ]] || {
  echo "LOCAL_BACKEND_API_KEY is missing or invalid." >&2
  exit 1
}

work="$(mktemp -d)"
trap 'rm -rf "$work"; unset litellm_key backend_key' EXIT
chmod 0700 "$work"

api_get() {
  local path="$1"
  local output="$2"
  curl --fail --silent --show-error \
    -H "X-Session-API-Key: $backend_key" \
    --output "$output" \
    "$canvas_url$path"
}

api_write() {
  local method="$1"
  local path="$2"
  local payload="$3"
  local expected="$4"
  local output="$5"
  local status
  status="$(curl --silent --show-error \
    --request "$method" \
    -H "X-Session-API-Key: $backend_key" \
    -H 'Content-Type: application/json' \
    --data-binary "@$payload" \
    --output "$output" \
    --write-out '%{http_code}' \
    "$canvas_url$path")"
  if [[ "$status" != "$expected" ]]; then
    echo "OpenHands API request failed: $method $path returned HTTP $status." >&2
    jq -r 'if type == "object" then (.detail // .message // "No diagnostic message") else "No diagnostic message" end' \
      "$output" >&2 || true
    exit 1
  fi
}

curl --fail --silent --show-error "$canvas_url/ready" >/dev/null
curl --fail --silent --show-error \
  -H "Authorization: Bearer $litellm_key" \
  "$litellm_url/v1/models" >"$work/models.json"
jq -e '.data[] | select(.id == "local-code")' "$work/models.json" >/dev/null || {
  echo "LiteLLM does not advertise the local-code model." >&2
  exit 1
}

api_get /api/llm/provider-connections "$work/connections.json"
connection_id="$(jq -r --arg name "$provider_name" \
  '[.[] | select(.display_name == $name)][0].id // empty' "$work/connections.json")"

jq -n \
  --arg display_name "$provider_name" \
  --arg api_key "$litellm_key" \
  --arg base_url "$litellm_url" \
  '{display_name: $display_name, provider: "custom", api_key: $api_key, base_url: $base_url}' \
  >"$work/provider.json"

if [[ -z "$connection_id" ]]; then
  api_write POST /api/llm/provider-connections "$work/provider.json" 201 "$work/provider-response.json"
  connection_id="$(jq -r '.id' "$work/provider-response.json")"
  provider_action="created"
else
  api_write PATCH "/api/llm/provider-connections/$connection_id" \
    "$work/provider.json" 200 "$work/provider-response.json"
  provider_action="updated"
fi

[[ -n "$connection_id" && "$connection_id" != "null" ]] || {
  echo "OpenHands did not return a provider connection ID." >&2
  exit 1
}

jq -n \
  --arg connection_id "$connection_id" \
  '{
    llm: {
      model: "litellm_proxy/local-code",
      provider_connection_id: $connection_id,
      auth_type: "api_key",
      usage_id: "openhands-local-code",
      api_mode: "chat",
      timeout: 1200,
      stream_idle_timeout: 1200,
      drop_params: true,
      native_tool_calling: true,
      log_completions: false
    },
    include_secrets: false
  }' >"$work/profile.json"

api_write POST "/api/profiles/$profile_name/validate" \
  "$work/profile.json" 200 "$work/validate-response.json"
jq -e '.valid == true' "$work/validate-response.json" >/dev/null || {
  echo "OpenHands rejected the local-code profile during validation." >&2
  jq -r '.message // .error // "No diagnostic message"' "$work/validate-response.json" >&2
  exit 1
}

api_write POST "/api/profiles/$profile_name" \
  "$work/profile.json" 201 "$work/profile-response.json"
api_write POST "/api/profiles/$profile_name/activate" \
  /dev/null 200 "$work/activate-response.json"
api_get /api/profiles "$work/profiles.json"
jq -e --arg name "$profile_name" '.active_profile == $name' "$work/profiles.json" >/dev/null || {
  echo "OpenHands did not activate the expected profile." >&2
  exit 1
}

echo "OpenHands provider connection $provider_action: $provider_name"
echo "Validated and activated OpenHands LLM profile: $profile_name"
echo "Model route verified through authenticated LiteLLM: local-code"
