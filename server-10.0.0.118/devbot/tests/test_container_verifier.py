import json
import tempfile
import unittest
from pathlib import Path

from devbot.container_verifier import ContainerVerifier


class ContainerVerifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_uses_hardened_rootless_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "container-data"
            repo = data / "runner-scratch" / "7" / "repo"
            cwd = repo / "src"
            cwd.mkdir(parents=True)
            calls = []

            async def command(argv, **kwargs):
                calls.append((argv, kwargs))
                if "SecurityOptions" in argv[-1]:
                    return 0, json.dumps(["name=seccomp", "name=rootless"])
                if "CgroupDriver" in argv[-1]:
                    return 0, "systemd 2"
                return 0, "PASS"

            verifier = ContainerVerifier(
                data, Path("/mnt/Storage1/devbot"),
                "unix:///run/rootless-docker/docker.sock", command,
            )
            result = await verifier.run(
                ["python", "-m", "pytest"], cwd=cwd, timeout=90,
                env={"APP_ENV": "test"}, image="python@sha256:" + "a" * 64,
            )
            self.assertEqual(result, (0, "PASS"))
            argv, kwargs = calls[2]
            for pair in (
                ["--network", "none"], ["--cap-drop", "ALL"],
                ["--security-opt", "no-new-privileges=true"],
                ["--pids-limit", "256"], ["--read-only"],
            ):
                self.assertTrue(all(item in argv for item in pair))
            mount = argv[argv.index("--mount") + 1].replace("\\", "/")
            self.assertTrue(mount.endswith(
                "/mnt/Storage1/devbot/runner-scratch/7/repo,dst=/workspace"
            ))
            self.assertEqual(argv[argv.index("--workdir") + 1], "/workspace/src")
            self.assertEqual(argv[argv.index("--user") + 1], "0:0")
            self.assertEqual(kwargs["env"], {"DOCKER_HOST": "unix:///run/rootless-docker/docker.sock"})
            self.assertEqual(calls[3][0][1:3], ["rm", "--force"])
            self.assertEqual(calls[3][0][-1], argv[argv.index("--name") + 1])

    async def test_non_rootless_daemon_fails_before_container_start(self):
        calls = []

        async def command(argv, **kwargs):
            calls.append(argv)
            return 0, json.dumps(["name=seccomp"])

        verifier = ContainerVerifier(
            Path("/data"), Path("/host/data"), "unix:///run/docker.sock", command,
        )
        with self.assertRaisesRegex(RuntimeError, "not running in rootless mode"):
            await verifier.assert_rootless()
        self.assertEqual(len(calls), 1)

    async def test_rootless_daemon_without_cgroup_v2_fails_closed(self):
        async def command(argv, **kwargs):
            if "SecurityOptions" in argv[-1]:
                return 0, json.dumps(["name=rootless"])
            return 0, "none 1"

        verifier = ContainerVerifier(
            Path("/data"), Path("/host/data"), "unix:///run/docker.sock", command,
        )
        with self.assertRaisesRegex(RuntimeError, "cgroup v2"):
            await verifier.assert_rootless()

    async def test_checkout_outside_data_dir_is_rejected(self):
        async def command(argv, **kwargs):
            if "SecurityOptions" in argv[-1]:
                return 0, json.dumps(["name=rootless"])
            return 0, "systemd 2"

        verifier = ContainerVerifier(
            Path("/data"), Path("/host/data"), "unix:///run/docker.sock", command,
        )
        with self.assertRaisesRegex(ValueError, "isolated runner checkout"):
            await verifier.run(
                ["true"], cwd=Path("/outside/repo"), timeout=10, env={},
                image="python@sha256:" + "a" * 64,
            )
