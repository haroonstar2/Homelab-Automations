#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "--apply" || -n "${2:-}" ]]; then
  echo "Usage: sudo $0 --apply"
  echo "Runs the automated local-code qualification cases as ai-agent."
  exit 2
fi

if [[ "$EUID" -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 1
fi

agent_user="ai-agent"
agent_home="$(getent passwd "$agent_user" | cut -d: -f6)"
agent_group="$(id -gn "$agent_user")"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workspace="/mnt/Storage1/ai-platform/workspaces/qualification"
runner="$workspace/qualify.py"
secret_env="$agent_home/.config/ai-platform/litellm.env"
opencode="$agent_home/.local/bin/opencode"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
output="$workspace/results/$timestamp"

test -x "$opencode" || {
  echo "Pinned OpenCode is not installed for $agent_user." >&2
  exit 1
}
test -r "$secret_env" || {
  echo "LiteLLM agent environment is missing." >&2
  exit 1
}
test -r "$repo_root/tests/model-qualification/qualify.py" || {
  echo "Qualification runner is missing from the repository." >&2
  exit 1
}

install -d -m 0700 -o "$agent_user" -g "$agent_group" \
  "$workspace" "$workspace/results" "$output"
install -m 0700 -o "$agent_user" -g "$agent_group" \
  "$repo_root/tests/model-qualification/qualify.py" "$runner"

runuser -u "$agent_user" -- env \
  HOME="$agent_home" \
  PATH="$agent_home/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  bash -c '
    set -euo pipefail
    set -a
    source "$1"
    set +a
    "$2" --version >"$4/opencode-version.txt"
    exec python3 "$3" --model litellm/local-code --opencode "$2" --output "$4"
  ' bash "$secret_env" "$opencode" "$runner" "$output"

echo "Qualification evidence: $output"

