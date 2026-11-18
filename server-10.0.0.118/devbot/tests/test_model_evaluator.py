import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "evaluate_code_models.py"
SPEC = importlib.util.spec_from_file_location("evaluate_code_models", SCRIPT)
evaluator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = evaluator
SPEC.loader.exec_module(evaluator)


class ModelEvaluatorTests(unittest.TestCase):
    def test_score_requires_generation_acceptance_change_and_scope(self):
        allowed = frozenset({"calc.py"})
        self.assertTrue(evaluator.score_result(0, 0, {"calc.py"}, allowed)["passed"])
        self.assertFalse(evaluator.score_result(1, 0, {"calc.py"}, allowed)["passed"])
        self.assertFalse(evaluator.score_result(0, 1, {"calc.py"}, allowed)["passed"])
        self.assertFalse(evaluator.score_result(0, 0, set(), allowed)["passed"])
        result = evaluator.score_result(0, 0, {"calc.py", "test_public.py"}, allowed)
        self.assertFalse(result["passed"])
        self.assertEqual(result["unexpected_files"], ["test_public.py"])

    def test_fixtures_have_clean_public_baseline_but_unmet_hidden_requirement(self):
        for case in evaluator.CASES:
            with self.subTest(case=case.name), tempfile.TemporaryDirectory() as tmp:
                repo, acceptance = evaluator.prepare_case(case, Path(tmp))
                public = evaluator.run(
                    [evaluator.sys.executable, "-m", "unittest", "discover", "-q"], repo
                )
                hidden = evaluator.run([evaluator.sys.executable, str(acceptance), str(repo)], repo)
                self.assertEqual(public.returncode, 0, public.stdout)
                self.assertNotEqual(hidden.returncode, 0, "Hidden acceptance must require a real edit")


if __name__ == "__main__":
    unittest.main()
