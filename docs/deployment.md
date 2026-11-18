# Deployment

This document describes staged deployment. It is deliberately not a one-shot
cutover command.

## Verified upstream contracts (2026-11-18)

| Component | Pin | Upstream contract |
| --- | --- | --- |
| Switch core | 0.32.1 | `switch-v0.32.1` standalone Compose |
| Switch Console | 0.41.1 | separate desktop release |
| OpenHands Agent Canvas | 1.41.0 | Docker quickstart and VM self-hosting guide |
| Ollama | 0.40.16 | Docker and local FAQ |
| LiteLLM | 1.108.11 LTS line | proxy configuration and reliability docs |
| Open WebUI | 0.13.4 | Docker quickstart and hardening guide |
| SearXNG | 2026.11.13-8a53a6aa5 | official rolling container build |
| Caddy | 2.11.9 | reverse proxy and Tailscale certificate docs |
| OpenCode | 2.2.7 | custom OpenAI-compatible provider and scripted run docs |
| n8n | 2.48.5 | official stable release |

Upgrade only through a reviewed pull request. Re-read each component's release
notes and configuration schema; do not change these pins in place on the server.

## Prerequisites

1. Run `scripts/bootstrap-server.sh --apply` as root.
2. Run `tests/isolation/verify-rootless-boundary.sh` as root and retain output.
3. Seed the dedicated model store with
   `sudo scripts/seed-ollama-store.sh --apply`. This copies and verifies the
   active legacy store; it never starts a second writer against that store.
4. Copy `.env.example` to a server-side `.env`, set mode `0600`, generate every
   required secret, and choose `SWITCH_SERVER_NAME` permanently.
5. Keep the legacy stack running. Do not reuse its Discord application.
6. Validate with `docker compose --env-file .env -f compose/<file>.yml config`.

The bootstrap requires `setfacl` so the documented OpenHands container uid
10001 can access only its explicit state and project roots without making them
world-writable. It adds the invoking sudo user to the `ai-platform` service
group; start a new login session before running operator backups.

## Core model and chat plane

Start only after configuration validation:

```bash
docker compose --env-file .env -f compose/core.yml up -d ollama litellm searxng open-webui caddy
```

Caddy listens only on host loopback:

- `127.0.0.1:18080` -> Open WebUI
- `127.0.0.1:18000` -> Switch
- `127.0.0.1:18001` -> OpenHands
- `127.0.0.1:14000` -> authenticated LiteLLM for host-side agent CLIs only

Publish the loopback listeners to the tailnet after reviewing tailnet ACLs:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:18080
tailscale serve --bg --https=8443 http://127.0.0.1:18000
tailscale serve --bg --https=8444 http://127.0.0.1:18001
tailscale serve status
```

The first command uses the normal tailnet HTTPS URL. Switch and OpenHands use
explicit HTTPS ports on the same MagicDNS host. Do not use Funnel.

## OpenHands runtime gate

`compose/openhands.yml` implements OpenHands' documented single-container
quickstart and is only for authentication and proxy validation:

```bash
docker compose --env-file .env -f compose/openhands.yml --profile single-sandbox up -d
```

It does not pass the Phase 3 isolation gate. The documented production shape is
Agent Canvas running under the dedicated `ai-agent` identity with:

```bash
export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock
export OH_CONVERSATION_RUNTIME=docker
export LOCAL_BACKEND_API_KEY='from a mode-0600 environment file'
agent-canvas --public --host 127.0.0.1
```

Install the pinned Node.js 24.22.3, uv 0.13.9, and
`@openhands/agent-canvas@1.41.0` runtime and its user systemd unit with:

```bash
sudo scripts/install-openhands-runtime.sh --apply
sudo scripts/configure-openhands.sh --apply --env-file .env
```

Public mode keeps backend-key authentication enabled, while the explicit host
argument restricts the service to loopback port `18002`. Its internal Agent
Server and automation processes use loopback ports `18100` and `18101` to avoid
the Caddy listeners. It requires its generated agent-only backend key, disables telemetry, and uses
`OH_CONVERSATION_RUNTIME=docker` with the `ai-agent` rootless Docker socket.
The configuration command first verifies that `ai-agent` can access its own
rootless Docker daemon. It performs an authenticated completion through the
`local-code` proxy route, then uses Agent Server's settings API to encrypt and
persist only its required model, key, and base URL without printing either API
key. Agent Canvas initially persists ACP settings with `agent_context: null`,
so the command first migrates that field to an object and explicitly switches
the discriminator to a valid default `openhands` agent before applying the LLM
diff. This
intentionally avoids Agent Server 1.62's profile persistence paths, which
return HTTP 500 with an encrypted provider reference or a plaintext draft key
when `OH_SECRET_KEY` is enabled.
The saved SDK model identifier is `litellm_proxy/local-code` and its compatible
base URL is `http://127.0.0.1:14000/v1`; LiteLLM still routes the public model
alias `local-code` to Ollama. The command writes the credential, base URL, and
model in that order so every intermediate persisted LLM remains valid.
OpenHands normally rejects contexts below 16,384 tokens. The qualified
`local-code` route accurately advertises 14,336, so its protected service
environment sets the SDK's documented `ALLOW_SHORT_CONTEXT_WINDOWS=true`
override instead of overstating the model's capacity.
Expose it through Tailscale Serve only after verifying authentication. Verify
that every conversation is a rootless container before enabling GitHub
automations. Never mount the rootful socket.

## Switch gate

`compose/switch.yml` is localized from the released 0.32.1 standalone contract.
It retains separate schema-owner and runtime database roles so row-level
security is effective. Before its first start:

1. Compare it to the release's standalone Compose artifact.
2. Choose the permanent server name.
3. Generate all secrets and set `SWITCH_GATEWAY_PUBLIC_URL` to the tailnet HTTPS
   URL.
4. Install Switch Console 0.41.1 on the administrative workstation.
5. Create a new Discord application; the legacy token stays active for rollback.

Do not start Switch merely to see whether the configuration works. First-start
identity choices are persistent.

Install the checksum-verified OpenCode 2.2.7 standalone Linux release for the
dedicated agent identity with:

```bash
sudo scripts/install-opencode-agent.sh --apply --env-file .env
sudo scripts/run-model-qualification.sh --apply
```

The installer copies `config/opencode/opencode.jsonc` and writes
`LITELLM_API_KEY` to a mode-0600 agent-only environment file. OpenCode's
documented custom-provider runtime sends `local-code` and `local-fast` to the
loopback-only LiteLLM listener. The qualification wrapper runs as `ai-agent`
and retains its evidence under the dedicated qualification workspace.

## Primary sources

- https://github.com/sandbox-quantum/switch/releases/tag/switch-v0.32.1
- https://github.com/sandbox-quantum/switch/blob/switch-v0.32.1/RELEASING.md
- https://github.com/OpenHands/OpenHands/blob/v1.41.0/README.md
- https://github.com/OpenHands/OpenHands/blob/v1.41.0/docs/SELF_HOSTING.md
- https://docs.ollama.com/docker
- https://docs.ollama.com/faq
- https://docs.litellm.ai/docs/proxy/reliability
- https://docs.openwebui.com/getting-started/quick-start/
- https://docs.openwebui.com/getting-started/advanced-topics/hardening/
- https://docs.searxng.org/admin/installation-docker.html
- https://tailscale.com/docs/solutions/host-private-server-docker
- https://caddyserver.com/docs/caddyfile/directives/reverse_proxy
- https://opencode.ai/docs/providers
- https://opencode.ai/docs/cli/commands

