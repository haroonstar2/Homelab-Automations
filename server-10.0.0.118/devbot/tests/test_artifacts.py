import json
import tempfile
import unittest
from pathlib import Path

from devbot.runner import CodeRunner


class ArtifactTests(unittest.TestCase):
    def test_artifacts_are_atomic_and_secrets_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = CodeRunner(root, "github-secret", "", "llm-secret")
            runner._artifact(root, "run.json", {"token": "github-secret", "key": "llm-secret"})
            text = (root / "artifacts" / "run.json").read_text()
            self.assertNotIn("github-secret", text)
            self.assertNotIn("llm-secret", text)
            self.assertEqual(json.loads(text), {"token": "***", "key": "***"})
            self.assertFalse((root / "artifacts" / ".run.json.tmp").exists())
