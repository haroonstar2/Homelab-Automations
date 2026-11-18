import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psutil
from devbot.commands.code import CodeCog
from devbot.runner import CodeRunner, _run_cmd


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
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
