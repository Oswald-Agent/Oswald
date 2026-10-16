"""Real Docker sandbox integration tests.

Verifies true container isolation, resource boundaries, and command execution
when a live Docker daemon is available. Automatically skipped if Docker is not
running on the host system.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from oswald.core.sandbox import DockerSandboxRuntime, SandboxConfig


def _is_docker_running() -> bool:
    """Check if Docker CLI exists and the daemon responds."""
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return False
    try:
        res = subprocess.run(  # noqa: S603
            [docker_bin, "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
            check=False,
        )
        return res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


has_docker = _is_docker_running()


@pytest.mark.integration
@pytest.mark.skipif(not has_docker, reason="Live Docker daemon is not available")
async def test_real_docker_sandbox_lifecycle(tmp_path: Path) -> None:
    """Spins up a real Docker container, executes commands inside /workspace, and tears down."""
    workspace = tmp_path / "sandbox_workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("print('OSWALD SANDBOX ACTIVE')", encoding="utf-8")

    config = SandboxConfig(
        task_id="int-test-1",
        workspace_path=workspace,
        image="python:3.12-slim",
    )

    runtime = DockerSandboxRuntime(config=config)

    async with runtime as sb:
        assert await sb.is_running()

        # Run python script from bind-mounted /workspace
        result = await sb.exec_command(["python", "sample.py"])
        assert result.exit_code == 0
        assert "OSWALD SANDBOX ACTIVE" in result.stdout

        # Verify network isolation (--network none blocks outbound connections)
        net_result = await sb.exec_command(
            [
                "python",
                "-c",
                "import urllib.request; urllib.request.urlopen('http://example.com', timeout=2)",
            ],
            timeout_seconds=5.0,
        )
        assert net_result.exit_code != 0

    # Ensure container is purged after context exit
    assert not await runtime.is_running()
