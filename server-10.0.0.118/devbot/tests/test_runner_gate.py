import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from devbot.runner import CodeRunner


class PublicationGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runner = CodeRunner.__new__(CodeRunner)
        self.runner.scratch_root = Path(self.temp.name)
        self.runner.github_token = "test-token"
        self.runner.active_status = {}
        self.runner.checks_file = self.runner.scratch_root / "project-checks.json"
        self.runner.checks_file.write_text(json.dumps({
            "o/r": {"image": "python@sha256:" + "a" * 64,
                    "protected_paths": ["tests"], "checks": [{"argv": ["true"]}]},
        }))
        self.repo = self.runner.scratch_root / "1" / "repo"
        self.repo.mkdir(parents=True)
        (self.repo / "main.py").write_text("x = 1\n")
        self.runner._run_aider = AsyncMock()
        self.runner._repair_on_error = AsyncMock()
        self.runner._run_verification = AsyncMock(return_value=("FAIL", False))
        self.commands = []
        self.changed_paths = {"main.py"}

    async def command(self, cmd, **kwargs):
        self.commands.append(cmd)
        if cmd[1:3] == ["rev-parse", "--abbrev-ref"]:
            return 0, "agent/test\n"
        if cmd[1] == "diff" and "--name-only" in cmd:
            return 0, "\0".join(sorted(self.changed_paths)) + "\0"
        if cmd[1:3] == ["ls-files", "--others"]:
            return 0, ""
        return 0, " M main.py\n" if cmd[1] == "status" else "abc123\n"

    async def test_initial_failure_after_repair_never_commits_or_pushes(self):
        self.runner._run_verification.side_effect = [("PASS", True), ("FAIL", False), ("FAIL", False)]
        with patch("devbot.runner._run_cmd", self.command):
            result = await self.runner.execute_task(
                1, "o/r", "agent/test", "edit main.py", "main.py",
                required_paths=["main.py"], allowed_paths=["main.py"],
            )
        self.assertFalse(result.ok)
        self.assertEqual(self.runner._run_verification.await_count, 3)
        self.assertFalse(any(c[1] in ("commit", "push") for c in self.commands))

    async def test_followup_failure_never_commits_or_pushes(self):
        with patch("devbot.runner._run_cmd", self.command):
            result = await self.runner.followup_fix(
                1, "o/r", "agent/test", "main.py", "plan", "fix",
                required_paths=["main.py"], allowed_paths=["main.py"],
            )
        self.assertFalse(result.ok)
        self.assertTrue(any(c[1:4] == ["reset", "--hard", "HEAD"] for c in self.commands))
        self.assertTrue(any(c[1:3] == ["clean", "-fd"] for c in self.commands))
        self.assertFalse(any(c[1] in ("commit", "push") for c in self.commands))

    async def test_push_retry_refuses_dirty_worktree(self):
        self.runner._run_verification.return_value = ("PASS", True)
        with patch("devbot.runner._run_cmd", self.command):
            result = await self.runner.push_existing_branch(1, "o/r", "agent/test")
        self.assertFalse(result.ok)
        self.assertIn("clean worktree", result.error)
        self.assertFalse(any(c[1] == "push" for c in self.commands))

    async def test_push_retry_binds_verification_to_pushed_head(self):
        full_sha = "a" * 40

        async def clean_command(cmd, **kwargs):
            self.commands.append(cmd)
            if cmd[1:3] == ["rev-parse", "--abbrev-ref"]:
                return 0, "agent/test\n"
            if cmd[1:3] == ["status", "--porcelain=v1"]:
                return 0, ""
            if cmd[1:3] == ["rev-parse", "HEAD"]:
                return 0, full_sha + "\n"
            if cmd[1:3] == ["rev-parse", "--short"]:
                return 0, "aaaaaaa\n"
            if cmd[1] == "symbolic-ref":
                return 0, "refs/remotes/origin/main\n"
            return 0, "ok\n"

        report = type("Report", (tuple,), {
            "as_dict": lambda self: {"passed": True, "checks": [], "output": "PASS"},
        })(("PASS", True))
        self.runner._run_verification.return_value = report
        with patch("devbot.runner._run_cmd", clean_command):
            result = await self.runner.push_existing_branch(1, "o/r", "agent/test")
        self.assertTrue(result.ok)
        self.assertEqual(result.verification["commit_sha"], full_sha)
        self.assertTrue(any(c[1] == "push" for c in self.commands))

    async def test_push_retry_refuses_head_changed_after_verification(self):
        heads = iter(("a" * 40, "b" * 40))

        async def changing_command(cmd, **kwargs):
            self.commands.append(cmd)
            if cmd[1:3] == ["rev-parse", "--abbrev-ref"]:
                return 0, "agent/test\n"
            if cmd[1:3] == ["status", "--porcelain=v1"]:
                return 0, ""
            if cmd[1:3] == ["rev-parse", "HEAD"]:
                return 0, next(heads) + "\n"
            if cmd[1:3] == ["rev-parse", "--short"]:
                return 0, "aaaaaaa\n"
            if cmd[1] == "symbolic-ref":
                return 0, "refs/remotes/origin/main\n"
            return 0, "ok\n"

        self.runner._run_verification.return_value = ("PASS", True)
        with patch("devbot.runner._run_cmd", changing_command):
            result = await self.runner.push_existing_branch(1, "o/r", "agent/test")
        self.assertFalse(result.ok)
        self.assertIn("HEAD changed", result.error)
        self.assertFalse(any(c[1] == "push" for c in self.commands))

    async def test_successful_repair_can_publish(self):
        self.runner._run_verification.side_effect = [("PASS", True), ("FAIL", False), ("PASS", True)]
        with patch("devbot.runner._run_cmd", self.command):
            result = await self.runner.execute_task(
                1, "o/r", "agent/test", "main.py", "main.py",
                required_paths=["main.py"], allowed_paths=["main.py"],
            )
        self.assertTrue(result.ok)
        self.assertTrue(any(c[1:4] == ["reset", "--hard", "HEAD"] for c in self.commands))
        self.assertTrue(any(c[1:3] == ["clean", "-fd"] for c in self.commands))
        self.assertTrue(any(c[1] == "push" for c in self.commands))

    async def test_followup_wrong_file_fails_contract_without_publication(self):
        self.runner._run_verification.return_value = ("PASS", True)
        with patch("devbot.runner._run_cmd", self.command):
            result = await self.runner.followup_fix(
                1, "o/r", "agent/test", "create required.py", "plan", "continue",
                required_paths=["required.py"], allowed_paths=["required.py"],
            )
        self.assertFalse(result.ok)
        self.assertIn("required paths not changed: required.py", result.error)
        self.assertIn("outside the allowed scope", result.error)
        self.assertFalse(any(c[1] in ("commit", "push") for c in self.commands))
