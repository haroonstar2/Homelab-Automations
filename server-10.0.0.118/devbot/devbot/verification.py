"""Required checks configured by the owner outside model-editable repositories."""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VerificationReport:
    output: str
    passed: bool
    checks: tuple[dict, ...]

    def __iter__(self):
        yield self.output
        yield self.passed

    def __getitem__(self, index):
        return (self.output, self.passed)[index]

    def as_dict(self) -> dict:
        return {"passed": self.passed, "checks": list(self.checks), "output": self.output}

    def failure_summary(self, max_chars: int = 900) -> str:
        failed = next((check for check in self.checks if check["status"] != "PASS"), None)
        if not failed:
            return self.output[-max_chars:]
        heading = f"{failed['component']}: {failed['status']} (exit {failed['exit_code']})\n"
        output_budget = max(0, max_chars - len(heading))
        return heading + failed["output"][-output_budget:]


def load_checks(policy_file: Path, repo_full: str, repo_dir: Path) -> list[dict]:
    policy = json.loads(policy_file.read_text(encoding="utf-8"))
    checks = policy.get(repo_full, {}).get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError(f"No required checks configured for {repo_full}")
    result = []
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError("Each check must be an object")
        argv = check.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise ValueError("Check argv must be a nonempty list of strings (no shell)")
        cwd = (repo_dir / check.get("cwd", ".")).resolve()
        if not cwd.is_relative_to(repo_dir.resolve()) or not cwd.is_dir():
            raise ValueError("Check cwd must be an existing directory inside the repository")
        timeout = check.get("timeout", 300)
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise ValueError("Check timeout must be 1..3600 seconds")
        configured_env = check.get("env", {})
        if not isinstance(configured_env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in configured_env.items()
        ):
            raise ValueError("Check env must be an object of literal string values")
        safe_names = ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC", "TEMP", "TMP")
        clean_env = {name: os.environ[name] for name in safe_names if name in os.environ}
        clean_env.update(configured_env)
        component = check.get("component") or cwd.name or argv[0]
        if not isinstance(component, str) or not component.strip():
            raise ValueError("Check component must be a nonempty string")
        result.append({
            "argv": argv, "cwd": cwd, "timeout": timeout, "env": clean_env,
            "component": component.strip(),
        })
    return result


async def verify(policy_file: Path, repo_full: str, repo_dir: Path, run_cmd) -> VerificationReport:
    try:
        checks = load_checks(policy_file, repo_full, repo_dir)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        output = f"Verification NOT RUN: {exc}"
        return VerificationReport(output, False, ({
            "component": "configuration", "argv": [], "status": "NOT RUN",
            "exit_code": None, "duration_seconds": 0.0, "output": str(exc),
        },))
    notes = []
    results = []
    for check in checks:
        label = json.dumps(check["argv"])
        started = time.monotonic()
        try:
            code, out = await run_cmd(
                check["argv"], cwd=check["cwd"], timeout=check["timeout"], env=check["env"]
            )
        except OSError as exc:
            duration = round(time.monotonic() - started, 3)
            notes.append(f"[{check['component']}] {label}: NOT RUN\n{exc}")
            results.append({
                "component": check["component"], "argv": check["argv"], "status": "NOT RUN",
                "exit_code": None, "duration_seconds": duration, "output": str(exc),
            })
            return VerificationReport("\n".join(notes), False, tuple(results))
        duration = round(time.monotonic() - started, 3)
        status = "PASS" if code == 0 else "FAIL"
        notes.append(f"[{check['component']}] {label}: {status} (exit {code}, {duration:.3f}s)\n{out}")
        results.append({
            "component": check["component"], "argv": check["argv"], "status": status,
            "exit_code": code, "duration_seconds": duration, "output": out,
        })
        if code:
            return VerificationReport("\n".join(notes), False, tuple(results))
    return VerificationReport("\n".join(notes), True, tuple(results))
