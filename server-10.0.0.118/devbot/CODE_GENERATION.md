# Coding runner

The Discord bot orchestrates planning, approval, verification and publication. Aider 0.86.2
performs repository mapping and SEARCH/REPLACE edits in a subprocess. There is no automatic
fallback to full-file model output. A successful model invocation is not a successful task:
the owner's required repository checks must pass before publication.

## Required verification policy

Copy `project-checks.example.json` to `/data/project-checks.json` (the DATA_DIR volume), and
replace the example repository key and commands with commands verified for your project.
The file lives outside the model-editable checkout. Every listed command is required, runs
without a shell and runs regardless of whether changes have already been committed.
Missing policy, invalid configuration, unavailable tools or a nonzero command exit blocks publication.
Include dependency setup explicitly when required. No package manager is guessed or run implicitly.
The policy's `image` must use an immutable `sha256:...` local image ID or `name@sha256:...` repository
digest and contain every tool invoked by its checks.
Each repository policy may also define `protected_paths`. Files at or below those paths cannot appear in
an approved model-edit scope, which keeps tests, build scripts, and verification configuration independent
from the generated change.

Every `/code` and `/continue` request requires an explicit comma-separated path scope. The owner sees that
scope with the plan before approval. Aider validates the whole edit response before applying it, and the
runner independently compares the final changed paths with the same contract before committing.

## Verification isolation

Checks run through the host user's rootless Docker daemon, never in the bot container. Each check receives
only its literal policy environment and its current task checkout at `/workspace`. It has no network,
Linux capabilities, or privilege escalation; its root filesystem is read-only, temporary storage is
bounded, and CPU, memory, process count, and wall time are limited. The bot refuses a daemon that does not
report rootless mode. Do not substitute `/var/run/docker.sock`.

After installing rootless Docker for uid 1000 and deploying, run the adversarial live probe with the same
digest-pinned Python image used by the policy:

`docker compose exec devbot python -m scripts.verify_isolation python@sha256:<digest>`

It succeeds only when bot environment secrets, sibling task data, root-filesystem writes, and external
network access are all denied.

## Harness choice

Aider supplies repository maps and tested patch parsing, avoiding a home-built whole-file editor.
Costs: a larger dependency graph, model-specific limitations and an API bridge that must track its version.
The stock CLI's one-shot path can return zero after model errors, so the bridge uses the pinned Python
API and surfaces tool errors, malformed responses and context exhaustion as failure. The API is not
upstream-stable; upgrading Aider requires running integration tests against its actual source.
The bot owns commits and publication; Aider auto-commits and automatic shell suggestions are disabled.

## Local tests

From this directory, with requirements and pytest installed: `python -m pytest tests -q`.
Tests include actual Aider patch application in a temporary Git repository, without model inference.
Live server/model integration must be checked separately; these tests do not measure model quality.
