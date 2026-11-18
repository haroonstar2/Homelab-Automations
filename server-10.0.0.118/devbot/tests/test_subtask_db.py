import unittest
from unittest.mock import AsyncMock

from devbot.db import Database, SCHEMA


class SubtaskDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_latest_and_update_subtask(self):
        pool = AsyncMock()
        expected = {"id": 7, "sequence": 2}
        pool.fetchrow.return_value = expected
        database = Database(pool)

        created = await database.create_code_subtask(
            10, "Add service", "Create games.service.ts",
            required_paths=["apps/core-api/src/games/games.service.ts"],
            allowed_paths=["apps/core-api/src/games/games.service.ts"],
            model="smart", commit_title="feat(core-api): add match history service",
        )
        self.assertEqual(created, expected)
        create_args = pool.fetchrow.await_args.args
        self.assertEqual(create_args[1], 10)
        self.assertEqual(create_args[4], ["apps/core-api/src/games/games.service.ts"])
        self.assertEqual(create_args[7], "feat(core-api): add match history service")

        await database.get_latest_code_subtask(10)
        self.assertIn("ORDER BY sequence DESC", pool.fetchrow.await_args.args[0])

        await database.update_code_subtask_result(
            7, "done", base_sha="abc", verification={"passed": True}, commit_sha="def",
        )
        update_args = pool.execute.await_args.args
        self.assertEqual(update_args[1:4], (7, "done", "abc"))
        self.assertIn('"passed": true', update_args[4])
        self.assertEqual(update_args[5], "def")

    def test_schema_keeps_ordered_subtask_history(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS code_subtasks", SCHEMA)
        self.assertIn("UNIQUE (thread_id, sequence)", SCHEMA)
        self.assertIn("commit_title", SCHEMA)
