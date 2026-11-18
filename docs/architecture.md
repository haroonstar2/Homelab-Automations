# Architecture

The replacement is a set of maintained platforms joined by explicit, narrow
boundaries. It is not another custom coding bot.

```text
Discord -> Switch Rooms -> OpenCode/Codex -> LiteLLM -> Ollama or qualified cloud model
                  |
                  +-> GitHub issues, branches, checks, draft pull requests
                                      |
                                      v
                             OpenHands Agent Canvas
```

Switch and OpenHands do not have a documented native integration. GitHub is the
initial interoperability boundary. No custom bridge is authorized until the
platform is operating and evidence shows one is necessary.

## Trust boundaries

- Rootful Docker runs platform services only.
- `ai-agent` owns workspaces and a dedicated rootless Docker daemon. It is not
  a member of the `docker` group and cannot access `/var/run/docker.sock`.
- Model and database networks are internal Docker networks. Ollama, LiteLLM,
  Postgres, and SearXNG publish no host ports.
- Caddy binds loopback only. Tailscale Serve provides tailnet HTTPS and access
  control. Application authentication remains enabled as defense in depth.
- Every agent receives one explicit workspace. No broad home or data-disk mount
  is permitted.

Heavy local model concurrency is one until qualification data proves the host
can safely support more.

