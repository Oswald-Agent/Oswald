"""Workspace diff protocol and adapters for extracting git diffs across tasks."""

from typing import Protocol, runtime_checkable

from oswald.core.vcs.models import DiffResult
from oswald.core.vcs.worktree import WorktreeManager


@runtime_checkable
class WorkspaceDiff(Protocol):
    """Protocol for querying diffs and modified files for a task worktree."""

    async def diff_since_base(self, task_id: str) -> str:
        """Capture unified git diff against the task's base commit."""
        ...

    async def changed_files(self, task_id: str) -> list[str]:
        """Capture list of relative file paths changed against base commit."""
        ...


class WorktreeWorkspaceDiff:
    """Adapts WorktreeManager to satisfy the WorkspaceDiff protocol."""

    def __init__(self, worktree_manager: WorktreeManager) -> None:
        self._manager = worktree_manager

    async def diff_since_base(self, task_id: str) -> str:
        res: DiffResult = await self._manager.get_diff(task_id)
        return res.diff

    async def changed_files(self, task_id: str) -> list[str]:
        res: DiffResult = await self._manager.get_diff(task_id)
        return res.files_changed


class FakeWorkspaceDiff:
    """Deterministic in-memory fake for WorkspaceDiff protocol."""

    def __init__(
        self,
        diff: str = "",
        changed_files: list[str] | None = None,
    ) -> None:
        self._diff = diff
        self._changed_files = list(changed_files) if changed_files is not None else []

    def set_diff(self, diff: str, changed_files: list[str]) -> None:
        self._diff = diff
        self._changed_files = list(changed_files)

    async def diff_since_base(self, task_id: str) -> str:
        return self._diff

    async def changed_files(self, task_id: str) -> list[str]:
        return list(self._changed_files)
