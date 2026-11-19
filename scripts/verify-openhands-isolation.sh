#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "--apply" || -n "${2:-}" ]]; then
  echo "Usage: sudo $0 --apply"
  echo "Creates a disposable OpenHands conversation and verifies rootless isolation."
  exit 2
fi

if [[ "$EUID" -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 1
fi

for required in awk chmod chown comm curl cut docker find getent grep head id install jq kill mktemp \
  mv readlink rm runuser sed seq setfacl sh sleep sort tail; do
  command -v "$required" >/dev/null 2>&1 || {
    echo "Required command is missing: $required" >&2
    exit 1
  }
done

agent_user="ai-agent"
agent_group="$(id -gn "$agent_user")"
agent_uid="$(id -u "$agent_user")"
agent_home="$(getent passwd "$agent_user" | cut -d: -f6)"
container_uid_expected="$agent_uid"
subuid_start="$(awk -F: -v user="$agent_user" '$1 == user { print $2; exit }' /etc/subuid)"
[[ "$subuid_start" =~ ^[0-9]+$ ]] || {
  echo "No subordinate UID range exists for $agent_user." >&2
  exit 1
}
mapped_container_uid=$((subuid_start + container_uid_expected - 1))
runtime_dir="/run/user/$agent_uid"
rootless_socket="$runtime_dir/docker.sock"
canvas_url="http://127.0.0.1:18002"
expected_image="ghcr.io/openhands/agent-server:1.62.0-python"
openhands_env="$agent_home/.config/ai-platform/openhands.env"
state_root="/mnt/Storage1/ai-platform/openhands/state"
workspace_parent="/mnt/Storage1/ai-platform/workspaces/openhands"
workspace=""

backend_key="$(sed -n 's/^LOCAL_BACKEND_API_KEY=//p' "$openhands_env" | tail -n 1)"
[[ ${#backend_key} -ge 32 ]] || {
  echo "LOCAL_BACKEND_API_KEY is missing or invalid." >&2
  exit 1
}

work="$(mktemp -d)"
chmod 0700 "$work"
conversation_id=""
container_id=""
watcher_pid=""

agent_docker() {
  runuser -u "$agent_user" -- env \
    HOME="$agent_home" \
    XDG_RUNTIME_DIR="$runtime_dir" \
    DOCKER_HOST="unix://$rootless_socket" \
    docker "$@"
}

delete_conversation() {
  if [[ -n "$conversation_id" ]]; then
    curl --silent --show-error \
      --request DELETE \
      -H "X-Session-API-Key: $backend_key" \
      --output /dev/null \
      "$canvas_url/api/conversations/$conversation_id" || true
  fi
}

cleanup() {
  if [[ -n "$watcher_pid" ]]; then
    kill "$watcher_pid" >/dev/null 2>&1 || true
    wait "$watcher_pid" >/dev/null 2>&1 || true
  fi
  delete_conversation
  if [[ -n "$workspace" && "$workspace" == "$workspace_parent"/isolation-gate.* ]]; then
    rm -rf -- "$workspace"
  fi
  rm -rf -- "$work"
  unset backend_key
}
trap cleanup EXIT

test -S "$rootless_socket" || {
  echo "Rootless Docker socket is missing: $rootless_socket" >&2
  exit 1
}
agent_docker info --format '{{json .SecurityOptions}}' \
  | grep -q 'name=rootless' || {
    echo "The ai-agent Docker daemon does not report rootless mode." >&2
    exit 1
  }
if runuser -u "$agent_user" -- docker -H unix:///var/run/docker.sock info \
  >/dev/null 2>&1; then
  echo "The ai-agent identity can control the rootful Docker daemon." >&2
  exit 1
fi

install -d -m 0700 -o "$agent_user" -g "$agent_group" "$workspace_parent"
workspace="$(mktemp -d "$workspace_parent/isolation-gate.XXXXXXXX")"
chown "$agent_user:$agent_group" "$workspace"
setfacl -m \
  "u:${mapped_container_uid}:rwx,m::rwx,d:u:${mapped_container_uid}:rwx,d:m::rwx" \
  "$workspace"
find "$state_root" -type d -exec setfacl -m \
  "u:${mapped_container_uid}:rwx,m::rwx,d:u:${mapped_container_uid}:rwx,d:m::rwx" \
  {} +
runuser -u "$agent_user" -- sh -c \
  'printf "%s\n" openhands-isolation-gate >"$1/sentinel.txt"; ln -sfn /etc "$1/escape"' \
  sh "$workspace"

curl --fail --silent --show-error \
  -H "X-Session-API-Key: $backend_key" \
  -H 'X-Expose-Secrets: plaintext' \
  --output "$work/settings.json" \
  "$canvas_url/api/settings"

jq -e '
  .agent_settings.agent_kind == "openhands" and
  .llm_api_key_is_set == true
' "$work/settings.json" >/dev/null || {
  echo "OpenHands settings are not ready for an isolated conversation." >&2
  exit 1
}

agent_docker ps -aq --filter label=ai.openhands.runtime-owner \
  | sort >"$work/containers-before.txt"

capture_runtime_diagnostics() {
  local observed_id log_pid poll
  for _ in $(seq 1 18000); do
    agent_docker ps -aq --filter label=ai.openhands.runtime-owner \
      | sort >"$work/diagnostic-containers.txt"
    observed_id="$(comm -13 "$work/containers-before.txt" \
      "$work/diagnostic-containers.txt" | head -n 1)"
    if [[ -n "$observed_id" ]]; then
      printf '%s\n' "$observed_id" >"$work/diagnostic-container-id.txt"
      agent_docker logs --follow --timestamps "$observed_id" \
        >"$work/runtime.log" 2>&1 &
      log_pid=$!
      for poll in $(seq 1 120); do
        if ! agent_docker inspect "$observed_id" \
          >"$work/runtime-inspect-full.json" 2>/dev/null; then
          break
        fi
        jq '.[0] | {
          id: .Id,
          image: .Config.Image,
          user: .Config.User,
          path: .Path,
          args: .Args,
          state: .State,
          mounts: [.Mounts[]? | {type: .Type, source: .Source, destination: .Destination, rw: .RW}],
          network_mode: .HostConfig.NetworkMode,
          readonly_rootfs: .HostConfig.ReadonlyRootfs
        }' "$work/runtime-inspect-full.json" \
          >"$work/runtime-inspect-safe.tmp"
        mv "$work/runtime-inspect-safe.tmp" "$work/runtime-inspect-safe.json"
        sleep 0.1
      done
      kill "$log_pid" >/dev/null 2>&1 || true
      wait "$log_pid" >/dev/null 2>&1 || true
      return 0
    fi
    sleep 0.1
  done
}

jq -n \
  --arg workspace "$workspace" \
  --slurpfile settings "$work/settings.json" \
  '{
    workspace: {kind: "LocalWorkspace", working_dir: $workspace},
    agent_settings: $settings[0].agent_settings,
    initial_message: {
      role: "user",
      content: [{type: "text", text: "Isolation gate; do not run."}],
      run: false
    },
    max_iterations: 2,
    autotitle: false,
    container: {browser: false}
  }' >"$work/conversation-request.json"

echo "Creating disposable OpenHands conversation container..."
capture_runtime_diagnostics &
watcher_pid=$!
status="$(curl --silent --show-error \
  --max-time 1800 \
  --request POST \
  -H "X-Session-API-Key: $backend_key" \
  -H 'Content-Type: application/json' \
  --data-binary "@$work/conversation-request.json" \
  --output "$work/conversation-response.json" \
  --write-out '%{http_code}' \
  "$canvas_url/api/conversations")"
if [[ "$status" != "200" && "$status" != "201" ]]; then
  wait "$watcher_pid" >/dev/null 2>&1 || true
  watcher_pid=""
  echo "Conversation creation returned HTTP $status." >&2
  jq -r '.detail // .message // "No diagnostic message"' \
    "$work/conversation-response.json" >&2 || true
  if [[ -s "$work/runtime-inspect-safe.json" ]]; then
    echo "Short-lived runtime container:" >&2
    jq . "$work/runtime-inspect-safe.json" >&2
  fi
  if [[ -s "$work/runtime.log" ]]; then
    llm_key="$(jq -r '.agent_settings.llm.api_key // empty' "$work/settings.json")"
    echo "Redacted runtime log:" >&2
    if [[ -n "$llm_key" ]]; then
      sed -e "s/${backend_key}/[REDACTED_BACKEND_KEY]/g" \
        -e "s/${llm_key}/[REDACTED_LLM_KEY]/g" \
        "$work/runtime.log" | tail -n 100 >&2
    else
      sed -e "s/${backend_key}/[REDACTED_BACKEND_KEY]/g" \
        "$work/runtime.log" | tail -n 100 >&2
    fi
  fi
  exit 1
fi

wait "$watcher_pid" >/dev/null 2>&1 || true
watcher_pid=""

conversation_id="$(jq -r '.id // .conversation_id // empty' \
  "$work/conversation-response.json")"
[[ -n "$conversation_id" ]] || {
  echo "OpenHands did not return a conversation ID." >&2
  exit 1
}

for _ in $(seq 1 120); do
  agent_docker ps -aq --filter label=ai.openhands.runtime-owner \
    | sort >"$work/containers-after.txt"
  container_id="$(comm -13 "$work/containers-before.txt" \
    "$work/containers-after.txt" | head -n 1)"
  [[ -n "$container_id" ]] && break
  sleep 2
done
[[ -n "$container_id" ]] || {
  echo "OpenHands did not create a rootless conversation container." >&2
  exit 1
}

agent_docker inspect "$container_id" >"$work/container-inspect.json"
jq -e --arg image "$expected_image" '.[0].Config.Image == $image' \
  "$work/container-inspect.json" >/dev/null || {
  echo "Conversation container image is not pinned to $expected_image." >&2
  exit 1
}
jq -e '
  .[0].State.Running == true and
  ([.[0].Mounts[]?.Source] | all(
    contains("docker.sock") | not
  )) and
  ([.[0].HostConfig.Binds[]?] | all(
    contains("docker.sock") | not
  ))
' "$work/container-inspect.json" >/dev/null || {
  echo "Conversation container is stopped or exposes a Docker socket." >&2
  exit 1
}

