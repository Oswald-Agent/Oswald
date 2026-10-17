"""Exception hierarchy for Git and VCS operations."""


class VCSError(Exception):
    """Base exception for all VCS and worktree errors."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class WorktreeError(VCSError):
    """Raised when creating, operating on, or removing a Git worktree fails."""

    def __init__(self, message: str, *, task_id: str = "", details: str = "") -> None:
        super().__init__(message)
        self.task_id = task_id
        self.details = details
