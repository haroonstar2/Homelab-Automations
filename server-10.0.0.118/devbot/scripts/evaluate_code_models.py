#!/usr/bin/env python3
"""Grade models on small repository edits through devbot's real Aider worker.

This complements benchmark_models.py, which measures throughput only. Aider's
official benchmark is broader and better for comparing general coding ability,
but is expensive and does not exercise this bot's prompt, token limits, or
worker bridge. These deliberately small cases are a local release gate, not a
claim of general model quality.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_ROOT))

from devbot.prompts import CODE_STYLE_CONVENTIONS  # noqa: E402
from devbot.runner import select_target_files  # noqa: E402


@dataclass(frozen=True)
class EvaluationCase:
    name: str
    task: str
    files: dict[str, str]
    acceptance: str
    allowed_changes: frozenset[str]


CASES = (
    EvaluationCase(
        name="bounded_clamp",
        task=(
            "In calc.py, change clamp(value, low, high) so values below low return low, "
            "values above high return high, and in-range values are unchanged. Reject low > high "
            "with ValueError. Preserve the public API."
        ),
        files={
            "calc.py": "def clamp(value: int, low: int, high: int) -> int:\n    return value\n",
            "test_public.py": (
                "import unittest\nfrom calc import clamp\n\n"
                "class PublicTests(unittest.TestCase):\n"
                "    def test_in_range(self):\n        self.assertEqual(clamp(5, 0, 10), 5)\n"
            ),
        },
        acceptance=(
            "import sys, unittest\nsys.path.insert(0, sys.argv[1])\nfrom calc import clamp\n\n"
            "class HiddenTests(unittest.TestCase):\n"
            "    def test_bounds(self):\n"
            "        self.assertEqual(clamp(-2, 0, 10), 0)\n"
            "        self.assertEqual(clamp(12, 0, 10), 10)\n"
            "    def test_invalid(self):\n"
            "        with self.assertRaises(ValueError):\n            clamp(1, 3, 2)\n\n"
            "unittest.main(argv=[sys.argv[0]])\n"
        ),
        allowed_changes=frozenset({"calc.py"}),
    ),
    EvaluationCase(
        name="cross_file_display_name",
        task=(
            "Add user_profile.display_name(raw) that trims surrounding whitespace and returns 'Anonymous' "
            "when the result is empty. Update greeting.py to use it. Do not change tests."
        ),
        files={
            "user_profile.py": "\"\"\"User profile formatting.\"\"\"\n",
            "greeting.py": "def greeting(raw: str) -> str:\n    return f\"Hello, {raw}!\"\n",
            "test_public.py": (
                "import unittest\nfrom greeting import greeting\n\n"
                "class PublicTests(unittest.TestCase):\n"
                "    def test_name(self):\n        self.assertEqual(greeting('Ada'), 'Hello, Ada!')\n"
            ),
        },
        acceptance=(
            "import sys, unittest\nsys.path.insert(0, sys.argv[1])\n"
            "from greeting import greeting\nfrom user_profile import display_name\n\n"
            "class HiddenTests(unittest.TestCase):\n"
            "    def test_trim_and_empty(self):\n"
            "        self.assertEqual(display_name('  Ada  '), 'Ada')\n"
            "        self.assertEqual(greeting('   '), 'Hello, Anonymous!')\n\n"
            "unittest.main(argv=[sys.argv[0]])\n"
        ),
        allowed_changes=frozenset({"user_profile.py", "greeting.py"}),
    ),
)


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=1800, check=False)
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return subprocess.CompletedProcess(command, 124, output + "\nTimed out after 1800 seconds")


def changed_paths(repo: Path) -> set[str]:
    tracked = run(["git", "diff", "--name-only"], repo).stdout.splitlines()
    untracked = run(["git", "ls-files", "--others", "--exclude-standard"], repo).stdout.splitlines()
    return {item.replace("\\", "/") for item in (*tracked, *untracked) if item.strip()}


def score_result(worker_exit: int, acceptance_exit: int, changed: set[str], allowed: frozenset[str]) -> dict:
    unexpected = sorted(changed - allowed)
    return {
        "passed": worker_exit == 0 and acceptance_exit == 0 and bool(changed) and not unexpected,
        "worker_succeeded": worker_exit == 0,
        "acceptance_passed": acceptance_exit == 0,
        "changed_files": sorted(changed),
        "unexpected_files": unexpected,
    }


def prepare_case(case: EvaluationCase, root: Path) -> tuple[Path, Path]:
    repo = root / "repo"
    repo.mkdir(parents=True)
    for name, content in case.files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    acceptance = root / "acceptance.py"
    acceptance.write_text(case.acceptance, encoding="utf-8")
    for command in (["git", "init", "-q"], ["git", "config", "user.name", "evaluator"],
                    ["git", "config", "user.email", "evaluator@local"], ["git", "add", "."],
                    ["git", "commit", "-qm", "fixture"]):
        completed = run(command, repo)
        if completed.returncode:
            raise RuntimeError(completed.stdout)
    return repo, acceptance


def evaluate_case(case: EvaluationCase, model: str, base_url: str, api_key: str, output: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"devbot-eval-{case.name}-") as temporary:
        repo, acceptance = prepare_case(case, Path(temporary))
        safe_model = re.sub(r"[^A-Za-z0-9_.-]+", "_", model)
        case_output = output / safe_model / case.name
        case_output.mkdir(parents=True, exist_ok=True)
        baseline = run([sys.executable, "-m", "unittest", "discover", "-q"], repo)
        if baseline.returncode:
            raise RuntimeError(f"Invalid evaluation fixture {case.name}: {baseline.stdout}")
        payload = {
            "model": model,
            "artifact_dir": str(case_output / "aider-cache"),
            "files": select_target_files(repo, case.task),
            "context_tokens": 16384,
            "output_tokens": 4096,
            "result_file": str(case_output / "aider-result.json"),
            "message": f"Task:\n{case.task}\n\n{CODE_STYLE_CONVENTIONS}\nComplete the task and preserve tests.",
        }
        request = case_output / "request.json"
        request.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        env = {**os.environ, "OPENAI_API_BASE": base_url, "OPENAI_API_KEY": api_key,
               "PYTHONIOENCODING": "utf-8"}
        started = time.monotonic()
        worker = run([sys.executable, str(SCRIPT_ROOT / "devbot" / "aider_worker.py"), str(request)], repo, env)
        elapsed = time.monotonic() - started
        (case_output / "worker.log").write_text(worker.stdout, encoding="utf-8")
        public_run = run([sys.executable, "-m", "unittest", "discover", "-q"], repo)
        hidden_run = run([sys.executable, str(acceptance), str(repo)], repo)
        (case_output / "public-tests.log").write_text(public_run.stdout, encoding="utf-8")
        (case_output / "acceptance.log").write_text(hidden_run.stdout, encoding="utf-8")
        changed = changed_paths(repo)
        acceptance_exit = public_run.returncode or hidden_run.returncode
        result = score_result(worker.returncode, acceptance_exit, changed, case.allowed_changes)
        result.update({"case": case.name, "elapsed_seconds": round(elapsed, 3)})
        metadata = case_output / "aider-result.json"
        if metadata.exists():
            result["generation"] = json.loads(metadata.read_text(encoding="utf-8"))
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("models", nargs="+", help="LiteLLM/OpenAI-compatible model names")
    parser.add_argument("--base-url", default=os.getenv("OPENAI_API_BASE", "http://litellm:4000/v1"))
    parser.add_argument("--api-key", default=os.getenv("OPENAI_API_KEY", ""))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.api_key:
        parser.error("set OPENAI_API_KEY or pass --api-key")
    output = args.output or Path("evaluation-results") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output.mkdir(parents=True, exist_ok=True)
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "models": []}
    for model in args.models:
        cases = [evaluate_case(case, model, args.base_url, args.api_key, output) for case in CASES]
        report["models"].append({
            "model": model, "passed": sum(case["passed"] for case in cases),
            "total": len(cases), "cases": cases,
        })
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if all(row["passed"] == row["total"] for row in report["models"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
