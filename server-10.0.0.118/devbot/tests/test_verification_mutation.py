import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devbot.runner import CodeRunner


class MutationTests(unittest.IsolatedAsyncioTestCase):
    async def test_verification_side_effect_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", tmp], check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            (root / "tracked.txt").write_text("before")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=root, check=True)
            runner = CodeRunner(root, "", "", "")

            async def mutating_verify(*args):
                (root / "tracked.txt").write_text("changed by test")
                return "command passed", True

            with patch("devbot.verification.verify", mutating_verify):
                output, passed = await runner._run_verification(root, "o/r")
            self.assertFalse(passed)
            self.assertIn("MUTATED", output)
