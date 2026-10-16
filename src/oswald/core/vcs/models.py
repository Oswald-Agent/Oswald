"""Immutable data schemas for Git worktree management and VCS operations.

Defines schemas representing task worktree contexts and git diff outputs.
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class WorktreeContext(BaseModel):
    """Execution context representing an isolated Git worktree for a task."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    task_id: str = Field(description="Unique task identifier associated with this worktree")
    worktree_path: Path = Field(description="Absolute local filesystem path to the worktree")
    branch_name: str = Field(description="Dedicated Git branch created for the task")
    base_ref: str = Field(description="Base commit or branch the worktree was checked out from")


class DiffResult(BaseModel):
    """Result of capturing git changes from a task worktree."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    task_id: str = Field(description="Task identifier")
    diff: str = Field(description="Standard unified git diff text")
    files_changed: list[str] = Field(
        default_factory=list,
        description="List of relative file paths modified, added, or deleted",
    )
