"""Core VCS package.

Responsibility:
    GitProvider Protocol and implementations; the only place allowed to run git.

Forbidden imports:
    Must not import from `oswald.api`, `oswald.workers`, or `oswald.db`.
"""

from oswald.core.vcs.exceptions import VCSError, WorktreeError
from oswald.core.vcs.models import DiffResult, WorktreeContext
from oswald.core.vcs.worktree import WorktreeManager

__all__ = [
    "DiffResult",
    "VCSError",
    "WorktreeContext",
    "WorktreeError",
    "WorktreeManager",
]
