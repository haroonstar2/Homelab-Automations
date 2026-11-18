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

    async def test_verification_detects_mutation_of_an_already_dirty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", tmp], check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            tracked = root / "tracked.txt"
            tracked.write_text("committed")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=root, check=True)
            tracked.write_text("generated change")
            runner = CodeRunner(root, "", "", "")

            async def mutating_verify(*args):
                tracked.write_text("changed by verification")
                return "command passed", True

            with patch("devbot.verification.verify", mutating_verify):
                output, passed = await runner._run_verification(root, "o/r")
            self.assertFalse(passed)
            self.assertIn("MUTATED", output)

    async def test_verification_detects_mutation_of_an_untracked_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", tmp], check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "commit", "--allow-empty", "-q", "-m", "base"], cwd=root, check=True)
            generated = root / "generated.txt"
            generated.write_text("generated change")
            runner = CodeRunner(root, "", "", "")

            async def mutating_verify(*args):
                generated.write_text("changed by verification")
                return "command passed", True

            with patch("devbot.verification.verify", mutating_verify):
                output, passed = await runner._run_verification(root, "o/r")
            self.assertFalse(passed)
            self.assertIn("MUTATED", output)

    async def test_fingerprint_changes_for_deletion_and_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", tmp], check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text("first")
            second.write_text("second")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=root, check=True)
            runner = CodeRunner(root, "", "", "")
            baseline = await runner._worktree_fingerprint(root)

            first.unlink()
            self.assertNotEqual(baseline, await runner._worktree_fingerprint(root))
            first.write_text("first")

            second.rename(root / "renamed.txt")
            self.assertNotEqual(baseline, await runner._worktree_fingerprint(root))
