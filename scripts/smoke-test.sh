#!/usr/bin/env bash
set -euo pipefail

: "${LITELLM_MASTER_KEY:?set LITELLM_MASTER_KEY}"

litellm_url="${LITELLM_URL:-http://127.0.0.1:4000}"
open_webui_url="${OPEN_WEBUI_URL:-http://127.0.0.1:8080}"
switch_url="${SWITCH_URL:-http://127.0.0.1:8000}"
openhands_url="${OPENHANDS_URL:-http://127.0.0.1:18000}"

curl --fail --silent --show-error "$litellm_url/health/liveliness" >/dev/null
curl --fail --silent --show-error \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  "$litellm_url/v1/models" >/dev/null
curl --fail --silent --show-error "$open_webui_url/health" >/dev/null
curl --fail --silent --show-error "$switch_url/health/ready" >/dev/null
curl --fail --silent --show-error "$openhands_url/alive" >/dev/null
echo "Platform smoke tests passed."

