import tempfile
import unittest
from pathlib import Path

from devbot.workspace_paths import checked_path


class WorkspacePathsTests(unittest.TestCase):
    def test_paths_cannot_escape_or_touch_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("../outside.py", "app/../../outside.py", "/etc/file", "C:/file", ".git/config", "app\\file.py"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    checked_path(root, name)
            self.assertEqual(checked_path(root, "settings.py"), root / "settings.py")
            self.assertEqual(checked_path(root, "app/[id]/page.tsx"), root / "app/[id]/page.tsx")
