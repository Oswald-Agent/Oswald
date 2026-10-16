"""Fake sandbox runtime for deterministic unit testing without Docker daemon."""

import asyncio
import time
from collections import deque
from pathlib import Path

from oswald.core.sandbox.exceptions import (
    ContainerNotRunningError,
    SandboxTimeoutError,
)
from oswald.core.sandbox.models import ExecResult, SandboxConfig, SandboxStatus


class FakeSandboxRuntime:
    """In-memory sandbox test double implementing the SandboxRuntime protocol."""

    def __init__(self, config: SandboxConfig, execute_locally: bool = False) -> None:
        self._config = config
        self._execute_locally = execute_locally
        self._status: SandboxStatus = SandboxStatus.PENDING
        self.executed_commands: list[list[str]] = []
        self._mock_responses: deque[ExecResult] = deque()

    @property
    def config(self) -> SandboxConfig:
        return self._config

    @property
    def status(self) -> SandboxStatus:
        return self._status

    def queue_response(
        self,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        duration_seconds: float = 0.01,
    ) -> None:
        """Queue a pre-defined command execution response for tests."""
        self._mock_responses.append(
            ExecResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration_seconds,
            )
        )

    async def start(self) -> None:
        self._status = SandboxStatus.RUNNING

    async def exec_command(
        self,
        cmd: list[str],
        timeout_seconds: float = 120.0,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        if self._status != SandboxStatus.RUNNING:
            raise ContainerNotRunningError(
                f"Container for task '{self._config.task_id}' is not running",
                task_id=self._config.task_id,
            )

        self.executed_commands.append(list(cmd))

        # Return queued mock if present
        if self._mock_responses:
            return self._mock_responses.popleft()

        # If configured to run locally (e.g. running real python/pytest in tmpdir)
        if self._execute_locally:
            start = time.perf_counter()
            workspace = Path(self._config.workspace_path)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(workspace),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout_seconds,
                )
            except TimeoutError as exc:
                raise SandboxTimeoutError(
                    f"Command timed out after {timeout_seconds}s: {' '.join(cmd)}",
                    task_id=self._config.task_id,
                ) from exc

            return ExecResult(
                exit_code=proc.returncode if proc.returncode is not None else 0,
                stdout=stdout_b.decode("utf-8", errors="replace"),
                stderr=stderr_b.decode("utf-8", errors="replace"),
                duration_seconds=time.perf_counter() - start,
            )

        # Default fast stub response
        return ExecResult(
            exit_code=0,
            stdout="Fake stdout",
            stderr="",
            duration_seconds=0.01,
        )

    async def stop(self) -> None:
        self._status = SandboxStatus.STOPPED

    async def is_running(self) -> bool:
        return self._status == SandboxStatus.RUNNING

    async def __aenter__(self) -> "FakeSandboxRuntime":
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        await self.stop()
