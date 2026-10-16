"""Immutable data schemas for sandbox execution environments.

Defines schemas for container security configuration, process execution outcomes,
and lifecycle state tracking.
"""

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class SandboxStatus(StrEnum):
    """Lifecycle status of a sandbox execution container."""

    PENDING = "pending"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


class SandboxConfig(BaseModel):
    """Hardened security and resource configuration for a task sandbox container."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    task_id: str = Field(description="Unique task identifier associated with this container")
    workspace_path: Path = Field(description="Host filesystem path to bind mount at /workspace")
    image: str = Field(default="python:3.12-slim", description="Base Docker image name")
    memory_limit: str = Field(default="2g", description="Maximum container memory limit (e.g. 2g)")
    cpus: float = Field(default=2.0, ge=0.1, description="Maximum CPU allocation")
    read_only_root: bool = Field(
        default=True,
        description="Whether container root filesystem is read-only",
    )
    network_disabled: bool = Field(
        default=True,
        description="Whether network access is completely disabled (--network none)",
    )
    user: str = Field(default="1000:1000", description="UID:GID to run container process as")
    tmpfs_mounts: dict[str, str] = Field(
        default_factory=lambda: {"/tmp": "rw,noexec,nosuid,size=512m"},  # noqa: S108
        description="Tmpfs writable scratch mounts for compilers and temporary files",
    )


class ExecResult(BaseModel):
    """Result of running a command inside the sandbox."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    exit_code: int = Field(description="Process exit code (0 indicates success)")
    stdout: str = Field(default="", description="Standard output stream captured from process")
    stderr: str = Field(default="", description="Standard error stream captured from process")
    duration_seconds: float = Field(
        ge=0.0,
        description="Total command execution wall-clock time in seconds",
    )
