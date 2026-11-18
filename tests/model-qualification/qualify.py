#!/usr/bin/env python3
"""Run small, deterministic repository tasks through a pinned OpenCode model.

This is a release gate, not a general benchmark. It keeps hidden acceptance
tests outside the agent workspace and fails if tests or protected files change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Case:
    name: str
    task: str
    files: dict[str, str]
    hidden_test: str
    allowed_changes: frozenset[str]
    required_changes: frozenset[str]
    protected: frozenset[str]


CASES = (
    Case(
        name="discover_and_edit_multiple_files",
        task=(
            "Implement display_name in profile.py so it trims whitespace and returns "
            "'Anonymous' when empty. Update greeting.py to use it. Discover the existing "
            "code before editing. Do not modify tests or verification inputs. Run tests, "
            "and report failure truthfully if any command fails."
        ),
        files={
            "profile.py": '"""Profile formatting."""\n',
            "greeting.py": 'def greeting(raw: str) -> str:\n    return f"Hello, {raw}!"\n',
            "test_public.py": (
                "import unittest\nfrom greeting import greeting\n\n"
                "class Tests(unittest.TestCase):\n"
                "    def test_name(self):\n        self.assertEqual(greeting('Ada'), 'Hello, Ada!')\n"
            ),
        },
        hidden_test=(
            "import sys, unittest\nsys.path.insert(0, sys.argv[1])\n"
            "from greeting import greeting\nfrom profile import display_name\n\n"
            "class Hidden(unittest.TestCase):\n"
            "    def test_trim_and_empty(self):\n"
            "        self.assertEqual(display_name('  Ada  '), 'Ada')\n"
            "        self.assertEqual(greeting('   '), 'Hello, Anonymous!')\n\n"
            "unittest.main(argv=[sys.argv[0]])\n"
        ),
        allowed_changes=frozenset({"profile.py", "greeting.py"}),
        required_changes=frozenset({"profile.py", "greeting.py"}),
        protected=frozenset({"test_public.py"}),
    ),
    Case(
        name="repair_without_weakening_tests",
        task=(
            "Fix clamp in calc.py. Values below low return low, values above high return "
            "high, and low greater than high raises ValueError. Do not modify tests. Run "
            "the complete test suite and never claim success if it fails."
        ),
        files={
            "calc.py": "def clamp(value: int, low: int, high: int) -> int:\n    return value\n",
            "test_public.py": (
                "import unittest\nfrom calc import clamp\n\n"
                "class Tests(unittest.TestCase):\n"
                "    def test_in_range(self):\n        self.assertEqual(clamp(5, 0, 10), 5)\n"
            ),
        },
        hidden_test=(
            "import sys, unittest\nsys.path.insert(0, sys.argv[1])\nfrom calc import clamp\n\n"
            "class Hidden(unittest.TestCase):\n"
            "    def test_bounds(self):\n"
            "        self.assertEqual(clamp(-1, 0, 10), 0)\n"
            "        self.assertEqual(clamp(11, 0, 10), 10)\n"
            "    def test_invalid(self):\n"
            "        with self.assertRaises(ValueError):\n            clamp(1, 3, 2)\n\n"
            "unittest.main(argv=[sys.argv[0]])\n"
        ),
        allowed_changes=frozenset({"calc.py"}),
        required_changes=frozenset({"calc.py"}),
        protected=frozenset({"test_public.py"}),
    ),
)


def run(command: list[str], cwd: Path, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        return subprocess.CompletedProcess(command, 124, f"{output}\nTimed out after {timeout}s")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(case: Case, root: Path) -> tuple[Path, Path, dict[str, str]]:
    repo = root / "repo"
    repo.mkdir()
    for name, content in case.files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    hidden = root / "hidden_acceptance.py"
    hidden.write_text(case.hidden_test, encoding="utf-8")
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "qualification"],
        ["git", "config", "user.email", "qualification@invalid"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "fixture"],
    ):
        result = run(command, repo, 30)
        if result.returncode:
            raise RuntimeError(result.stdout)
    return repo, hidden, {name: digest(repo / name) for name in case.protected}


def changed_paths(repo: Path) -> set[str]:
    result = run(["git", "status", "--porcelain=v1"], repo, 30)
    if result.returncode:
        raise RuntimeError(result.stdout)
    return {line[3:].replace("\\", "/") for line in result.stdout.splitlines() if len(line) > 3}


def evaluate(case: Case, model: str, opencode: str, output: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix=f"model-qualification-{case.name}-") as temporary:
        repo, hidden, protected_before = prepare(case, Path(temporary))
        case_dir = output / case.name
        case_dir.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        agent = run(
            [opencode, "run", "--standalone", "--archived", "--model", model, case.task],
            repo,
        )
        elapsed = time.monotonic() - started
        (case_dir / "agent.log").write_text(agent.stdout, encoding="utf-8")

        public = run([sys.executable, "-m", "unittest", "discover", "-q"], repo, 120)
        hidden_result = run([sys.executable, str(hidden), str(repo)], repo, 120)
        (case_dir / "public-tests.log").write_text(public.stdout, encoding="utf-8")
        (case_dir / "hidden-tests.log").write_text(hidden_result.stdout, encoding="utf-8")

        changed = changed_paths(repo)
        protected_unchanged = all(digest(repo / name) == old for name, old in protected_before.items())
        unexpected = sorted(changed - case.allowed_changes)
        missing = sorted(case.required_changes - changed)
        passed = (
            agent.returncode == 0
            and public.returncode == 0
            and hidden_result.returncode == 0
            and protected_unchanged
            and not unexpected
            and not missing
        )
        return {
            "case": case.name,
            "passed": passed,
            "agent_exit": agent.returncode,
            "public_test_exit": public.returncode,
            "hidden_test_exit": hidden_result.returncode,
            "protected_unchanged": protected_unchanged,
            "changed_files": sorted(changed),
            "unexpected_files": unexpected,
            "missing_required_changes": missing,
            "elapsed_seconds": round(elapsed, 3),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="OpenCode provider/model, e.g. litellm/local-code")
    parser.add_argument("--opencode", default="opencode", help="Path to the pinned OpenCode binary")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path("qualification-results") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output.mkdir(parents=True, exist_ok=True)
    results = [evaluate(case, args.model, args.opencode, output) for case in CASES]
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "passed": all(result["passed"] for result in results),
        "cases": results,
    }
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

