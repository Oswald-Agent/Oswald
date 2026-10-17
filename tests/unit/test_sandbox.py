"""Unit tests for the sandbox execution subsystem."""

from pathlib import Path

import pytest

from oswald.core.sandbox import (
    ContainerNotRunningError,
    DockerSandboxRuntime,
    ExecResult,
    FakeSandboxRuntime,
    SandboxConfig,
    SandboxError,
    SandboxRuntime,
    SandboxStatus,
)


@pytest.fixture
def sample_config(tmp_path: Path) -> SandboxConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return SandboxConfig(
        task_id="task-42",
        workspace_path=workspace,
    )


# 1. Configuration & Model Tests


def test_sandbox_config_defaults(tmp_path: Path) -> None:
    """SandboxConfig enforces hardened security and resource defaults."""
    cfg = SandboxConfig(task_id="task-1", workspace_path=tmp_path)

    assert cfg.task_id == "task-1"
    assert cfg.image == "python:3.12-slim"
    assert cfg.memory_limit == "2g"
    assert cfg.cpus == 2.0
    assert cfg.read_only_root is True
    assert cfg.network_disabled is True
    assert cfg.user == "1000:1000"
    assert "/tmp" in cfg.tmpfs_mounts  # noqa: S108


def test_exec_result_attributes() -> None:
    """ExecResult captures exit code, streams, and duration."""
    res = ExecResult(
        exit_code=0,
        stdout="hello",
        stderr="",
        duration_seconds=0.15,
    )
    assert res.exit_code == 0
    assert res.stdout == "hello"
    assert res.duration_seconds == 0.15


# 2. Protocol Conformance


def test_runtimes_conform_to_protocol(sample_config: SandboxConfig) -> None:
    """Both FakeSandboxRuntime and DockerSandboxRuntime satisfy the SandboxRuntime protocol."""
    fake = FakeSandboxRuntime(config=sample_config)
    docker_rt = DockerSandboxRuntime(config=sample_config)

    assert isinstance(fake, SandboxRuntime)
    assert isinstance(docker_rt, SandboxRuntime)


# 3. FakeSandboxRuntime Tests


def _status(runtime: SandboxRuntime) -> SandboxStatus:
    return runtime.status


async def test_fake_sandbox_lifecycle(sample_config: SandboxConfig) -> None:
    """FakeSandboxRuntime transitions through lifecycle states and records calls."""
    sandbox = FakeSandboxRuntime(config=sample_config)
    assert _status(sandbox) == SandboxStatus.PENDING
    assert not await sandbox.is_running()

    await sandbox.start()
    assert _status(sandbox) == SandboxStatus.RUNNING
    assert await sandbox.is_running()

    # Execute command
    res = await sandbox.exec_command(["echo", "hi"])
    assert res.exit_code == 0
    assert sandbox.executed_commands == [["echo", "hi"]]

    await sandbox.stop()
    assert _status(sandbox) == SandboxStatus.STOPPED
    assert not await sandbox.is_running()


async def test_fake_sandbox_queued_responses(sample_config: SandboxConfig) -> None:
    """FakeSandboxRuntime returns queued responses in FIFO order."""
    sandbox = FakeSandboxRuntime(config=sample_config)
    await sandbox.start()

    sandbox.queue_response(exit_code=1, stdout="", stderr="command error")
    sandbox.queue_response(exit_code=0, stdout="success output")

    res1 = await sandbox.exec_command(["flake8"])
    assert res1.exit_code == 1
    assert "command error" in res1.stderr

    res2 = await sandbox.exec_command(["pytest"])
    assert res2.exit_code == 0
    assert res2.stdout == "success output"


async def test_fake_sandbox_exec_when_not_running(sample_config: SandboxConfig) -> None:
    """Attempting to execute commands on unstarted sandbox raises ContainerNotRunningError."""
    sandbox = FakeSandboxRuntime(config=sample_config)

    with pytest.raises(ContainerNotRunningError, match="not running"):
        await sandbox.exec_command(["ls"])


async def test_fake_sandbox_context_manager(sample_config: SandboxConfig) -> None:
    """Async context manager starts and stops sandbox cleanly."""
    sandbox = FakeSandboxRuntime(config=sample_config)

    async with sandbox as sb:
        assert await sb.is_running()
        await sb.exec_command(["echo", "test"])

    assert not await sandbox.is_running()


# 4. DockerSandboxRuntime Command Construction Tests


def test_docker_sandbox_container_naming(sample_config: SandboxConfig) -> None:
    """Docker container name is uniquely derived from task ID."""
    runtime = DockerSandboxRuntime(config=sample_config)
    assert runtime.container_name == "oswald-sandbox-task-42"


async def test_docker_sandbox_exec_when_not_running(sample_config: SandboxConfig) -> None:
    """Executing command on unstarted Docker runtime raises ContainerNotRunningError."""
    runtime = DockerSandboxRuntime(config=sample_config)

    with pytest.raises(ContainerNotRunningError, match="not running"):
        await runtime.exec_command(["python", "--version"])


async def test_docker_sandbox_start_nonexistent_workspace(tmp_path: Path) -> None:
    """Starting Docker runtime with nonexistent workspace path raises SandboxError."""
    bad_path = tmp_path / "nonexistent" / "dir"
    config = SandboxConfig(task_id="task-bad-dir", workspace_path=bad_path)
    runtime = DockerSandboxRuntime(config=config)

    with pytest.raises(SandboxError, match="does not exist or is not a directory"):
        await runtime.start()
