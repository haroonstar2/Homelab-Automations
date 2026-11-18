#!/usr/bin/env bash
set -euo pipefail

expected_user="${AGENT_USER:-ai-agent}"
workspace="${AGENT_TEST_WORKSPACE:-/mnt/Storage1/ai-platform/workspaces/isolation-test}"
rootless_socket="/run/user/$(id -u "$expected_user")/docker.sock"

if id -nG "$expected_user" | tr ' ' '\n' | grep -qx docker; then
  echo "FAIL: $expected_user belongs to the rootful docker group." >&2
  exit 1
fi

test -S "$rootless_socket" || {
  echo "FAIL: rootless socket is missing: $rootless_socket" >&2
  exit 1
}

if runuser -u "$expected_user" -- docker -H unix:///var/run/docker.sock info >/dev/null 2>&1; then
  echo "FAIL: agent identity can control rootful Docker." >&2
  exit 1
fi

install -d -m 0700 -o "$expected_user" -g "$expected_user" "$workspace"
runuser -u "$expected_user" -- ln -sfn /etc "$workspace/escape"

runuser -u "$expected_user" -- docker -H "unix://$rootless_socket" run --rm \
  --network none \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  -v "$workspace:/workspace:ro" \
  busybox:1.37.0@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec \
  sh -ec 'test ! -S /var/run/docker.sock; test "$(readlink /workspace/escape)" = /etc; test -r /workspace/escape/passwd'

echo "Rootless boundary checks passed. The symlink resolves to container /etc, not host /etc."

