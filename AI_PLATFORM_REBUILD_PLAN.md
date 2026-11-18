# AI Platform In-Place Rebuild Plan

## Handoff instructions for a new chat

This file is the authoritative plan for replacing the custom `devbot` project.

**The existing repository must be remade in place. Do not create a new repository.** Preserve this repository's Git history and remote, establish a recoverable legacy snapshot, and then replace the old application and layout on the current default branch with version-pinned infrastructure for Switch, OpenHands, Ollama, and the supporting services described below.

Before changing anything, the implementing agent must:

1. Read this entire file.
2. Inspect the current worktree and recent Git history. Do not discard or overwrite uncommitted changes.
3. Inspect the actual server state over `ssh haroonstar2@10.0.0.118`.
4. Read the current official deployment documentation and released configuration schemas for every third-party service before writing Compose files or configuration.
5. Make a recoverable legacy snapshot in this same repository, such as a `legacy/devbot-final` branch and an annotated tag, after resolving any uncommitted work with the user.
6. Implement and verify one phase at a time. Do not stop the live `devbot` until the replacement passes the cutover acceptance tests.

## Decision

Replace the bespoke Discord coding bot with established platforms:

- **Switch Rooms** for Discord rooms, identity, shared context, permissions, approvals, and interactive coding-agent coordination.
- **OpenHands Agent Canvas** for isolated coding workspaces, long-running autonomous work, GitHub issue-to-PR automation, PR review, and CI repair.
- **Ollama** for private local inference.
- **LiteLLM** for stable model aliases, routing, fallback, timeouts, and optional cloud-provider access.
- **Open WebUI** for ordinary chat, document knowledge, and model experimentation.
- **SearXNG** for private web search.

The repository will own deployment configuration, security policy, integration configuration, runbooks, backups, smoke tests, and acceptance tests. It will no longer own a custom coding-agent implementation.

## Integration constraint

Switch does not currently document OpenHands as a native agent provider. Its documented providers include Claude Code, OpenCode, Codex, Cursor, and Antigravity. Do not invent an unsupported direct integration.

Initial design:

```text
Discord
   |
   v
Switch Rooms
   |-- local-coder --> OpenCode --> LiteLLM --> Ollama
   |-- strong-coder -> Codex CLI or another qualified frontier provider
   `-- GitHub connector
                          |
GitHub issues and PRs ----+
                          v
                  OpenHands Agent Canvas
                  |-- isolated workspaces
                  |-- issue-to-draft-PR automation
                  |-- PR review and CI repair
                  `-- local and optional cloud model profiles
```

GitHub issues, branches, checks, and pull requests are the initial interoperability boundary between Switch and OpenHands. A custom Switch/OpenHands bridge is explicitly deferred until the platform works and evidence shows that a bridge is necessary.

## Existing environment baseline

Last verified on 2026-11-18:

- Main server: `haroonstar2@10.0.0.118` (`haroonpcserver`)
- CPU: Intel Core i7-8700, 6 cores / 12 threads
- RAM: 32 GiB
- System disk: 116 GiB with approximately 42 GiB free
- Data disk: `/mnt/Storage1`, approximately 1.6 TiB free
- Graphics: Intel UHD 630 and Radeon RX 550-class hardware
- Rootful Docker is active and hosts the existing stack.
- Rootless Docker is active for the server user but had no running containers when inspected.
- The legacy `devbot`, Ollama, LiteLLM, and Postgres were still running.
- The local hardware is suitable for orchestration but not fast, concurrent large-model inference. Default heavy-model concurrency must remain one until measurement proves otherwise.

Re-check all of this before implementation because live state may have changed.

## Target repository layout

Remake this repository toward the following layout. Reuse the existing `server-10.0.0.118` and `pi-10.0.0.117` directories where that makes operational ownership clearer; do not create a second repository.

