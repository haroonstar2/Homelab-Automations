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
agent_uid="$(id -u "$agent_user")"
openhands_env="$agent_home/.config/ai-platform/openhands.env"
canvas_url="http://127.0.0.1:18002"
litellm_url="http://127.0.0.1:14000"
litellm_openai_url="$litellm_url/v1"

for required in chown chmod curl docker grep jq mktemp runuser sed seq sleep systemctl; do
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
runuser -u "$agent_user" -- env \
  HOME="$agent_home" \
  XDG_RUNTIME_DIR="/run/user/$agent_uid" \
  DOCKER_HOST="unix:///run/user/$agent_uid/docker.sock" \
  docker info >/dev/null || {
    echo "The ai-agent identity cannot access its rootless Docker daemon." >&2
    exit 1
  }

if ! grep -Fqx 'ALLOW_SHORT_CONTEXT_WINDOWS=true' "$openhands_env"; then
  if grep -q '^ALLOW_SHORT_CONTEXT_WINDOWS=' "$openhands_env"; then
    sed -i 's/^ALLOW_SHORT_CONTEXT_WINDOWS=.*/ALLOW_SHORT_CONTEXT_WINDOWS=true/' \
      "$openhands_env"
  else
    printf 'ALLOW_SHORT_CONTEXT_WINDOWS=true\n' >>"$openhands_env"
  fi
  chown "$agent_user:$(id -gn "$agent_user")" "$openhands_env"
  chmod 0600 "$openhands_env"
  runtime_dir="/run/user/$agent_uid"
  runuser -u "$agent_user" -- env \
    HOME="$agent_home" \
    XDG_RUNTIME_DIR="$runtime_dir" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime_dir/bus" \
    systemctl --user restart agent-canvas.service
  for _ in $(seq 1 60); do
    if curl --fail --silent "$canvas_url/ready" >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done
  curl --fail --silent --show-error "$canvas_url/ready" >/dev/null || {
    echo "Agent Canvas did not become ready after enabling short contexts." >&2
    exit 1
  }
fi

curl --fail --silent --show-error \
  -H "Authorization: Bearer $litellm_key" \
  "$litellm_url/v1/models" >"$work/models.json"
jq -e '.data[] | select(.id == "local-code")' "$work/models.json" >/dev/null || {
  echo "LiteLLM does not advertise the local-code model." >&2
  exit 1
}

jq -n '{agent_settings_diff: {agent_context: {}}}' \
  >"$work/context-migration.json"
api_write PATCH /api/settings "$work/context-migration.json" 200 \
  "$work/context-migration-response.json"

jq -n '{agent_settings_diff: {agent_kind: "openhands", agent_context: {}}}' \
  >"$work/agent-migration.json"
echo "Migrating Agent Canvas agent variant to OpenHands..."
api_write PATCH /api/settings "$work/agent-migration.json" 200 \
  "$work/agent-migration-response.json"
jq -e '.agent_settings.agent_kind == "openhands"' \
  "$work/agent-migration-response.json" >/dev/null || {
    echo "Agent Canvas did not switch to the OpenHands agent variant." >&2
    exit 1
  }

jq -n \
  --arg api_key "$litellm_key" \
  '{agent_settings_diff: {llm: {api_key: $api_key}}}' \
  >"$work/key-settings.json"
echo "Persisting encrypted local-code credential..."
api_write PATCH /api/settings "$work/key-settings.json" 200 \
  "$work/key-settings-response.json"

jq -n \
  --arg base_url "$litellm_openai_url" \
  '{agent_settings_diff: {llm: {base_url: $base_url}}}' \
  >"$work/base-url-settings.json"
echo "Persisting LiteLLM OpenAI-compatible base URL..."
api_write PATCH /api/settings "$work/base-url-settings.json" 200 \
  "$work/base-url-settings-response.json"

jq -n '{agent_settings_diff: {llm: {model: "litellm_proxy/local-code"}}}' \
  >"$work/model-settings.json"
echo "Persisting litellm_proxy/local-code model alias..."
api_write PATCH /api/settings "$work/model-settings.json" 200 \
  "$work/settings-response.json"
jq -e '
  .llm_api_key_is_set == true and
  .agent_settings.agent_kind == "openhands" and
  .agent_settings.llm.model == "litellm_proxy/local-code" and
  .agent_settings.llm.base_url == "http://127.0.0.1:14000/v1"
' "$work/settings-response.json" >/dev/null || {
  echo "OpenHands did not persist the expected encrypted LLM settings." >&2
  exit 1
}

jq -n '{
  model: "local-code",
  messages: [
    {role: "system", content: "Reply with the single word ready."},
    {role: "user", content: "Health check"}
  ],
  max_tokens: 8,
  temperature: 0,
  stream: false
}' >"$work/completion-request.json"

completion_status="$(curl --silent --show-error \
  --max-time 1200 \
  -H "Authorization: Bearer $litellm_key" \
  -H 'Content-Type: application/json' \
  --data-binary "@$work/completion-request.json" \
  --output "$work/completion-response.json" \
  --write-out '%{http_code}' \
  "$litellm_url/v1/chat/completions")"
if [[ "$completion_status" != "200" ]]; then
  echo "LiteLLM completion preflight returned HTTP $completion_status." >&2
  jq -r '.error.message // "No diagnostic message"' \
    "$work/completion-response.json" >&2 || true
  exit 1
fi
jq -e '.choices[0].message.content | type == "string" and length > 0' \
  "$work/completion-response.json" >/dev/null || {
    echo "LiteLLM completion preflight returned no assistant content." >&2
    exit 1
  }

echo "Stored encrypted OpenHands LLM settings: litellm_proxy/local-code"
echo "Completion route verified through authenticated LiteLLM: local-code"
echo "Accepted qualified short context window: 14336 tokens"
echo "Rootless Docker access verified for: $agent_user"
