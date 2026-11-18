import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from devbot.commands.code import CodeCog


class PullRequestLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_push_success_pr_failure_is_persisted_as_partial_failure(self):
        outcome = SimpleNamespace(
            ok=True,
            branch="agent/test",
            commit_sha="abc1234",
            base_sha="base123",
            diff_stat="one file changed",
            cumulative_diff_stat="one file changed",
            test_output="PASS",
            verification={"passed": True, "checks": [], "output": "PASS"},
            change_contract=None,
            error="",
        )
        db = SimpleNamespace(
            get_code_task=AsyncMock(return_value={
                "repo": "owner/repository", "branch": "agent/test",
                "task": "task", "plan": "plan", "pr_url": "",
            }),
            get_latest_code_subtask=AsyncMock(return_value={"id": 7, "commit_title": "fix(repo): task"}),
            update_code_task_status=AsyncMock(),
            update_code_subtask_result=AsyncMock(),
        )
        github = SimpleNamespace(
            get_default_branch=AsyncMock(return_value="main"),
            create_draft_pr=AsyncMock(side_effect=RuntimeError("permission denied")),
        )
        bot = SimpleNamespace(
            db=db,
            github=github,
            runner=SimpleNamespace(push_existing_branch=AsyncMock(return_value=outcome)),
            lock_for=lambda _thread_id: asyncio.Lock(),
        )
        cog = CodeCog(bot)
        message = SimpleNamespace(edit=AsyncMock())
        thread = SimpleNamespace(id=42, send=AsyncMock(return_value=message))

        await cog.push_only(thread)

        db.update_code_task_status.assert_awaited_once_with(42, "pr_failed")
        db.update_code_subtask_result.assert_awaited_once()
        self.assertEqual(db.update_code_subtask_result.await_args.args[1], "pr_failed")
        rendered = message.edit.await_args.kwargs["content"]
        self.assertIn("Draft PR creation failed", rendered)
        self.assertIn("/tree/agent/test", rendered)
        self.assertNotIn("Review / Draft PR", rendered)

    async def test_open_pr_propagates_github_failure(self):
        bot = SimpleNamespace(
            github=SimpleNamespace(
                get_default_branch=AsyncMock(return_value="main"),
                create_draft_pr=AsyncMock(side_effect=RuntimeError("permission denied")),
            )
        )
        cog = CodeCog(bot)
        with self.assertRaisesRegex(RuntimeError, "permission denied"):
            await cog._open_pr("owner/repository", "agent/test", "task", "plan", "PASS")


if __name__ == "__main__":
    unittest.main()
