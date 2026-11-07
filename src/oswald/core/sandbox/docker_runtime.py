"""Docker-based hardened container execution runtime.

Launches and manages task-scoped Docker containers with restricted capabilities:
- Isolated networking (--network none)
- Dropped capabilities (--cap-drop ALL)
- Read-only root filesystem
- Dedicated memory and CPU ceilings
- Task worktree bind-mounted at /workspace
"""

import asyncio
import os
import shutil
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO

import structlog

from oswald.core.sandbox.exceptions import (
    ContainerNotRunningError,
    SandboxError,
    SandboxTimeoutError,
)
from oswald.core.sandbox.models import ExecResult, SandboxConfig, SandboxStatus


class StreamCollector:
    """Streams command output directly to disk if logs_dir is provided, else buffers in memory."""

    def __init__(
        self,
        logs_dir: Path | None = None,
        stream_name: str = "stdout",
        max_head: int = 2 * 1024 * 1024,
        max_tail: int = 3 * 1024 * 1024,
        max_total: int = 200 * 1024 * 1024,
        task_id: str = "",
    ) -> None:
        self._logs_dir = logs_dir
        self._stream_name = stream_name
        self._max_head = max_head
        self._max_tail = max_tail
        self._max_total = max_total
        self._task_id = task_id

        self.total_bytes = 0
        self._file: BinaryIO | None = None
        self._file_path: Path | None = None
        self.log_filename: str | None = None
        self._mem_buffer: bytearray | None = bytearray() if logs_dir is None else None

    def _ensure_no_symlinks(self, target: Path) -> None:
        if self._logs_dir is None:
            return
        for candidate in (target, self._logs_dir, self._logs_dir.parent):
            if candidate.is_symlink():
                raise SandboxError(
                    f"Refusing to access symlinked log path: {candidate}",
                    task_id=self._task_id,
                )

    def _open_log_file(self) -> BinaryIO:
        if self._logs_dir is None:
            raise SandboxError("Logs directory is not configured", task_id=self._task_id)
        self._ensure_no_symlinks(self._logs_dir)
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_no_symlinks(self._logs_dir)

        timestamp = time.time_ns()
        prefix = f"{self._task_id}_" if self._task_id else ""
        self.log_filename = f"exec_{prefix}{timestamp}_{self._stream_name}.log"
        self._file_path = self._logs_dir / self.log_filename

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._file_path, flags, 0o600)
        except OSError as exc:
            raise SandboxError(
                f"Failed to safely open log file '{self._file_path}': {exc}",
                task_id=self._task_id,
            ) from exc
        return os.fdopen(fd, "wb")

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return

        self.total_bytes += len(chunk)
        if self.total_bytes > self._max_total:
            self.close()
            raise SandboxError(
                f"Command output exceeded limit of {self._max_total} bytes",
                task_id=self._task_id,
            )

        if self._logs_dir is not None:
            if self._file is None:
                self._file = self._open_log_file()
            self._file.write(chunk)
        elif self._mem_buffer is not None:
            self._mem_buffer.extend(chunk)

    def close(self) -> None:
        if self._file is not None:
            if not self._file.closed:
                self._file.close()
            self._file = None

    def get_output(self, container_logs_prefix: str = "/workspace/.oswald/logs") -> str:
        self.close()

        if self._logs_dir is None and self._mem_buffer is not None:
            return self._mem_buffer.decode("utf-8", errors="replace")

        if self._file_path is None or not self._file_path.exists():
            return ""

        self._ensure_no_symlinks(self._file_path)
        read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self._file_path, read_flags)
        with os.fdopen(fd, "rb") as f:
            if self.total_bytes <= (self._max_head + self._max_tail):
                return f.read().decode("utf-8", errors="replace")
            head_bytes = f.read(self._max_head)
            f.seek(self.total_bytes - self._max_tail)
            tail_bytes = f.read(self._max_tail)

        head_str = head_bytes.decode("utf-8", errors="replace")
        tail_str = tail_bytes.decode("utf-8", errors="replace")
        omitted = self.total_bytes - self._max_head - self._max_tail
        container_path = f"{container_logs_prefix}/{self.log_filename}"

        marker = f"\n\n[... {omitted:,} bytes omitted. Full log saved to {container_path} ...]\n\n"
        return f"{head_str}{marker}{tail_str}"

    def __enter__(self) -> "StreamCollector":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        self.close()


