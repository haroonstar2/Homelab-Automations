import asyncio
import base64
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import psutil
from devbot.commands.code import CodeCog
from devbot.runner import CodeRunner, _run_cmd


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_git_auth_does_not_persist_token_in_remote_or_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", tmp], check=True)
            token = "github-secret-token"
            runner = CodeRunner(root, token, "http://unused", "")
            remote = runner._remote_url("owner/repository")
            subprocess.run(["git", "remote", "add", "origin", remote], cwd=root, check=True)

            stored = subprocess.run(
                ["git", "remote", "get-url", "origin"], cwd=root, check=True,
                text=True, stdout=subprocess.PIPE,
            ).stdout.strip()
            self.assertEqual(stored, "https://github.com/owner/repository.git")
            self.assertNotIn(token, stored)

            env = runner._git_auth_env()
            configured = subprocess.run(
                ["git", "config", "--get", "http.extraHeader"], cwd=root, env=env,
                check=True, text=True, stdout=subprocess.PIPE,
            ).stdout.strip()
            scheme, encoded = configured.split()[-2:]
            self.assertEqual(scheme, "Basic")
            self.assertEqual(
                base64.b64decode(encoded).decode("utf-8"), f"x-access-token:{token}",
            )
            self.assertNotIn(token, " ".join(["git", "remote", "get-url", "origin"]))
            self.assertEqual(
                subprocess.run(
                    ["git", "config", "--local", "--get", "http.extraHeader"],
                    cwd=root, env={**os.environ, "GIT_CONFIG_COUNT": "0"},
                    text=True, stdout=subprocess.PIPE,
                ).returncode,
                1,
            )

    async def test_branch_creation_retry_and_wrong_branch_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", tmp], check=True)
            runner = CodeRunner(root, "", "http://unused", "")
            await runner._git(root, "config", "user.name", "test")
            await runner._git(root, "config", "user.email", "test@example.com")
            await runner._git(root, "commit", "--allow-empty", "-m", "base")
            await runner._prepare_branch(root, "agent/test")
            head = await runner._git(root, "rev-parse", "HEAD")
            await runner._prepare_branch(root, "agent/test")
            self.assertEqual(head, await runner._git(root, "rev-parse", "HEAD"))
            with self.assertRaises(RuntimeError):
                await runner._assert_branch(root, "agent/other")
            with self.assertRaises(RuntimeError):
                await runner._git(root, "checkout", "missing-branch")

    async def test_existing_workspace_discards_failed_uncommitted_changes_before_planning(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            repo = data / "runner-scratch" / "1" / "repo"
            repo.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            tracked = repo / "main.py"
            tracked.write_text("committed\n")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
            subprocess.run(["git", "checkout", "-q", "-b", "agent/test"], cwd=repo, check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://github.com/owner/repository.git"],
                cwd=repo, check=True,
            )
            tracked.write_text("failed generated change\n")
            (repo / "untracked.py").write_text("failed\n")
            runner = CodeRunner(data, "", "http://unused", "")

            with (
                patch.object(runner, "_sync_branch", AsyncMock()),
                patch.object(runner, "build_planning_context", AsyncMock(return_value="clean plan")),
            ):
                returned_repo, outline = await runner.prepare_existing_workspace(
                    1, "owner/repository", "agent/test", "next task",
                )

            self.assertEqual(returned_repo, repo)
            self.assertEqual(outline, "clean plan")
            self.assertEqual(tracked.read_text(), "committed\n")
            self.assertFalse((repo / "untracked.py").exists())

    async def test_duplicate_jobs_are_not_queued_and_cancel_waits(self):
        bot = SimpleNamespace(db=SimpleNamespace(update_code_task_status=AsyncMock()))
        cog = CodeCog(bot)
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def job():
            entered.set()
            try:
                await asyncio.sleep(100)
            finally:
                cancelled.set()

        cog._spawn_task(1, job())
        await entered.wait()
        first = cog.active_tasks[1]
        cog._spawn_task(1, job())
        self.assertIs(cog.active_tasks[1], first)
        await cog.cancel_thread_task(SimpleNamespace(id=1), announce=False)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(first.cancelled())

    async def test_subprocess_timeout(self):
        code, _ = await _run_cmd([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.1)
        self.assertEqual(code, 124)

    async def test_cancellation_terminates_descendant(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "child.pid"
            script = (
                "import subprocess,sys,time; from pathlib import Path; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)"
            )
            task = asyncio.create_task(_run_cmd([sys.executable, "-c", script, str(pidfile)]))
            try:
                for _ in range(100):
                    if pidfile.exists():
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(pidfile.exists())
                pid = int(pidfile.read_text())
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertFalse(psutil.pid_exists(pid))
