# Cutover acceptance checklist

Record evidence, timestamps, actor identities, repository, branch, commit, and
pull-request URLs for every case. A checkbox without evidence is not a pass.

- [ ] Intended Discord agent receives the message.
- [ ] Non-owner Discord account cannot instruct owner-only agents.
- [ ] Switch context survives service and host restart.
- [ ] Agent accesses only its approved workspace.
- [ ] Execution container cannot read unrelated host files or secrets.
- [ ] Execution container cannot access rootful Docker.
- [ ] Repository symlinks cannot escape the workspace boundary.
- [ ] Protected verification inputs cannot be changed unnoticed.
- [ ] Failed or incomplete tests prevent a success report.
- [ ] Success produces the expected branch, commit, push, and draft PR.
- [ ] Push or PR failure is reported as failure and remains resumable.
- [ ] GitHub issue triggers OpenHands and produces a tested draft PR.
- [ ] Follow-up feedback resumes the correct task.
- [ ] Cancellation leaves consistent resumable state.
- [ ] Secrets are absent from URLs, commits, logs, prompts, traces, artifacts.
- [ ] Local and strong-model profiles are independently selectable.
- [ ] Truncation and context-limit failures are explicit.
- [ ] Uptime Kuma detects a stopped critical service.
- [ ] Server reboot restores only the intended platform automatically.
- [ ] Encrypted backup restores into a disposable environment.

