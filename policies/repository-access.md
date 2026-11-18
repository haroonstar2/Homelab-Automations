# Repository access policy

Coding agents are untrusted tenants. Access is deny-by-default.

1. An agent receives one explicit workspace path for one task. Never mount a
   home directory, `/mnt/Storage1`, or a directory containing multiple unrelated
   repositories.
2. Repository credentials must be a GitHub App installation token or a
   fine-grained token limited to selected repositories. Default permissions are
   Contents read/write, Pull requests read/write, and Metadata read.
3. Default branches are protected. Autonomous work publishes only draft pull
   requests from task-specific branches.
4. Verification inputs, CI workflows, ownership files, and this policy are
   protected paths. A change to them requires separate human review and cannot
   make the same task's verification pass.
5. Git remote URLs never contain credentials. Tokens are injected through a
   credential helper or request header and redacted from logs.
6. A task is successful only after its expected commit, push, checks, and draft
   pull request are independently observed. Any failed step is reported as a
   failure with resumable state.

The allowlist for each agent belongs in server-side configuration outside Git.

