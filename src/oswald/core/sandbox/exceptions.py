"""Exception hierarchy for sandbox container execution."""


class SandboxError(Exception):
    """Base exception for all sandbox runtime failures."""

    def __init__(self, message: str, *, task_id: str = "", details: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.task_id = task_id
        self.details = details


class SandboxTimeoutError(SandboxError):
    """Raised when a command executed inside the sandbox exceeds its allocated timeout."""


class ContainerNotRunningError(SandboxError):
    """Raised when attempting to execute a command on an unstarted or stopped container."""
