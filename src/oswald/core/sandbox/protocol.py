"""Protocol defining the interface for sandbox container runtimes.

Any concrete sandbox implementation (Docker, Fake, etc.) satisfies this protocol.
"""

from typing import Protocol, runtime_checkable

from oswald.core.sandbox.models import ExecResult, SandboxConfig, SandboxStatus


@runtime_checkable
class SandboxRuntime(Protocol):
    """Protocol satisfied by container execution backends."""

    @property
    def config(self) -> SandboxConfig:
        """Retrieve the sandbox configuration."""
        ...

    @property
    def status(self) -> SandboxStatus:
        """Current status of the sandbox environment."""
        ...

    async def start(self) -> None:
        """Provision and launch the isolated sandbox environment."""
        ...

    async def exec_command(
        self,
        cmd: list[str],
        timeout_seconds: float = 120.0,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Execute a command inside the /workspace directory of the sandbox.

        Args:
            cmd: Command arguments to execute.
            timeout_seconds: Maximum execution time before aborting.
            env: Optional additional environment variables.

        Returns:
            ExecResult containing exit code, stdout, stderr, and duration.
        """
        ...

    async def stop(self) -> None:
        """Gracefully terminate and destroy the sandbox environment."""
        ...

    async def is_running(self) -> bool:
        """Return True if the sandbox is actively running and ready to accept commands."""
        ...

    async def __aenter__(self) -> "SandboxRuntime":
        """Async context manager entry point."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Async context manager exit point ensuring safe teardown."""
        ...
