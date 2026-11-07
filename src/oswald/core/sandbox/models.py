"""Immutable data schemas for sandbox execution environments.

Defines schemas for container security configuration, process execution outcomes,
and lifecycle state tracking.
"""

import os
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


def _default_sandbox_user() -> str:
    uid = getattr(os, "getuid", lambda: 1000)()
    gid = getattr(os, "getgid", lambda: 1000)()
    return f"{uid}:{gid}" if uid != 0 else "1000:1000"


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

    task_id: str = Field(
        pattern=r"^[a-zA-Z0-9_\-\.]+$",
        description="Unique task identifier associated with this container",
    )
    workspace_path: Path = Field(description="Host filesystem path to bind mount at /workspace")
    image: str = Field(default="python:3.12-slim", description="Base Docker image name")
    memory_limit: str = Field(default="2g", description="Maximum container memory limit (e.g. 2g)")
    cpus: float = Field(default=2.0, ge=0.1, description="Maximum CPU allocation")
    pids_limit: int = Field(default=512, ge=32, description="Maximum container PID ceiling")
    read_only_root: bool = Field(
        default=True,
        description="Whether container root filesystem is read-only",
    )
    network_disabled: bool = Field(
        default=True,
        description="Whether network access is completely disabled (--network none)",
    )
    user: str = Field(
        default_factory=_default_sandbox_user,
        description="UID:GID to run container process as",
    )
    tmpfs_mounts: dict[str, str] = Field(
        default_factory=lambda: {"/tmp": "rw,nosuid,nodev,size=512m"},  # noqa: S108
        description="Tmpfs writable scratch mounts for compilers and temporary files",
    )
    host_cache_mounts: dict[str, str] = Field(
        default_factory=dict,
        description="Host directory to container read-only cache mount path mapping, mounted as :ro",
    )
    max_head_bytes: int = Field(
        default=2 * 1024 * 1024,  # 2 MiB
        description="Maximum initial stdout/stderr bytes retained in memory",
    )
    max_tail_bytes: int = Field(
        default=3 * 1024 * 1024,  # 3 MiB
        description="Maximum trailing stdout/stderr bytes retained in memory ring buffer",
    )
    max_total_output_bytes: int = Field(
        default=200 * 1024 * 1024,  # 200 MiB
        description="Maximum allowed total stdout/stderr bytes written to disk before aborting",
    )
    report_dir: Path | None = Field(
        default=None,
        description="Host directory for task run reports and direct execution logs",
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