```text
IDKServer/
|-- compose/
|   |-- core.yml
|   |-- switch.yml
|   |-- openhands.yml
|   |-- observability.yml
|   `-- automation.yml
|-- config/
|   |-- litellm/
|   |-- caddy/
|   |-- langfuse/
|   `-- monitoring/
|-- policies/
|   |-- repository-access.md
|   |-- agent-network-policy.md
|   `-- model-qualification.md
|-- scripts/
|   |-- bootstrap-server.sh
|   |-- backup.sh
|   |-- restore-test.sh
|   `-- smoke-test.sh
|-- tests/
|   |-- isolation/
|   `-- end-to-end/
|-- docs/
|   |-- architecture.md
|   |-- deployment.md
|   |-- operations.md
|   `-- migration.md
|-- pi-10.0.0.117/
|-- server-10.0.0.118/
|-- .env.example
|-- AI_PLATFORM_REBUILD_PLAN.md
`-- README.md
```

The exact layout may be adjusted when the official released Switch and OpenHands deployment artifacts are inspected. Avoid copying or modifying upstream Compose bundles blindly; document every local override.

## Supporting tools

Adopt after the core path works:

- **Caddy** for named internal HTTPS endpoints and reverse proxying.
- **Tailscale** for authenticated remote access without public exposure.
- **Langfuse** for LLM traces, latency, token usage, and failure analysis.
- **n8n** only for deterministic Notion, journaling, check-in, wrap-up, and scheduled personal workflows.
- **Uptime Kuma** on the Raspberry Pi for service monitoring.
- **Restic** for encrypted, restorable backups.
- **Renovate** for controlled dependency and image update pull requests.
- **Trivy** and **Gitleaks** in CI for image/configuration scanning and secret detection.

Do not add Kubernetes, another vector database, multiple workflow engines, automatic production deployment, or arbitrary community MCP servers during the initial rebuild.

## Security model

1. Treat every coding agent and repository as untrusted.
2. Run agent execution through a dedicated Linux identity and the dedicated rootless Docker daemon.
3. Never mount the rootful Docker socket into Switch, OpenHands, or an agent workspace.
4. Mount only explicit workspace roots, not `/home`, `/mnt/Storage1`, or the repository collection as a whole.
5. Keep Ollama, LiteLLM, Postgres, Redis, and administrative APIs off unrestricted LAN interfaces.
6. Expose user interfaces through authenticated Caddy/Tailscale routes.
7. Use a GitHub App or fine-grained credentials limited to selected repositories and minimum permissions.
8. Default all autonomous publication to draft pull requests. Protect default branches.
9. Store secrets outside Git. Prefer SOPS/age or supported container secrets; otherwise use server-side files with restrictive ownership and permissions.
10. Pin all container images, Compose bundles, actions, and important dependencies to released versions or immutable digests.
11. Limit agent egress to required GitHub and package-registry traffic where practical, and document every allowed destination.
12. Never report success when verification, push, or pull-request creation failed.

## Implementation phases

### Phase 0: Freeze and preserve the existing repository

1. Inspect `git status`, the current branch, remotes, and recent commits.
2. Resolve existing uncommitted changes without discarding them.
3. Create a recoverable legacy branch and annotated tag in this repository.
4. Back up the current server Compose files, `.env` secrets separately, Postgres data, `/mnt/Storage1/devbot`, and any Notion-related state.
5. Record the exact running images, versions, mounts, ports, Docker contexts, and systemd services.
6. Verify that the backup can be read before modifying the live deployment.

Gate: the current application can be restored from Git plus the encrypted/server backup.

### Phase 1: Establish the platform foundation

1. Create dedicated persistent directories under `/mnt/Storage1/ai-platform/` for Switch, OpenHands, workspaces, model data, observability, and backups.
2. Create dedicated service and agent-execution identities.
3. Verify the rootless Docker socket and ensure it cannot control rootful Docker.
4. Define internal Docker networks and eliminate unnecessary host port publication.
5. Deploy Caddy/Tailscale routing using official configuration patterns.
6. Add health checks, log rotation, resource limits, and restart behavior.

Gate: a disposable container launched through the agent runtime cannot read unrelated host files, access the rootful Docker socket, or write outside its workspace.

### Phase 2: Rebuild the model plane

1. Preserve and reuse the existing Ollama model volume where compatible.
2. Version-pin Ollama and LiteLLM.
3. Replace ambiguous `smart` and `fast` aliases with explicit profiles such as `local-code`, `local-fast`, `local-embed`, and optional `frontier-code`.
4. Configure one concurrent heavy local request initially.
5. Build model qualification cases covering multi-file edits, existing-code discovery, test repair, tool calling, long-context retention, truncated output, and refusal to claim false success.
6. Benchmark correctness and end-to-end task success, not only tokens per second.
7. Add an optional strong-model route for tasks the local model cannot reliably complete.

Gate: no model becomes the default coder until it passes the written qualification suite.

### Phase 3: Deploy OpenHands

1. Read the current released Agent Canvas installation, runtime, authentication, GitHub, and automation documentation.
2. Deploy a pinned Agent Canvas release with persistent settings and workspaces on the data disk.
3. Keep its UI and backend private until authentication/reverse-proxy behavior is verified.
4. Configure per-conversation Docker isolation through the dedicated rootless runtime.
5. Add a local LiteLLM/Ollama profile and an optional qualified strong-model profile.
6. Connect a repository-scoped GitHub App.
7. Configure issue-to-draft-PR, PR review, and failing-CI repair workflows one at a time.
8. Verify restart recovery and truthful failure reporting for each workflow.

Gate: a GitHub issue in a disposable repository produces an isolated, tested draft PR without exposing host secrets or unrelated repositories.

### Phase 4: Deploy Switch and Discord

1. Read the current released Switch self-hosting, server naming, Discord, GitHub connector, remote-host, agent-provider, and security documentation.
2. Pin a Switch server release. Do not use the development branch or mutable image tags.
3. Choose the permanent Switch server name before first start.
4. Deploy its official supported stack and database using documented overrides.
5. Install Switch Console on the administrative workstation and connect it to the server.
6. Create a new Discord bot application for Switch so the legacy bot remains available for rollback.
7. Connect and verify Discord identity linking, rooms, threads, attachments, slash commands, and permission boundaries.
8. Create `local-coder` using OpenCode routed through LiteLLM/Ollama.
9. Create `strong-coder` using Codex CLI or another explicitly qualified provider.
10. Onboard the server as a remote host and give each agent only an explicit working directory.
11. Connect GitHub and Notion with minimum scopes.

Gate: a Discord request persists across restart, reaches only the addressed agent, operates in the allowed repository, and produces an accurately reported draft PR.

### Phase 5: Replace legacy personal-assistant features

Use established tools instead of recreating scripts:

| Legacy feature | Replacement |
| --- | --- |
| `/code`, `/fix`, `/continue` | Switch interactive coder or OpenHands workspace |
| `/review` | OpenHands PR-review automation |
| `/ask` | Switch room or Open WebUI |
| `/doc`, `/docs` | Open WebUI knowledge collections |
| `/research` | Switch/Open WebUI with SearXNG |
| `/note`, `/journal`, `/log` | n8n to Notion |
| `/checkin`, `/wrapup`, `/retro` | n8n schedules reading GitHub and Notion |
| `/resume` | Switch room history, OpenHands conversations, and GitHub state |
| `/status` | Uptime Kuma and platform health checks |

n8n may implement deterministic schedules and integrations only. It must not become another coding-agent framework.

### Phase 6: Observability, maintenance, and recovery

1. Add Langfuse only after model calls work correctly without it.
2. Add Uptime Kuma checks for all critical internal and user-facing endpoints.
3. Add structured logs with redaction and retention limits.
4. Add Restic backups for configuration, Switch state, OpenHands state, and required database volumes.
5. Perform a real restore into a disposable location.
6. Add CI for Compose validation, configuration linting, secret scanning, Trivy scanning, and smoke tests.
7. Configure Renovate to open reviewed update PRs rather than updating automatically.

Gate: the platform can be restored and its critical workflows revalidated from documented procedures.

### Phase 7: Cut over and remove the legacy application

1. Run every acceptance test below against the replacement.
2. Announce a short Discord maintenance window.
3. Disable the legacy bot token and stop only the legacy `devbot` and Aider-runner services.
4. Activate the Switch Discord connection.
5. Run one low-risk real repository task through the local agent, strong agent, and OpenHands automation.
6. Keep legacy data read-only for a documented rollback window.
7. After the window, remove legacy application code, tests, images, and obsolete Compose services from this existing repository and the server.
8. Retain the legacy Git branch/tag and encrypted backup.
9. Rewrite `README.md` to describe only the new platform and its operational procedures.

Rollback: stop Switch/OpenHands, re-enable the legacy Discord token, and start the preserved legacy stack using the documented backup.

## Acceptance tests required before cutover

1. A Discord message reaches the intended named agent.
2. A different Discord account cannot instruct owner-only agents.
3. Conversation context survives a Switch or host restart.
4. The agent can access only an approved repository/workspace.
5. An execution container cannot read unrelated server files or secret stores.
6. It cannot access the rootful Docker socket.
7. Repository symlinks cannot escape the workspace boundary.
8. Generated changes cannot modify protected verification inputs unnoticed.
9. Failed or incomplete tests prevent a success report and publication as passing work.
10. Successful work produces the expected branch, commit, push, and draft PR.
11. Push or PR-creation failure is reported as failure and remains recoverable.
12. A GitHub issue triggers OpenHands and produces a tested draft PR.
13. Follow-up feedback continues the correct task instead of silently starting over.
14. Cancellation leaves a consistent, resumable state.
15. Secrets do not appear in Git URLs, commits, logs, prompts, traces, or artifacts.
16. Local and strong-model profiles can be selected independently.
17. Model truncation and context-limit failures are detected explicitly.
18. Uptime Kuma detects stopped critical services.
19. A server reboot restores the full intended platform automatically.
20. An encrypted backup restores successfully into a disposable environment.

## Scope discipline

- Implement one phase and one integration at a time.
- Verify actual installed versions, schemas, CLI help, and official documentation before using flags or fields.
- Keep the live legacy bot available until cutover criteria pass.
- Preserve unrelated homelab services such as Jellyfin, Pi-hole, Uptime Kuma, databases used by other projects, and game servers.
- Do not delete server backups or legacy data merely because the repository has been remade.
- Do not expose administrative or model APIs directly to the public internet.
- Do not build custom functionality when an established, maintainable tool already provides it.

## Definition of done

The rebuild is complete when this same repository contains only the maintained configuration, policies, automation, tests, and documentation needed to operate the new platform; Discord-to-agent and GitHub-to-OpenHands workflows pass the acceptance suite; backups are proven restorable; the legacy bot is no longer running; and rollback remains possible through the preserved legacy Git snapshot and encrypted backup.
