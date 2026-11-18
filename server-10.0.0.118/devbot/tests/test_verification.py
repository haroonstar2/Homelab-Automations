import json
import sys
import tempfile
import unittest
from pathlib import Path

from devbot.runner import _run_cmd
from devbot.verification import load_protected_paths, verify


async def local_run(cmd, **kwargs):
    kwargs.pop("image")
    return await _run_cmd(cmd, **kwargs)


class RequiredCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.policy = self.root / "project-checks.json"

    def configure(self, checks, protected_paths=None):
        entry = {"checks": checks, "image": "python@sha256:" + "a" * 64}
        if protected_paths is not None:
            entry["protected_paths"] = protected_paths
        self.policy.write_text(json.dumps({"o/r": entry}))

    async def test_missing_and_empty_policy_fail_closed(self):
        self.assertFalse((await verify(self.policy, "o/r", self.repo, local_run))[1])
        self.configure([])
        self.assertFalse((await verify(self.policy, "o/r", self.repo, local_run))[1])

    async def test_actual_commands_run_even_without_dirty_files(self):
        self.configure([{"argv": [sys.executable, "-c", "print('actual test ran')"]}])
        output, ok = await verify(self.policy, "o/r", self.repo, local_run)
        self.assertTrue(ok)
        self.assertIn("actual test ran", output)

    async def test_failed_setup_prevents_later_checks(self):
        self.configure([
            {"argv": [sys.executable, "-c", "raise SystemExit(2)"]},
            {"argv": [sys.executable, "-c", "print('SHOULD NOT RUN')"]},
        ])
        output, ok = await verify(self.policy, "o/r", self.repo, local_run)
        self.assertFalse(ok)
        self.assertNotIn("SHOULD NOT RUN", output)

    async def test_structured_failure_names_the_failing_component(self):
        self.configure([
            {
                "component": "core-api build",
                "argv": [sys.executable, "-c", "print('nest failure'); raise SystemExit(2)"],
            },
            {
                "component": "web-client build",
                "argv": [sys.executable, "-c", "print('next success')"],
            },
        ])
        report = await verify(self.policy, "o/r", self.repo, local_run)
        self.assertFalse(report.passed)
        self.assertEqual(len(report.checks), 1)
        self.assertEqual(report.checks[0]["component"], "core-api build")
        self.assertIn("core-api build: FAIL", report.failure_summary())
        self.assertNotIn("next success", report.output)

    async def test_long_failure_summary_keeps_component_heading(self):
        self.configure([{
            "component": "core-api build",
            "argv": [sys.executable, "-c", "print('x' * 2000); raise SystemExit(2)"],
        }])
        report = await verify(self.policy, "o/r", self.repo, local_run)
        summary = report.failure_summary(max_chars=200)
        self.assertTrue(summary.startswith("core-api build: FAIL (exit 2)"))
        self.assertLessEqual(len(summary), 200)

    async def test_missing_executable_is_not_a_pass(self):
        self.configure([{"argv": ["nonexistent-devbot-test-tool"]}])
        output, ok = await verify(self.policy, "o/r", self.repo, local_run)
        self.assertFalse(ok)
        self.assertIn("NOT RUN", output)

    async def test_invalid_command_and_outside_cwd_are_rejected(self):
        for check in ({"argv": "echo test"}, {"argv": ["echo"], "cwd": ".."}):
            self.configure([check])
            self.assertFalse((await verify(self.policy, "o/r", self.repo, local_run))[1])

    async def test_unpinned_verifier_image_is_rejected(self):
        self.policy.write_text(json.dumps({
            "o/r": {"image": "python:latest", "checks": [{"argv": ["true"]}]},
        }))
        report = await verify(self.policy, "o/r", self.repo, local_run)
        self.assertFalse(report.passed)
        self.assertIn("sha256 image ID or repository digest", report.output)

    async def test_local_sha256_image_id_is_accepted(self):
        self.policy.write_text(json.dumps({
            "o/r": {"image": "sha256:" + "b" * 64,
                    "checks": [{"argv": [sys.executable, "-c", "print('ok')"]}]},
        }))
        report = await verify(self.policy, "o/r", self.repo, local_run)
        self.assertTrue(report.passed)

    async def test_secrets_are_not_inherited_but_literal_test_env_is(self):
        self.configure([{
            "argv": [sys.executable, "-c", "import os; assert 'PRODUCTION_SECRET' not in os.environ; assert os.environ['APP_ENV']=='test'"],
            "env": {"APP_ENV": "test"},
        }])
        old = __import__("os").environ["PRODUCTION_SECRET"] = "do-not-pass"
        try:
            self.assertTrue((await verify(self.policy, "o/r", self.repo, local_run))[1])
        finally:
            __import__("os").environ.pop("PRODUCTION_SECRET", None)

    def test_protected_paths_are_policy_owned_and_contained(self):
        (self.repo / "tests").mkdir()
        self.configure([{"argv": ["true"]}], ["tests", "pyproject.toml", "tests"])
        self.assertEqual(
            load_protected_paths(self.policy, "o/r", self.repo),
            ("tests", "pyproject.toml"),
        )
        for paths in ("tests", ["../outside"], [".git/config"]):
            with self.subTest(paths=paths):
                self.configure([{"argv": ["true"]}], paths)
                with self.assertRaises(ValueError):
                    load_protected_paths(self.policy, "o/r", self.repo)
