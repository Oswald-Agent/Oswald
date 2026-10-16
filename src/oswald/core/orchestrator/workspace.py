"""Task workspace lifecycle coordinator.

Unifies Git worktree isolation with Docker sandbox container execution.
Guarantees cleanup of containers and worktrees on completion or failure.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import structlog

from oswald.core.sandbox.docker_runtime import DockerSandboxRuntime
from oswald.core.sandbox.models import ExecResult, SandboxConfig
from oswald.core.sandbox.protocol import SandboxRuntime
from oswald.core.vcs.models import DiffResult, WorktreeContext
from oswald.core.vcs.worktree import WorktreeManager

SandboxFactory = Callable[[SandboxConfig], SandboxRuntime]


@dataclass
class TaskWorkspace:
    """Active task workspace combining isolated worktree and sandbox execution."""

    task_id: str
    worktree: WorktreeContext
    sandbox: SandboxRuntime
    worktree_manager: WorktreeManager
    diff_result: DiffResult | None = None

    async def exec_command(
        self,
        cmd: list[str],
        timeout_seconds: float = 120.0,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Execute command inside the task's isolated sandbox container."""
        return await self.sandbox.exec_command(
            cmd,
            timeout_seconds=timeout_seconds,
            env=env,
        )

    async def get_diff(self) -> DiffResult:
        """Capture current unified git diff for this task worktree."""
        diff = await self.worktree_manager.get_diff(self.task_id)
        self.diff_result = diff
        return diff


class TaskWorkspaceCoordinator:
    """Coordinates lifecycle of task worktrees and execution sandboxes."""

    def __init__(
        self,
        worktree_manager: WorktreeManager,
        sandbox_factory: SandboxFactory | None = None,
    ) -> None:
        self._vcs = worktree_manager
        self._sandbox_factory = sandbox_factory or (lambda cfg: DockerSandboxRuntime(config=cfg))
        self._log = structlog.get_logger().bind(component="orchestrator.workspace")

    @asynccontextmanager
    async def session(
        self,
        task_id: str,
        base_ref: str = "HEAD",
        image: str = "python:3.12-slim",
        delete_branch: bool = False,
    ) -> AsyncIterator[TaskWorkspace]:
        """Async context manager handling unified worktree and container lifecycle.

        Steps:
        1. Creates isolated Git worktree & branch for task.
        2. Configures and starts sandbox container mounting the worktree at /workspace.
        3. Yields TaskWorkspace handle.
        4. In finally block: captures diff, stops container, and prunes worktree.
        """
        self._log.info("task_workspace.starting", task_id=task_id)
        worktree = await self._vcs.create_worktree(task_id, base_ref=base_ref)

        config = SandboxConfig(
            task_id=task_id,
            workspace_path=worktree.worktree_path,
            image=image,
        )
        sandbox = self._sandbox_factory(config)
        workspace = TaskWorkspace(
            task_id=task_id,
            worktree=worktree,
            sandbox=sandbox,
            worktree_manager=self._vcs,
        )

        try:
            await sandbox.start()
            yield workspace
        finally:
            # 1. Capture final diff before destroying worktree
            try:
                workspace.diff_result = await self._vcs.get_diff(task_id)
            except Exception as exc:
                self._log.warning("task_workspace.diff_failed", task_id=task_id, error=str(exc))

            # 2. Stop and remove sandbox container
            try:
                await sandbox.stop()
            except Exception as exc:
                self._log.error(
                    "task_workspace.sandbox_cleanup_failed", task_id=task_id, error=str(exc)
                )

            # 3. Clean up worktree (preserve branch by default for review / PR)
            try:
                await self._vcs.remove_worktree(task_id, delete_branch=delete_branch)
            except Exception as exc:
                self._log.error(
                    "task_workspace.worktree_cleanup_failed", task_id=task_id, error=str(exc)
                )

            self._log.info("task_workspace.teardown_complete", task_id=task_id)
