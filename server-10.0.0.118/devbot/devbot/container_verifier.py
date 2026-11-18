"""Run owner-defined repository checks in disposable rootless Docker containers."""

from __future__ import annotations

import json
import uuid
from pathlib import Path


class ContainerVerifier:
    def __init__(self, data_dir: Path, host_data_dir: Path, docker_host: str, run_cmd) -> None:
        self.data_dir = data_dir.resolve()
        self.host_data_dir = host_data_dir.resolve()
        self.docker_host = docker_host.strip()
        self.run_cmd = run_cmd

    def _docker_env(self) -> dict[str, str]:
        if not self.docker_host.startswith("unix://"):
            raise RuntimeError("VERIFIER_DOCKER_HOST must name a rootless Unix socket")
        return {"DOCKER_HOST": self.docker_host}

    async def assert_rootless(self) -> None:
        code, output = await self.run_cmd(
            ["docker", "info", "--format", "{{json .SecurityOptions}}"],
            env=self._docker_env(), timeout=30,
        )
        if code:
            raise RuntimeError(f"Rootless Docker verifier is unavailable: {output[-500:]}")
        try:
            options = json.loads(output.strip())
        except json.JSONDecodeError as exc:
            raise RuntimeError("Docker returned invalid security option metadata") from exc
        if not isinstance(options, list) or not any("rootless" in str(item).lower() for item in options):
            raise RuntimeError("Verifier Docker daemon is not running in rootless mode")
        code, cgroups = await self.run_cmd(
            ["docker", "info", "--format", "{{.CgroupDriver}} {{.CgroupVersion}}"],
            env=self._docker_env(), timeout=30,
        )
        if code or cgroups.strip().lower() != "systemd 2":
            raise RuntimeError("Rootless verifier requires systemd cgroup v2 resource enforcement")

    def _host_repo(self, repo_dir: Path) -> Path:
        resolved = repo_dir.resolve()
        if not resolved.is_relative_to(self.data_dir):
            raise ValueError("Verification checkout is outside DATA_DIR")
        return self.host_data_dir / resolved.relative_to(self.data_dir)

    async def run(self, argv, *, cwd, timeout, env, image):
        await self.assert_rootless()
        repo_dir = next(
            (parent for parent in (Path(cwd), *Path(cwd).parents) if parent.name == "repo"),
            None,
        )
        if repo_dir is None or not repo_dir.resolve().is_relative_to(self.data_dir):
            raise ValueError("Verification cwd is not inside an isolated runner checkout")
        relative_cwd = Path(cwd).resolve().relative_to(repo_dir.resolve()).as_posix()
        workdir = "/workspace" + ("/" + relative_cwd if relative_cwd != "." else "")
        container_name = "devbot-check-" + uuid.uuid4().hex
        command = [
            "docker", "run", "--rm", "--name", container_name,
            "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
            "--pids-limit", "256", "--memory", "2g", "--cpus", "2",
            # Rootless container UID 0 maps to the unprivileged daemon owner on
            # the host. A subordinate UID such as 1000 cannot write the bind-mounted checkout.
            "--user", "0:0", "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
            "--tmpfs", "/home:rw,nosuid,nodev,size=64m",
            "--mount", f"type=bind,src={self._host_repo(repo_dir)},dst=/workspace",
            "--workdir", workdir,
        ]
        for name, value in sorted(env.items()):
            command.extend(["--env", f"{name}={value}"])
        command.extend([image, *argv])
        try:
            return await self.run_cmd(
                command, env=self._docker_env(), timeout=timeout,
            )
        finally:
            # Killing a timed-out Docker client does not reliably stop its container.
            # A unique name gives the parent an unconditional cleanup handle.
            await self.run_cmd(
                ["docker", "rm", "--force", container_name],
                env=self._docker_env(), timeout=30,
            )
