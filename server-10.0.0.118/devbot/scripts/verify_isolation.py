"""Live adversarial verifier probe. Run inside the deployed devbot container."""

import argparse
import asyncio
import os
import tempfile
from pathlib import Path

from devbot.container_verifier import ContainerVerifier
from devbot.runner import _run_cmd


PROBE = r"""
import os, pathlib, socket
assert os.environ.get('DEVBOT_PROBE_SECRET') is None, 'bot secret leaked'
assert not pathlib.Path('/data/runner-scratch/isolation-sibling/secret.txt').exists(), 'sibling data visible'
try:
    pathlib.Path('/etc/devbot-probe').write_text('escape')
except OSError:
    pass
else:
    raise AssertionError('container root is writable')
s = socket.socket()
s.settimeout(1)
try:
    s.connect(('1.1.1.1', 53))
except OSError:
    pass
else:
    raise AssertionError('external network is reachable')
print('secret=DENIED sibling=DENIED root-write=DENIED network=DENIED')
"""


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", help="digest-pinned verifier image containing python")
    args = parser.parse_args()
    if not (args.image.startswith("sha256:") or "@sha256:" in args.image):
        raise SystemExit("image must be an immutable sha256 image ID or repository digest")
    data_dir = Path(os.environ.get("DATA_DIR", "/data")).resolve()
    host_data_dir = Path(os.environ["VERIFIER_HOST_DATA_DIR"]).resolve()
    docker_host = os.environ["VERIFIER_DOCKER_HOST"]
    scratch = data_dir / "runner-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    sibling = scratch / "isolation-sibling"
    sibling.mkdir(exist_ok=True)
    (sibling / "secret.txt").write_text("must-not-be-readable")
    os.environ["DEVBOT_PROBE_SECRET"] = "must-not-leak"
    try:
        with tempfile.TemporaryDirectory(prefix="isolation-probe-", dir=scratch) as task:
            repo = Path(task) / "repo"
            repo.mkdir()
            verifier = ContainerVerifier(data_dir, host_data_dir, docker_host, _run_cmd)
            code, output = await verifier.run(
                ["python", "-c", PROBE], cwd=repo, timeout=30, env={}, image=args.image,
            )
            if code:
                raise SystemExit(f"isolation probe failed (exit {code}):\n{output}")
            print(output.strip())
    finally:
        os.environ.pop("DEVBOT_PROBE_SECRET", None)
        (sibling / "secret.txt").unlink(missing_ok=True)
        sibling.rmdir()


if __name__ == "__main__":
    asyncio.run(main())
