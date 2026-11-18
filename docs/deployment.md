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
3. Copy `.env.example` to a server-side `.env`, set mode `0600`, generate every
   required secret, and choose `SWITCH_SERVER_NAME` permanently.
4. Keep the legacy stack running. Do not reuse its Discord application.
5. Validate with `docker compose --env-file .env -f compose/<file>.yml config`.

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
agent-canvas --public
```

Install exactly `@openhands/agent-canvas@1.41.0` with Node.js 24 and manage it
with a user systemd unit. Verify that every conversation is a rootless container
before enabling GitHub automations. Never mount the rootful socket.

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

Install exactly `@opencode/cli@2.2.7` for `local-coder`, copy
`config/opencode/opencode.jsonc` to the dedicated agent identity, and expose the
master key as `LITELLM_API_KEY` from a mode-0600 environment file. OpenCode's
documented custom-provider runtime sends `local-code` and `local-fast` to the
loopback-only LiteLLM listener.

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

