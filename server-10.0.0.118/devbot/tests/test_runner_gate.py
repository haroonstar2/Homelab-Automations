import tempfile
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
            result = await self.runner.execute_task(1, "o/r", "agent/test", "edit main.py", "main.py")
        self.assertFalse(result.ok)
        self.assertEqual(self.runner._run_verification.await_count, 3)
        self.assertFalse(any(c[1] in ("commit", "push") for c in self.commands))

    async def test_followup_failure_never_commits_or_pushes(self):
        with patch("devbot.runner._run_cmd", self.command):
            result = await self.runner.followup_fix(1, "o/r", "agent/test", "main.py", "plan", "fix")
        self.assertFalse(result.ok)
        self.assertFalse(any(c[1] in ("commit", "push") for c in self.commands))

    async def test_push_retry_failure_never_pushes(self):
        with patch("devbot.runner._run_cmd", self.command):
            result = await self.runner.push_existing_branch(1, "o/r", "agent/test")
        self.assertFalse(result.ok)
        self.assertFalse(any(c[1] == "push" for c in self.commands))

    async def test_successful_repair_can_publish(self):
        self.runner._run_verification.side_effect = [("PASS", True), ("FAIL", False), ("PASS", True)]
        with patch("devbot.runner._run_cmd", self.command):
            result = await self.runner.execute_task(1, "o/r", "agent/test", "main.py", "main.py")
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
