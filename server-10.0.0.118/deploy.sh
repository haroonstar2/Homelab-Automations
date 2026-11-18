#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "ERROR: .env not found in $(pwd). Copy .env.example to .env first." >&2
  exit 1
fi

if [ ! -S /run/user/1000/docker.sock ]; then
  echo "ERROR: rootless Docker socket /run/user/1000/docker.sock is missing." >&2
  echo "Install and start Docker rootless mode for uid 1000 before deploying devbot." >&2
  exit 1
fi
if ! docker --host unix:///run/user/1000/docker.sock info --format '{{json .SecurityOptions}}' | grep -qi rootless; then
  echo "ERROR: verification daemon did not report rootless security mode." >&2
  exit 1
fi
if [ "$(docker --host unix:///run/user/1000/docker.sock info --format '{{.CgroupDriver}} {{.CgroupVersion}}')" != "systemd 2" ]; then
  echo "ERROR: rootless verification requires systemd cgroup v2 for resource limits." >&2
  exit 1
fi

echo "==> 1/5 Creating /mnt/Storage1/devbot data directories..."
sudo mkdir -p /mnt/Storage1/devbot/{archive,backup,docs,runner-scratch}
sudo chown -R 1000:1000 /mnt/Storage1/devbot

echo "==> 2/5 Updating Ollama and removing retired n8n/Windmill/sandbox containers..."
docker compose up -d --remove-orphans ollama searxng saas-postgres saas-redis

echo "==> 3/5 Pulling local models into Ollama (qwen3-coder:30b, qwen3:4b, nomic-embed-text)..."
docker exec ollama ollama pull qwen3:4b
docker exec ollama ollama pull nomic-embed-text
docker exec ollama ollama pull qwen3-coder:30b

echo "==> 4/5 Running CPU model benchmark..."
python3 devbot/scripts/benchmark_models.py qwen3-coder:30b qwen3:4b || true

echo "==> 5/5 Building and starting LiteLLM + devbot..."
docker compose up -d --build litellm devbot
docker compose ps
echo ""
echo "Done! Check logs with: docker logs -f devbot"
echo "Then run /status in your Discord server."
