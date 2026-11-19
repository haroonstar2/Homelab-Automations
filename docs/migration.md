# Migration record

## Phase status

| Phase | Status | Gate |
| --- | --- | --- |
| 0 Preserve legacy | Complete | Remote Git snapshot, encrypted Restic snapshot, and byte-for-byte restore verified |
| 1 Foundation | Deployed | Dedicated identities, storage, and rootless boundary verified |
| 2 Model plane | Deployed | Both written qualification cases passed with retained evidence |
| 3 OpenHands | Deployed; isolation passed | Disposable GitHub issue-to-draft-PR remains |
| 4 Switch/Discord | Deployed; private HTTPS verified | `switch.home`, Switch Console 0.41.1 installed; Console sign-in, new Discord app, agents, and permission gate remain |
| 5 Personal workflows | Not started | Core path must pass first |
| 6 Recovery/observability | Not started | Restic restore and Uptime Kuma detection required |
| 7 Cutover | Forbidden | All 20 acceptance tests must pass |

## Git preservation

- Legacy commit: `affb707`
- Branch: `legacy/devbot-final`
- Annotated tag: `legacy-devbot-final-20261118`
- `main` was two commits ahead of `origin/main` when preserved.

The branch and tag were pushed to the existing GitHub origin and verified with
`git ls-remote`. `origin/main` was not changed. Do not delete either reference.

## Server baseline (2026-11-18)

- Host: `haroonpcserver`, Ubuntu kernel 6.8.0-149, 32 GiB RAM.
- Root disk: 116 GiB total, 42 GiB free.
- Data disk: 1.8 TiB total, about 1.6 TiB free.
- Rootless Docker for uid 1000 was active with no containers.
- Rootful Docker ran devbot, Ollama, LiteLLM, Open WebUI, SearXNG, Postgres,
  Redis, Adminer, code-server, and unrelated homelab services.
- Legacy Ollama, Postgres, Redis, Adminer, SearXNG, Open WebUI, and code-server
  published LAN ports. Those exposures remain unchanged until staged migration.
- The legacy `.env` was mode 0664. The replacement requires 0600.

Unrelated Jellyfin, Portainer, game, Nextcloud, and network services are outside
this migration and must not be removed or reconfigured.

## Phase 0 backup

Path:

`/mnt/Storage1/ai-platform/backups/legacy-devbot-20261118T1430Z`

The 520 MiB owner-only backup contains:

- Legacy Compose, LiteLLM, and SearXNG configuration.
- The real `.env` in a separate mode-0600 secrets directory.
- Logical `pg_dumpall` output covering `devbot`, `saas_dev`, and `postgres`.
- `/mnt/Storage1/devbot` data and n8n state.
- Container, image, mount, Docker-context, and systemd inventories.
- A SHA-256 manifest.

Every checksum, gzip stream, and tar archive was verified readable. Restic
0.19.1 was installed from its checksum-verified official release and initialized
at `/mnt/Storage1/ai-platform/restic`. Encrypted snapshot `6bab79b7` passed a
100% pack read. It was restored to
`/mnt/Storage1/ai-platform/restore-tests/restic-phase0-20261118`; every file
matched the source byte-for-byte and all nested archives passed integrity tests.
The password file is separate at
`/home/haroonstar2/.config/ai-platform/restic-password` with mode 0600.

This local encrypted repository protects contents at rest but is not an offsite
copy. Phase 6 still requires a second failure-domain backup.

## OpenHands isolation evidence (2026-11-19)

Agent Canvas 1.41.0 and Agent Server 1.62.0 run under `ai-agent` with its
dedicated rootless Docker daemon. The live disposable-conversation gate proved:

- the runtime container executes as non-root uid 1002;
- neither rootless nor rootful Docker sockets are mounted into the container;
- a workspace symlink to `/etc` resolves to the container filesystem, not host
  `/etc`;
- deleting the conversation removes its runtime container.

Agent Server 1.62.0 creates mode-0700 bind directories and passes the host uid
to `docker run`, which rootless Docker maps to a subordinate uid. The maintained,
version-checked compatibility patch grants only that mapped uid access after
directory creation and refuses to apply to a different Agent Server version.
The remaining Phase 3 gate is a disposable GitHub issue producing a tested
draft pull request without access to unrelated repositories or host secrets.


