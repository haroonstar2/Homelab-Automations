import tempfile
import unittest
from pathlib import Path

from devbot.change_contract import ChangeContract


class ChangeContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()

    def test_required_nonexistent_path_is_a_generation_target(self):
        contract = ChangeContract.create(
            self.repo,
            required_paths=["apps/core-api/src/games/games.service.ts"],
            allowed_paths=["apps/core-api/src/games/games.service.ts"],
        )
        self.assertEqual(
            contract.add_targets(["package.json"]),
            ["apps/core-api/src/games/games.service.ts"],
        )

    def test_missing_required_or_unexpected_path_fails(self):
        contract = ChangeContract.create(self.repo, ["service.ts"], ["service.ts"])
        missing = contract.evaluate({"dto.ts"})
        self.assertFalse(missing.ok)
        self.assertEqual(missing.missing_required, ("service.ts",))
        self.assertEqual(missing.unexpected_paths, ("dto.ts",))
        self.assertIn("required paths not changed", missing.failure_message())

    def test_exact_required_change_passes(self):
        (self.repo / "service.ts").write_text("export class Service {}\n")
        result = ChangeContract.create(self.repo, ["service.ts"], ["service.ts"]).evaluate({"service.ts"})
        self.assertTrue(result.ok)

    def test_strict_scope_removes_unapproved_edit_targets(self):
        contract = ChangeContract.create(self.repo, ["required.py"], ["required.py"])
        self.assertEqual(
            contract.add_targets(["other.py", "required.py", "unrelated.py"]),
            ["required.py"],
        )

    def test_empty_new_required_file_does_not_satisfy_contract(self):
        contract = ChangeContract.create(self.repo, ["new.ts"], ["new.ts"])
        (self.repo / "new.ts").touch()
        result = contract.evaluate({"new.ts"})
        self.assertFalse(result.ok)
        self.assertEqual(result.empty_required_paths, ("new.ts",))

    def test_unsafe_or_inconsistent_paths_are_rejected(self):
        for path in ("../outside.ts", "/tmp/outside.ts", ".git/config", "C:\\outside.ts"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                ChangeContract.create(self.repo, [path])
        with self.assertRaises(ValueError):
            ChangeContract.create(self.repo, ["required.ts"], ["other.ts"])

    def test_explicit_nonempty_scope_is_required(self):
        with self.assertRaisesRegex(ValueError, "explicit allowed file scope"):
            ChangeContract.create(self.repo, ["service.py"])
        with self.assertRaisesRegex(ValueError, "at least one path"):
            ChangeContract.create(self.repo, (), [])

    def test_protected_verification_tree_cannot_be_scoped_or_changed(self):
        (self.repo / "tests").mkdir()
        with self.assertRaisesRegex(ValueError, "Protected verification paths"):
            ChangeContract.create(
                self.repo, ["tests/test_service.py"], ["tests/test_service.py"], ["tests"],
            )
        contract = ChangeContract.create(
            self.repo, ["service.py"], ["service.py"], ["tests", "pyproject.toml"],
        )
        result = contract.evaluate(["service.py", "tests/test_service.py"])
        self.assertFalse(result.ok)
        self.assertEqual(result.protected_paths_changed, ("tests/test_service.py",))
        self.assertIn("protected verification paths changed", result.failure_message())

    def test_legitimate_scoped_edit_passes_with_protected_inputs(self):
        (self.repo / "service.py").write_text("VALUE = 1\n")
        contract = ChangeContract.create(
            self.repo, ["service.py"], ["service.py"], ["tests", "pyproject.toml"],
        )
        self.assertTrue(contract.evaluate(["service.py"]).ok)
