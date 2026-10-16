"""Core orchestrator package.

Responsibility:
    The state machine, pipeline coordination, and composition root.

Forbidden imports:
    Must not import from `oswald.api`, `oswald.workers`, or `oswald.db`.
"""

from oswald.core.orchestrator.workspace import TaskWorkspace, TaskWorkspaceCoordinator

__all__ = ["TaskWorkspace", "TaskWorkspaceCoordinator"]
