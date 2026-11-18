import unittest
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from devbot.aider_worker import run


class HarnessFailureTests(unittest.TestCase):
    def coder(self):
        return SimpleNamespace(io=SimpleNamespace(tool_error=Mock()), run=Mock(),
                               num_exhausted_context_windows=0, num_malformed_responses=0)

    def test_success_uses_aider_run(self):
        coder = self.coder()
        coder.main_model = SimpleNamespace(name="openai/smart", info={})
        coder.total_tokens_sent = 10
        coder.total_tokens_received = 5
        with tempfile.TemporaryDirectory() as tmp:
            result = Path(tmp) / "result.json"
            with patch("devbot.aider_worker.make_coder", return_value=coder):
                run({"message": "Task: change behavior", "result_file": str(result)})
            metadata = __import__("json").loads(result.read_text())
            self.assertEqual(metadata["tokens_sent"], 10)
            self.assertEqual(metadata["finish_reasons"], [])
        coder.run.assert_called_once_with(with_message="Task: change behavior")

    def test_reported_error_does_not_become_success(self):
        coder = self.coder()
        coder.run.side_effect = lambda **kw: coder.io.tool_error("API failure")
        with patch("devbot.aider_worker.make_coder", return_value=coder):
            with self.assertRaises(RuntimeError):
                run({"message": "Task"})

    def test_finish_reason_is_recorded_from_non_streaming_response(self):
        coder = self.coder()
        coder.main_model = SimpleNamespace(name="openai/smart", info={})
        coder.total_tokens_sent = 10
        coder.total_tokens_received = 5
        coder.show_send_output = Mock()
        completion = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop")])
        coder.run.side_effect = lambda **kw: coder.show_send_output(completion)
        with tempfile.TemporaryDirectory() as tmp:
            result = Path(tmp) / "result.json"
            with patch("devbot.aider_worker.make_coder", return_value=coder):
                run({"message": "Task", "result_file": str(result)})
            self.assertEqual(__import__("json").loads(result.read_text())["finish_reasons"], ["stop"])

    def test_non_stop_finish_reason_fails_and_is_recorded(self):
        coder = self.coder()
        coder.main_model = SimpleNamespace(name="openai/smart", info={})
        coder.total_tokens_sent = 10
        coder.total_tokens_received = 5
        coder.show_send_output = Mock()
        completion = SimpleNamespace(choices=[SimpleNamespace(finish_reason="length")])
        coder.run.side_effect = lambda **kw: coder.show_send_output(completion)
        with tempfile.TemporaryDirectory() as tmp:
            result = Path(tmp) / "result.json"
            with patch("devbot.aider_worker.make_coder", return_value=coder):
                with self.assertRaisesRegex(RuntimeError, "invalid finish reasons"):
                    run({"message": "Task", "result_file": str(result)})
            self.assertEqual(
                __import__("json").loads(result.read_text())["finish_reasons"], ["length"],
            )

    def test_exhausted_or_malformed_response_fails(self):
        for attr in ("num_exhausted_context_windows", "num_malformed_responses"):
            coder = self.coder()
            setattr(coder, attr, 1)
            with patch("devbot.aider_worker.make_coder", return_value=coder):
                with self.assertRaises(RuntimeError):
                    run({"message": "Task"})

    def test_repair_uses_harness_and_preserves_full_diagnostics(self):
        import asyncio
        from unittest.mock import AsyncMock
        from devbot.runner import CodeRunner
        runner = CodeRunner.__new__(CodeRunner)
        runner._run_aider = AsyncMock()
        diagnostic = "x" * 10000 + "important final error"
        asyncio.run(runner._repair_on_error(
            Path("."), [], "task", "plan", diagnostic, AsyncMock(),
            conventions="project rules", ground_truth="versioned docs", model="selected-model",
        ))
        args = runner._run_aider.call_args.args
        self.assertIn(diagnostic, args[2])
        self.assertEqual(args[3], "plan")
        self.assertEqual(args[4], "project rules")
        self.assertEqual(runner._run_aider.call_args.kwargs["ground_truth"], "versioned docs")
        self.assertEqual(runner._run_aider.call_args.kwargs["model"], "selected-model")


class InstalledAiderTests(unittest.TestCase):
    def test_real_aider_applies_diff_without_committing(self):
        from aider.models import Model
        from devbot.aider_worker import make_coder

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", tmp], check=True)
            source = root / "sample.py"
            source.write_text("value = 1\n")
            guide = root / "guide.md"
            nested = "# Guide\n\n```python\nprint(1)\n```\n\nKeep this footer.\n"
            guide.write_text(nested)
            helper = root / "helper.py"
            helper.write_text("def answer():\n    return 1\n")
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
                    coder = make_coder({"model": "smart", "files": [source.name, guide.name, "settings.py"],
                                        "artifact_dir": str(root / ".git" / "devbot-cache"),
                                        "context_tokens": 16384, "output_tokens": 4096})
                self.assertFalse(coder.auto_commits)
                self.assertFalse(coder.suggest_shell_commands)
                self.assertEqual(coder.max_reflections, 1)
                self.assertIn(str(root / "settings.py"), coder.abs_fnames)
                self.assertEqual(coder.main_model.info["max_input_tokens"], 12288)
                self.assertEqual(coder.main_model.info["max_output_tokens"], 4096)
                self.assertEqual(coder.main_model.extra_params["max_tokens"], 4096)
                self.assertFalse(coder.check_tokens([{"role": "user", "content": "x" * 100000}]))
                coder.partial_response_content = (
                    "sample.py\n```python\n<<<<<<< SEARCH\nvalue = 1\n=======\nvalue = 2\n>>>>>>> REPLACE\n```"
                )
                coder.apply_updates()
                self.assertEqual(source.read_text(), "value = 2\n")
                coder.partial_response_content = (
                    "guide.md\n```markdown\n<<<<<<< SEARCH\n# Guide\n=======\n# Updated Guide\n>>>>>>> REPLACE\n```"
                )
                coder.apply_updates()
                self.assertEqual(guide.read_text(), nested.replace("# Guide", "# Updated Guide"))
                # Aider may expand beyond initial file hints after seeing the repo map.
                coder.partial_response_content = (
                    "helper.py\n```python\n<<<<<<< SEARCH\n    return 1\n=======\n    return 2\n>>>>>>> REPLACE\n```"
                )
                coder.apply_updates()
                self.assertEqual(helper.read_text(), "def answer():\n    return 2\n")
                coder.partial_response_content = (
                    "settings.py\n```python\n<<<<<<< SEARCH\n=======\nENABLED = True\n>>>>>>> REPLACE\n```"
                )
                coder.apply_updates()
                self.assertEqual((root / "settings.py").read_text(), "ENABLED = True\n")
                coder.partial_response_content = (
                    "../outside.py\n```python\n<<<<<<< SEARCH\n=======\nBAD = True\n>>>>>>> REPLACE\n```"
                )
                with self.assertRaises(ValueError):
                    coder.get_edits()
                coder.repo_map.TAGS_CACHE.close()
                coder.repo.repo.close()
            finally:
                os.chdir(old_cwd)
