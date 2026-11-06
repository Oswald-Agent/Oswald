import asyncio
import os
import sys
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock

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
    SandboxTimeoutError,
)
from oswald.core.sandbox.docker_runtime import StreamCollector


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

    expected_uid = getattr(os, "getuid", lambda: 1000)()
    expected_gid = getattr(os, "getgid", lambda: 1000)()
    expected_user = f"{expected_uid}:{expected_gid}" if expected_uid != 0 else "1000:1000"

    assert cfg.task_id == "task-1"
    assert cfg.image == "python:3.12-slim"
    assert cfg.memory_limit == "2g"
    assert cfg.cpus == 2.0
    assert cfg.pids_limit == 512
    assert cfg.read_only_root is True
    assert cfg.network_disabled is True
    assert cfg.user == expected_user
    assert cfg.tmpfs_mounts.get("/tmp") == "rw,nosuid,nodev,size=512m"  # noqa: S108
    assert cfg.max_head_bytes == 2 * 1024 * 1024
    assert cfg.max_tail_bytes == 3 * 1024 * 1024
    assert cfg.max_total_output_bytes == 200 * 1024 * 1024


def test_sandbox_config_invalid_task_id(tmp_path: Path) -> None:
    """SandboxConfig rejects path traversal and malformed task IDs."""
    with pytest.raises(ValueError):
        SandboxConfig(task_id="../escape", workspace_path=tmp_path)

    with pytest.raises(ValueError):
        SandboxConfig(task_id="task with spaces", workspace_path=tmp_path)


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