async def run_docker_command(
    args: Sequence[str],
    docker_bin: str = "docker",
    timeout_seconds: float = 120.0,
    check: bool = False,
) -> tuple[int, str, str]:
    """Execute raw docker command asynchronously via authorized docker runtime module."""
    cmd = [docker_bin, *args]
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds
        )
        exit_code = proc.returncode if proc.returncode is not None else 1
        return (
            exit_code,
            stdout_bytes.decode(errors="replace"),
            stderr_bytes.decode(errors="replace"),
        )
    except Exception as exc:
        if proc is not None and proc.returncode is None:
            with suppress(Exception):
                proc.kill()
        if check:
            raise SandboxError(f"Docker command failed: {' '.join(cmd)}: {exc}") from exc
        return 1, "", str(exc)


class DockerSandboxRuntime:
    """Hardened Docker sandbox implementation conforming to SandboxRuntime protocol."""

    def __init__(
        self,
        config: SandboxConfig,
        docker_bin: str | None = None,
    ) -> None:
        self._config = config
        self._docker_bin = docker_bin or shutil.which("docker") or "docker"
        self._workspace_path = Path(config.workspace_path).resolve()
        self._status: SandboxStatus = SandboxStatus.PENDING
        self._log = structlog.get_logger(
            component="sandbox.docker",
            task_id=config.task_id,
        )

    @property
    def config(self) -> SandboxConfig:
        """Retrieve sandbox configuration."""
        return self._config

    @property
    def status(self) -> SandboxStatus:
        """Current lifecycle status."""
        return self._status

    @property
    def container_name(self) -> str:
        """Deterministic container name for the task."""
        return f"oswald-sandbox-{self._config.task_id}"

    @staticmethod
    async def _drain_stream(stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        with suppress(Exception):
            while await stream.read(64 * 1024):
                pass

    async def _read_stream(
        self,
        stream: asyncio.StreamReader | None,
        collector: StreamCollector,
    ) -> None:
        if stream is None:
            return
        with collector:
            while chunk := await stream.read(64 * 1024):
                collector.feed(chunk)

    async def _run_docker(
        self,
        args: Sequence[str],
        timeout_seconds: float = 30.0,
        check: bool = True,
        logs_dir: Path | None = None,
    ) -> tuple[int, str, str]:
        """Execute a docker CLI command asynchronously."""
        cmd = [self._docker_bin, *args]

        out_collector = StreamCollector(
            logs_dir=logs_dir,
            stream_name="stdout",
            max_head=self._config.max_head_bytes,
            max_tail=self._config.max_tail_bytes,
            max_total=self._config.max_total_output_bytes,
            task_id=self._config.task_id,
        )
        err_collector = StreamCollector(
            logs_dir=logs_dir,
            stream_name="stderr",
            max_head=self._config.max_head_bytes,
            max_tail=self._config.max_tail_bytes,
            max_total=self._config.max_total_output_bytes,
            task_id=self._config.task_id,
        )

        proc: asyncio.subprocess.Process | None = None
        out_task: asyncio.Task[None] | None = None
        err_task: asyncio.Task[None] | None = None

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_task = asyncio.create_task(self._read_stream(proc.stdout, out_collector))
            err_task = asyncio.create_task(self._read_stream(proc.stderr, err_collector))
            await asyncio.wait_for(
                asyncio.gather(proc.wait(), out_task, err_task),
                timeout=timeout_seconds,
            )
        except (TimeoutError, asyncio.CancelledError, SandboxError) as exc:
            if proc is not None and proc.returncode is None:
                with suppress(ProcessLookupError):
                    proc.kill()
            for task in (out_task, err_task):
                if task is not None and not task.done():
                    task.cancel()
            if out_task or err_task:
                await asyncio.gather(
                    *(t for t in (out_task, err_task) if t is not None),
                    return_exceptions=True,
                )
            out_collector.close()
            err_collector.close()
            if proc is not None:
                await asyncio.gather(
                    proc.wait(),
                    self._drain_stream(proc.stdout),
                    self._drain_stream(proc.stderr),
                    return_exceptions=True,
                )
            if isinstance(exc, TimeoutError):
                raise SandboxTimeoutError(
                    f"Docker command timed out after {timeout_seconds}s: {' '.join(cmd)}",
                    task_id=self._config.task_id,
                ) from exc
            raise
        except OSError as exc:
            out_collector.close()
            err_collector.close()
            raise SandboxError(
                f"Failed to execute docker binary '{self._docker_bin}': {exc}",
                task_id=self._config.task_id,
            ) from exc

        stdout = out_collector.get_output()
        stderr = err_collector.get_output()
        exit_code = proc.returncode if proc.returncode is not None else 1

        if check and exit_code != 0:
            raise SandboxError(
                f"Docker command failed ({exit_code}): {' '.join(cmd)}\nStderr: {stderr.strip()}",
                task_id=self._config.task_id,
                details=stderr.strip(),
            )

        return exit_code, stdout, stderr

    async def start(self) -> None:
        """Create and start the isolated Docker container."""
        if not self._workspace_path.is_dir():
            raise SandboxError(
                f"Workspace path does not exist or is not a directory: {self._workspace_path}",
                task_id=self._config.task_id,
            )

        # Ensure any leftover container from previous runs is purged
        await self._run_docker(["rm", "-f", self.container_name], check=False)

        # Workspace path for Docker volume mounting
        workspace_mount = f"{self._workspace_path}:/workspace:rw"

        # Build hardened security run arguments
        run_args: list[str] = [
            "run",
            "-d",
            "--init",
            "--name",
            self.container_name,
            "-w",
            "/workspace",
            "-e",
            "HOME=/tmp",
            "-v",
            workspace_mount,
            "--memory",
            self._config.memory_limit,
            "--cpus",
            str(self._config.cpus),
            "--pids-limit",
            str(self._config.pids_limit),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
        ]

        if self._config.network_disabled:
            run_args.extend(["--network", "none"])

        if self._config.read_only_root:
            run_args.append("--read-only")

        if self._config.user:
            run_args.extend(["--user", self._config.user])

        for path, opts in self._config.tmpfs_mounts.items():
            if path != "/workspace/.git":
                run_args.extend(["--tmpfs", f"{path}:{opts}"])

        # Mount immutable read-only host package caches
        for host_dir, container_mount in self._config.host_cache_mounts.items():
            run_args.extend(["-v", f"{host_dir}:{container_mount}:ro"])

        # Mask /workspace/.git to eliminate host Git hook tampering (SEC-02).
        # In a git worktree, /workspace/.git is a pointer file; mounting tmpfs directly
        # onto a file fails in runc with ENOTDIR. For files, mask with a read-only bind mount
        # of /dev/null. When .git is a directory or absent, mask with an empty read-only tmpfs.
        git_target = self._workspace_path / ".git"
        if git_target.is_file():
            run_args.extend(["-v", "/dev/null:/workspace/.git:ro"])
        else:
            run_args.extend(["--tmpfs", "/workspace/.git:ro"])

        # Base image and keep-alive command
        run_args.extend([self._config.image, "sleep", "infinity"])

        self._log.info(
            "sandbox.starting",
            container=self.container_name,
            image=self._config.image,
            workspace=str(self._workspace_path),
        )

        try:
            await self._run_docker(run_args, timeout_seconds=60.0)
            self._status = SandboxStatus.RUNNING
        except asyncio.CancelledError:
            self._status = SandboxStatus.FAILED
            await self._run_docker(["rm", "-f", self.container_name], check=False)
            raise
        except Exception as exc:
            self._status = SandboxStatus.FAILED
            await self._run_docker(["rm", "-f", self.container_name], check=False)
            raise SandboxError(
                f"Failed to launch sandbox container for task '{self._config.task_id}': {exc}",
                task_id=self._config.task_id,
            ) from exc

    async def exec_command(
        self,
        cmd: list[str],
        timeout_seconds: float = 120.0,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Execute command inside container /workspace and return output."""
        if self._status != SandboxStatus.RUNNING:
            raise ContainerNotRunningError(
                f"Cannot execute command: sandbox container '{self.container_name}' is not running",
                task_id=self._config.task_id,
            )

        exec_args = ["exec", "-i", "-w", "/workspace"]

        if env:
            for k, v in env.items():
                exec_args.extend(["-e", f"{k}={v}"])

        exec_args.append(self.container_name)
        exec_args.extend(cmd)

        start_time = time.perf_counter()
        logs_dir = (
            self._config.report_dir / "logs"
            if self._config.report_dir is not None
            else self._workspace_path / ".oswald" / "logs"
        )
        try:
            exit_code, stdout, stderr = await self._run_docker(
                exec_args,
                timeout_seconds=timeout_seconds,
                check=False,
                logs_dir=logs_dir,
            )
        except (SandboxTimeoutError, SandboxError, asyncio.CancelledError) as exc:
            await self._run_docker(["kill", self.container_name], check=False)
            self._status = SandboxStatus.FAILED
            if isinstance(exc, SandboxTimeoutError):
                raise SandboxTimeoutError(
                    f"Command timed out after {timeout_seconds}s: {' '.join(cmd)}",
                    task_id=self._config.task_id,
                ) from None
            raise
        duration = time.perf_counter() - start_time

        stderr_lower = stderr.lower()
        if (
            exit_code != 0
            and "error response from daemon:" in stderr_lower
            and ("is not running" in stderr_lower or "no such container" in stderr_lower)
        ):
            self._status = SandboxStatus.STOPPED
            raise ContainerNotRunningError(
                f"Cannot execute command: sandbox container '{self.container_name}' is not running: {stderr.strip()}",
                task_id=self._config.task_id,
            )

        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
        )

    async def stop(self) -> None:
        """Terminate and remove the sandbox container."""
        self._log.info("sandbox.stopping", container=self.container_name)
        exit_code, _, stderr = await self._run_docker(
            ["rm", "-f", self.container_name], check=False
        )
        if exit_code != 0 and "no such container" not in stderr.lower():
            self._status = SandboxStatus.FAILED
            raise SandboxError(
                f"Failed to remove sandbox container '{self.container_name}': {stderr.strip()}",
                task_id=self._config.task_id,
            )
        self._status = SandboxStatus.STOPPED

    async def isolate_network(self) -> None:
        """Lock the running container to offline air-gapped networking (--network none)."""
        if self._status != SandboxStatus.RUNNING:
            return

        exit_code, stdout, _ = await self._run_docker(
            [
                "inspect",
                "-f",
                "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}",
                self.container_name,
            ],
            check=False,
        )
        if exit_code == 0:
            networks = [
                net.strip() for net in stdout.split() if net.strip() and net.strip() != "none"
            ]
            for net in networks:
                await self._run_docker(
                    ["network", "disconnect", net, self.container_name], check=False
                )
        else:
            await self._run_docker(
                ["network", "disconnect", "bridge", self.container_name], check=False
            )

    async def is_running(self) -> bool:
        """Check if container is active and running."""
        if self._status != SandboxStatus.RUNNING:
            return False

        try:
            exit_code, stdout, _ = await self._run_docker(
                ["inspect", "-f", "{{.State.Running}}", self.container_name],
                check=False,
            )
            return exit_code == 0 and stdout.strip().lower() == "true"
        except (SandboxError, OSError):
            return False

    async def __aenter__(self) -> "DockerSandboxRuntime":
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        await self.stop()
