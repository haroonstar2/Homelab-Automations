import json
import tempfile
import unittest
from pathlib import Path

from devbot.docs_rag import doc_collection_key
from devbot.runner import detect_stack_versions


class DependencyProvenanceTests(unittest.TestCase):
    def test_unversioned_docs_are_labeled_current(self):
        self.assertEqual(doc_collection_key("next", "14.2.1"), "next-current")
        self.assertEqual(doc_collection_key("next", "14.2.1", versioned=True), "next-v14")

    def test_lockfiles_and_python_metadata_are_detected_honestly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(json.dumps({"dependencies": {"next": "^14.0.0", "react": "^18"}}))
            (root / "package-lock.json").write_text(json.dumps({"packages": {
                "node_modules/next": {"version": "14.2.9"}, "node_modules/react": {"version": "18.3.1"}
            }}))
            (root / "go.mod").write_text("module x\ngo 1.24\nrequire example.com/lib v1.2.3\n")
            (root / "pyproject.toml").write_text('[project]\ndependencies=["discord.py>=2.5", "httpx==0.28.1"]\n')
            found = set(detect_stack_versions(root))
            self.assertIn(("node", "next", "14.2.9"), found)
            self.assertIn(("node", "react", "18.3.1"), found)
            self.assertIn(("go", "example.com/lib", "v1.2.3"), found)
            self.assertIn(("python", "discord.py", "declared:>=2.5"), found)
