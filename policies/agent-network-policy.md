# Agent network policy

Agent workspaces have no unrestricted route to the LAN or host administration
interfaces. The initial allowlist is:

- GitHub HTTPS and Git endpoints required for the selected repository.
- Explicit package registries needed by that repository.
- The internal LiteLLM endpoint for the selected model profile.
- DNS and time services required by the runtime.

Explicitly denied:

- `/var/run/docker.sock` and every rootful Docker API endpoint.
- Ollama, Postgres, Redis, Caddy admin, and host management APIs.
- RFC1918 and link-local destinations except named platform endpoints granted
  for the task.
- Cloud metadata endpoints and arbitrary MCP servers.

Rootless Docker is an isolation boundary from rootful Docker, not a complete
network sandbox. Egress enforcement remains a deployment gate until the
isolation tests demonstrate the allowlist on the actual server.

