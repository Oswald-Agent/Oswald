"""Docker-based hardened container execution runtime.

Launches and manages task-scoped Docker containers with restricted capabilities:
- Isolated networking (--network none)
- Dropped capabilities (--cap-drop ALL)
- Read-only root filesystem
- Dedicated memory and CPU ceilings
- Task worktree bind-mounted at /workspace
"""

import asyncio
import shutil
import time
from collections.abc import Sequence
from pathlib import Path

import structlog

from oswald.core.sandbox.exceptions import (
    ContainerNotRunningError,
    SandboxError,
    SandboxTimeoutError,
)
from oswald.core.sandbox.models import ExecResult, SandboxConfig, SandboxStatus


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
        self._log = structlog.get_logger().bind(
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

    async def _run_docker(
        self,
        args: Sequence[str],
        timeout_seconds: float = 30.0,
        check: bool = True,
    ) -> tuple[int, str, str]:
        """Execute a docker CLI command asynchronously."""
        cmd = [self._docker_bin, *args]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise SandboxTimeoutError(
                f"Docker command timed out after {timeout_seconds}s: {' '.join(cmd)}",
                task_id=self._config.task_id,
            ) from exc
        except OSError as exc:
            raise SandboxError(
                f"Failed to execute docker binary '{self._docker_bin}': {exc}",
                task_id=self._config.task_id,
            ) from exc

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
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
            "--name",
            self.container_name,
            "-w",
            "/workspace",
            "-v",
            workspace_mount,
            "--memory",
            self._config.memory_limit,
            "--cpus",
            str(self._config.cpus),
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
            run_args.extend(["--tmpfs", f"{path}:{opts}"])

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

        if not await self.is_running():
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
        try:
            exit_code, stdout, stderr = await self._run_docker(
                exec_args,
                timeout_seconds=timeout_seconds,
                check=False,
            )
        except SandboxTimeoutError:
            raise SandboxTimeoutError(
                f"Command timed out after {timeout_seconds}s: {' '.join(cmd)}",
                task_id=self._config.task_id,
            ) from None
        duration = time.perf_counter() - start_time

        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
        )

    async def stop(self) -> None:
        """Terminate and remove the sandbox container."""
        self._log.info("sandbox.stopping", container=self.container_name)
        await self._run_docker(["rm", "-f", self.container_name], check=False)
        self._status = SandboxStatus.STOPPED

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