container_uid="$(agent_docker exec "$container_id" id -u)"
[[ "$container_uid" != "0" ]] || {
  echo "Conversation container runs as root." >&2
  exit 1
}
agent_docker exec "$container_id" sh -ec '
  test ! -S /var/run/docker.sock
  test ! -S /run/docker.sock
' || {
  echo "Conversation container can see a Docker socket." >&2
  exit 1
}

workspace_destination="$(jq -r --arg source "$workspace" '
  .[0].Mounts[] | select(.Source == $source) | .Destination
' "$work/container-inspect.json" | head -n 1)"
[[ -n "$workspace_destination" ]] || {
  echo "Conversation container does not mount the isolated workspace." >&2
  exit 1
}
agent_docker exec "$container_id" sh -ec \
  'test "$(cat "$1/sentinel.txt")" = openhands-isolation-gate; test "$(readlink "$1/escape")" = /etc; test -r "$1/escape/passwd"' \
  sh "$workspace_destination"

delete_conversation
conversation_id=""
for _ in $(seq 1 60); do
  if ! agent_docker inspect "$container_id" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
if agent_docker inspect "$container_id" >/dev/null 2>&1; then
  echo "Conversation container remained after conversation deletion." >&2
  exit 1
fi

echo "OpenHands isolation gate passed."
echo "Conversation runtime: rootless Docker owned by $agent_user"
echo "Conversation image: $expected_image"
echo "Container user: uid $container_uid"
echo "Docker socket exposure: none"
echo "Workspace symlink boundary: container /etc, not host /etc"
echo "Conversation cleanup: container removed"