async def test_docker_sandbox_start_cancellation_cleans_up(
    sample_config: SandboxConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling start() triggers docker rm -f teardown, sets status FAILED, and re-raises."""
    runtime = DockerSandboxRuntime(config=sample_config)
    calls: list[list[str]] = []

    async def mock_run_docker(args: object, **kwargs: object) -> tuple[int, str, str]:
        if isinstance(args, (list, tuple)):
            calls.append(list(args))
            if "run" in args:
                raise asyncio.CancelledError()
        return 0, "", ""

    monkeypatch.setattr(runtime, "_run_docker", mock_run_docker)

    with pytest.raises(asyncio.CancelledError):
        await runtime.start()

    assert runtime.status == SandboxStatus.FAILED
    assert any("rm" in c and "-f" in c and runtime.container_name in c for c in calls)


async def test_docker_sandbox_masks_git_directory_with_tmpfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker runtime masks /workspace/.git with a read-only tmpfs mount when absent or a directory."""
    workspace = tmp_path / "workspace_dir"
    workspace.mkdir()
    config = SandboxConfig(task_id="task-mask-dir", workspace_path=workspace)
    runtime = DockerSandboxRuntime(config=config)

    calls: list[list[str]] = []

    async def mock_run_docker(args: object, **kwargs: object) -> tuple[int, str, str]:
        if isinstance(args, (list, tuple)):
            calls.append(list(args))
        return 0, "", ""

    monkeypatch.setattr(runtime, "_run_docker", mock_run_docker)
    await runtime.start()

    run_cmd = next(c for c in calls if c[0] == "run")
    assert "--tmpfs" in run_cmd
    tmpfs_idx = run_cmd.index("--tmpfs")
    assert "/workspace/.git:ro" in run_cmd[tmpfs_idx:]

    # Also test when .git is an existing directory
    git_dir = workspace / ".git"
    git_dir.mkdir()
    calls.clear()
    await runtime.start()
    run_cmd_dir = next(c for c in calls if c[0] == "run")
    assert "/workspace/.git:ro" in run_cmd_dir


async def test_docker_sandbox_masks_git_worktree_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker runtime masks worktree pointer file with read-only /dev/null bind mount."""
    workspace = tmp_path / "workspace_wt"
    workspace.mkdir()
    (workspace / ".git").write_text("gitdir: /host/repo/.git/worktrees/wt\n")
    config = SandboxConfig(task_id="task-mask-file", workspace_path=workspace)
    runtime = DockerSandboxRuntime(config=config)

    calls: list[list[str]] = []

    async def mock_run_docker(args: object, **kwargs: object) -> tuple[int, str, str]:
        if isinstance(args, (list, tuple)):
            calls.append(list(args))
        return 0, "", ""

    monkeypatch.setattr(runtime, "_run_docker", mock_run_docker)
    await runtime.start()

    run_cmd = next(c for c in calls if c[0] == "run")
    assert "-v" in run_cmd
    assert "/dev/null:/workspace/.git:ro" in run_cmd


def test_stream_collector_small_output(tmp_path: Path) -> None:
    """Small output <= max_head + max_tail is returned in full and preserved on disk."""
    logs_dir = tmp_path / ".oswald" / "logs"
    collector = StreamCollector(logs_dir=logs_dir, stream_name="stdout", max_head=100, max_tail=50)

    collector.feed(b"Small output line 1\n")
    collector.feed(b"Small output line 2\n")

    res = collector.get_output()
    assert res == "Small output line 1\nSmall output line 2\n"
    assert logs_dir.exists()
    assert collector.log_filename is not None
    saved_file = logs_dir / collector.log_filename
    assert saved_file.exists()
    assert saved_file.read_text() == "Small output line 1\nSmall output line 2\n"


def test_stream_collector_empty_output(tmp_path: Path) -> None:
    """Empty output returns empty string and creates no file on disk."""
    logs_dir = tmp_path / ".oswald" / "logs"
    collector = StreamCollector(logs_dir=logs_dir, stream_name="stdout")
    assert collector.get_output() == ""
    assert not logs_dir.exists()


def test_stream_collector_large_output_spill(tmp_path: Path) -> None:
    """Large output exceeding max_head spills full log to disk and formats head + tail in memory."""
    logs_dir = tmp_path / ".oswald" / "logs"
    collector = StreamCollector(
        logs_dir=logs_dir,
        stream_name="stdout",
        max_head=20,
        max_tail=20,
        max_total=1000,
    )

    collector.feed(b"HEAD_SECTION_1234567")  # 20 bytes: fills head
    collector.feed(b"MIDDLE_SECTION_LONG_OMITTED_DATA_HERE")  # 37 bytes: spilled to disk
    collector.feed(b"TAIL_SECTION_7654321")  # 20 bytes: fills tail

    res = collector.get_output()
    # Verifies head and tail are present in memory
    assert "HEAD_SECTION_1234567" in res
    assert "TAIL_SECTION_7654321" in res
    assert "bytes omitted. Full log saved to /workspace/.oswald/logs/" in res

    # Verifies full raw log is preserved on disk
    assert logs_dir.exists()
    assert collector.log_filename is not None
    saved_file = logs_dir / collector.log_filename
    assert saved_file.exists()
    assert (
        saved_file.read_bytes()
        == b"HEAD_SECTION_1234567MIDDLE_SECTION_LONG_OMITTED_DATA_HERETAIL_SECTION_7654321"
    )


def test_stream_collector_total_output_exceeded(tmp_path: Path) -> None:
    """StreamCollector raises SandboxError when total output exceeds max_total."""
    logs_dir = tmp_path / ".oswald" / "logs"
    collector = StreamCollector(
        logs_dir=logs_dir,
        stream_name="stdout",
        max_head=10,
        max_tail=10,
        max_total=50,
    )

    with pytest.raises(SandboxError, match="exceeded limit of 50 bytes"):
        collector.feed(b"x" * 60)


async def test_docker_sandbox_exec_timeout_kills_container(
    sample_config: SandboxConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SandboxTimeoutError during exec_command terminates container to stop runaway processes."""
    runtime = DockerSandboxRuntime(config=sample_config)
    runtime._status = SandboxStatus.RUNNING

    calls: list[list[str]] = []

    async def mock_run_docker(args: object, **kwargs: object) -> tuple[int, str, str]:
        if isinstance(args, (list, tuple)):
            calls.append(list(args))
            if "exec" in args:
                raise SandboxTimeoutError("timed out", task_id="task-42")
        return 0, "", ""

    monkeypatch.setattr(runtime, "_run_docker", mock_run_docker)
    monkeypatch.setattr(runtime, "is_running", AsyncMock(return_value=True))

    with pytest.raises(SandboxTimeoutError):
        await runtime.exec_command(["sleep", "100"])

    assert runtime.status == SandboxStatus.FAILED
    assert any("kill" in c and runtime.container_name in c for c in calls)


async def test_fake_sandbox_timeout_kills_process(sample_config: SandboxConfig) -> None:
    """FakeSandboxRuntime with execute_locally=True terminates process on timeout."""
    sandbox = FakeSandboxRuntime(config=sample_config, execute_locally=True)
    await sandbox.start()

    with pytest.raises(SandboxTimeoutError):
        await sandbox.exec_command(
            ["python", "-c", "import time; time.sleep(10)"],
            timeout_seconds=0.1,
        )


async def test_docker_sandbox_exec_cancellation_kills_container(
    sample_config: SandboxConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling exec_command terminates container to prevent runaway background execution."""
    runtime = DockerSandboxRuntime(config=sample_config)
    runtime._status = SandboxStatus.RUNNING

    calls: list[list[str]] = []

    async def mock_run_docker(args: object, **kwargs: object) -> tuple[int, str, str]:
        if isinstance(args, (list, tuple)):
            calls.append(list(args))
            if "exec" in args:
                raise asyncio.CancelledError()
        return 0, "", ""

    monkeypatch.setattr(runtime, "_run_docker", mock_run_docker)
    monkeypatch.setattr(runtime, "is_running", AsyncMock(return_value=True))

    with pytest.raises(asyncio.CancelledError):
        await runtime.exec_command(["sleep", "100"])

    assert runtime.status == SandboxStatus.FAILED
    assert any("kill" in c and runtime.container_name in c for c in calls)


async def test_docker_run_early_cancellation_clean(
    sample_config: SandboxConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during subprocess creation does not raise UnboundLocalError."""
    runtime = DockerSandboxRuntime(config=sample_config)

    async def mock_create_subprocess_exec(*args: object, **kwargs: object) -> object:
        raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_create_subprocess_exec)

    with pytest.raises(asyncio.CancelledError):
        await runtime._run_docker(["info"])


def test_stream_collector_rejects_symlinked_logs_dir(tmp_path: Path) -> None:
    """StreamCollector refuses to write spill logs through a symlinked log directory."""
    outside_dir = tmp_path / "outside_host_dir"
    outside_dir.mkdir()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    oswald_dir = workspace / ".oswald"
    oswald_dir.mkdir()
    symlink_logs = oswald_dir / "logs"
    symlink_logs.symlink_to(outside_dir)

    collector = StreamCollector(logs_dir=symlink_logs, stream_name="stdout")
    with pytest.raises(SandboxError, match="symlink"):
        collector.feed(b"malicious output")

    assert list(outside_dir.iterdir()) == []


async def test_run_docker_continuous_output_exceeding_cap_does_not_hang(tmp_path: Path) -> None:
    """Exceeding max_total_output_bytes on a noisy real subprocess drains pipes and exits cleanly."""
    config = SandboxConfig(
        task_id="task-cap",
        workspace_path=tmp_path,
        max_head_bytes=1024,
        max_tail_bytes=1024,
        max_total_output_bytes=4096,
    )
    runtime = DockerSandboxRuntime(config=config, docker_bin=sys.executable)

    with pytest.raises(SandboxError, match="exceeded limit"):
        await asyncio.wait_for(
            runtime._run_docker(
                [
                    "-c",
                    "import sys; data = b'x' * 65536\nwhile True: sys.stdout.buffer.write(data)",
                ],
                timeout_seconds=10.0,
            ),
            timeout=3.0,
        )


async def test_fake_sandbox_timeout_and_stop_kill_child_processes(
    sample_config: SandboxConfig,
) -> None:
    """FakeSandboxRuntime terminates spawned child processes on timeout and stop()."""
    sandbox = FakeSandboxRuntime(config=sample_config, execute_locally=True)
    await sandbox.start()
    marker_file = sample_config.workspace_path / "child_heartbeat.txt"

    script = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', 'import pathlib, time; p = pathlib.Path(\"{marker_file}\");\\nwhile True:\\n p.write_text(str(time.time())); time.sleep(0.02)'])\n"
        "time.sleep(10)\n"
    )

    with pytest.raises(SandboxTimeoutError):
        await sandbox.exec_command([sys.executable, "-c", script], timeout_seconds=0.2)

    if marker_file.exists():
        marker_file.unlink()
    await asyncio.sleep(0.1)
    assert not marker_file.exists()

    marker_file2 = sample_config.workspace_path / "child_heartbeat2.txt"
    script2 = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', 'import pathlib, time; p = pathlib.Path(\"{marker_file2}\");\\nwhile True:\\n p.write_text(str(time.time())); time.sleep(0.02)'])\n"
        "time.sleep(10)\n"
    )
    task = asyncio.create_task(
        sandbox.exec_command([sys.executable, "-c", script2], timeout_seconds=10.0)
    )
    await asyncio.sleep(0.1)
    await sandbox.stop()
    task.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await task
    if marker_file2.exists():
        marker_file2.unlink()
    await asyncio.sleep(0.1)
    assert not marker_file2.exists()


async def test_docker_sandbox_exec_sandbox_error_kills_container(
    sample_config: SandboxConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SandboxError during exec_command terminates container to stop runaway processes."""
    runtime = DockerSandboxRuntime(config=sample_config)
    runtime._status = SandboxStatus.RUNNING

    calls: list[list[str]] = []

    async def mock_run_docker(args: object, **kwargs: object) -> tuple[int, str, str]:
        if isinstance(args, (list, tuple)):
            calls.append(list(args))
            if "exec" in args:
                raise SandboxError("output cap exceeded", task_id="task-42")
        return 0, "", ""

    monkeypatch.setattr(runtime, "_run_docker", mock_run_docker)

    with pytest.raises(SandboxError):
        await runtime.exec_command(["sleep", "100"])

    assert runtime.status == SandboxStatus.FAILED
    assert any("kill" in c and runtime.container_name in c for c in calls)


async def test_docker_stop_reports_removal_failure(sample_config: SandboxConfig) -> None:
    runtime = DockerSandboxRuntime(sample_config)
    runtime._run_docker = AsyncMock(return_value=(1, "", "daemon unavailable"))  # type: ignore[method-assign]
    with pytest.raises(SandboxError, match="daemon unavailable"):
        await runtime.stop()
    assert _status(runtime) == SandboxStatus.FAILED


async def test_docker_stop_is_idempotent_when_container_absent(
    sample_config: SandboxConfig,
) -> None:
    runtime = DockerSandboxRuntime(sample_config)
    runtime._run_docker = AsyncMock(return_value=(1, "", "Error: No such container: task"))  # type: ignore[method-assign]
    await runtime.stop()
    assert _status(runtime) == SandboxStatus.STOPPED
