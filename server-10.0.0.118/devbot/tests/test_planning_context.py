import subprocess
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from devbot.runner import CodeRunner


class PlanningContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_receives_filtered_files_not_dependency_trees(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", tmp], check=True)
            (root / "service.py").write_text("class PaymentService: pass\n")
            dependency = root / "node_modules" / "package" / "huge.js"
            dependency.parent.mkdir(parents=True)
            dependency.write_text("ignored dependency\n")
            captured = {}

            async def fake_worker(command, cwd=None, timeout=0, **kwargs):
                request = json.loads(Path(command[-1]).read_text())
                captured.update(request)
                Path(request["output_file"]).write_text("map output")
                return 0, ""

            runner = CodeRunner(root, "", "http://unused", "")
            with patch("devbot.runner._run_cmd", side_effect=fake_worker):
                await runner.build_planning_context(root, "Change service.py")

            self.assertEqual(captured["files"], ["service.py"])
            self.assertEqual(captured["mentioned_fnames"], ["service.py"])

    async def test_symbol_map_and_exact_project_files_are_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", tmp], check=True)
            (root / "service.py").write_text("class PaymentService:\n    def charge(self, amount: int):\n        return amount\n")
            (root / "AGENTS.md").write_text("Always retain API compatibility.\n")
            (root / "go.mod").write_text("module example.test/project\n\ngo 1.24\n")
            (root / "package.json").write_text('{"scripts":{"test":"vitest run"}}')
            runner = CodeRunner(root, "", "http://unused", "")
            context = await runner.build_planning_context(root, "Change PaymentService")
            self.assertIn("PaymentService", context)
            self.assertIn("def charge", context)
            self.assertIn("Always retain API compatibility", context)
            self.assertIn("module example.test/project", context)
            self.assertIn("vitest run", context)
