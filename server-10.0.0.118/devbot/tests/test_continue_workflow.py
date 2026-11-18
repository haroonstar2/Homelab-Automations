import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from devbot.commands.code import CodeCog


class ContinueWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_continue_creates_fresh_scoped_subtask_and_plan(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repo = Path(temporary.name)
        status_message = SimpleNamespace(edit=AsyncMock())
        thread = Mock(spec=discord.Thread)
        thread.id = 10
        thread.send = AsyncMock(side_effect=[status_message, SimpleNamespace()])
        record = {
            "status": "done", "repo": "owner/repo", "branch": "agent/existing",
            "task": "Create only the DTO", "plan": "Old DTO-only approved plan",
        }
        latest = {"request": "Create only the DTO", "model": "smart"}
        bot = SimpleNamespace(
            db=SimpleNamespace(
                get_code_task=AsyncMock(return_value=record),
                get_latest_code_subtask=AsyncMock(return_value=latest),
                create_code_subtask=AsyncMock(), update_code_task_status=AsyncMock(),
            ),
            runner=SimpleNamespace(prepare_existing_workspace=AsyncMock(return_value=(repo, "current tree"))),
            notion=SimpleNamespace(get_project_conventions=AsyncMock(return_value="project rules")),
            llm=SimpleNamespace(chat=AsyncMock(return_value="Fresh service-only plan")),
            lock_for=lambda _thread_id: asyncio.Lock(),
        )
        cog = CodeCog(bot)
        request = "Add paginated match history service."
        required = ["apps/core-api/src/games/games.service.ts"]
        await cog.plan_continuation(thread, request, required, True)

        prompt = bot.llm.chat.await_args.args[1][1]["content"]
        self.assertIn("NEW SUBTASK (this is the only active request)", prompt)
        self.assertIn("Historical PR context only (not an approved plan)", prompt)
        self.assertNotIn("Old DTO-only approved plan", prompt)
        create = bot.db.create_code_subtask.await_args
        self.assertEqual(create.args[1:3], (request, "Fresh service-only plan"))
        self.assertEqual(create.kwargs["required_paths"], tuple(required))
        self.assertEqual(create.kwargs["allowed_paths"], tuple(required))
        self.assertEqual(
            create.kwargs["commit_title"],
            "feat(core-api): add paginated match history service",
        )
        bot.db.update_code_task_status.assert_awaited_once_with(10, "planned")


if __name__ == "__main__":
    unittest.main()
