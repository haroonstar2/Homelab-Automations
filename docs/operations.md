# Operations

## Routine checks

- `docker compose ... ps` reports every intended service healthy.
- `scripts/smoke-test.sh` passes with credentials loaded from the server-side
  environment file.
- Rootless agent containers appear only in the `ai-agent` daemon; the rootful
  daemon contains no agent workspace containers.
- Logs contain no tokens, authorization headers, prompts, or repository secrets.
- Disk, memory, swap, model latency, queue depth, and failed requests remain
  within the recorded baseline.

## Failure truthfulness

Never translate an unknown or failed command outcome into success. A task is
passing only when the branch, commit, push, checks, and draft pull request are
observable. Preserve task state after a failure so a human can resume it.

## Backups

Phase 0 created an owner-only server backup, recorded in `migration.md`. The
final platform uses Restic. `scripts/backup.sh` requires an external repository
and password file; neither belongs in Git. Run `scripts/restore-test.sh` into a
disposable location on a schedule and retain its output.

Back up configuration, Switch state/database, OpenHands state, Open WebUI data,
and deterministic n8n workflows. Workspaces and Ollama model blobs may be
recreated and are excluded unless a separate retention decision says otherwise.

## Updates

Renovate may open pinned update pull requests. It must not deploy them. Review
release notes, validate Compose, scan images/configuration, run smoke and
acceptance tests, and retain a rollback image before applying an update.

## Incident rollback

Before cutover, rollback means stop only the new platform. After cutover it
means stop Switch/OpenHands, re-enable the preserved legacy Discord token, and
start the legacy stack from `legacy/devbot-final-20261118` plus the verified
server backup. Never run both bots with the same token.

