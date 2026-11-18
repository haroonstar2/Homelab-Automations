import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from devbot.commands.code import CodeCog
from devbot.task_memory import append_feedback, task_specification


class TaskMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)

    def test_updates_survive_reload_and_duplicate_delivery(self):
        append_feedback(self.data, 1, 10, "Keep API compatibility")
        append_feedback(self.data, 1, 11, "Also add tests")
        append_feedback(self.data, 1, 10, "Keep API compatibility")
        spec = task_specification(self.data, 1, "original")
        self.assertIn("original", spec)
        self.assertEqual(spec.count("Keep API compatibility"), 1)
        self.assertIn("Also add tests", spec)

    async def test_running_or_done_discussion_does_not_mutate_requirements_or_execute(self):
        bot = SimpleNamespace(cfg=SimpleNamespace(data_dir=self.data),
                              db=SimpleNamespace(get_code_task=AsyncMock()), is_owner_message=lambda m: True)
        cog = CodeCog(bot)
        cog._spawn_task = Mock()
        thread = Mock(spec=discord.Thread)
        thread.id = 1
        thread.send = AsyncMock()
        for i, status in enumerate(("running", "done")):
            bot.db.get_code_task.return_value = {"status": status}
            message = SimpleNamespace(channel=thread, id=i, content="What changed?", attachments=[])
            await cog.on_message(message)
        cog._spawn_task.assert_not_called()
        self.assertNotIn("What changed?", task_specification(self.data, 1, "original"))
        self.assertEqual(thread.send.await_count, 2)

    async def test_running_attachment_requires_explicit_fix_after_completion(self):
        bot = SimpleNamespace(cfg=SimpleNamespace(data_dir=self.data),
                              db=SimpleNamespace(get_code_task=AsyncMock(return_value={"status": "running"})),
                              is_owner_message=lambda m: True)
        thread = Mock(spec=discord.Thread)
        thread.id = 1
        thread.send = AsyncMock()
        attachment = SimpleNamespace(filename="errors.log", size=20, content_type="text/plain",
                                     read=AsyncMock(return_value=b"complete diagnostic"))
        await CodeCog(bot).on_message(SimpleNamespace(channel=thread, id=2, content="", attachments=[attachment]))
        self.assertNotIn("complete diagnostic", task_specification(self.data, 1, "original"))
        thread.send.assert_awaited_once()

    async def test_fix_control_word_is_not_saved_as_requirement(self):
        bot = SimpleNamespace(
            cfg=SimpleNamespace(data_dir=self.data),
            db=SimpleNamespace(get_code_task=AsyncMock(return_value={"status": "done"})),
            is_owner_message=lambda m: True,
        )
        cog = CodeCog(bot)
        cog._spawn_task = Mock()
        thread = Mock(spec=discord.Thread)
        thread.id = 1
        thread.send = AsyncMock()
        await cog.on_message(SimpleNamespace(channel=thread, id=3, content="fix", attachments=[]))
        cog._spawn_task.assert_not_called()
        self.assertEqual(task_specification(self.data, 1, "original"), "original")
