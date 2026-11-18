from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("qualify.py")
SPEC = importlib.util.spec_from_file_location("model_qualification", MODULE_PATH)
assert SPEC and SPEC.loader
qualify = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = qualify
SPEC.loader.exec_module(qualify)


class QualificationHarnessTests(unittest.TestCase):
    def test_child_pwd_matches_cwd_and_tests_do_not_dirty_fixture(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qualification-harness-test-") as temporary:
            root = Path(temporary)
            stub = root / "opencode-stub"
            stub.write_text(
                "#!/bin/sh\n"
                "test \"$PWD\" = \"$(pwd)\" || exit 43\n"
                "mkdir -p __pycache__\n"
                ": >__pycache__/agent-created.pyc\n"
                "exit 42\n",
                encoding="utf-8",
            )
            os.chmod(stub, 0o755)

            result = qualify.evaluate(
                qualify.CASES[1],
                "unused/model",
                str(stub),
                root / "results",
            )

            self.assertEqual(result["agent_exit"], 42)
            self.assertEqual(result["changed_files"], [])
            self.assertEqual(result["unexpected_files"], [])


if __name__ == "__main__":
    unittest.main()
