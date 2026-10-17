"""Unit tests for Git Worktree Manager."""

import shutil
import subprocess
from pathlib import Path

import pytest

from oswald.core.vcs import (
    DiffResult,
    WorktreeContext,
    WorktreeError,
    WorktreeManager,
)

_GIT_BIN = shutil.which("git") or "git"


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a clean local git repository with an initial commit."""
    repo = tmp_path / "test_repo"
    repo.mkdir()

    def run_git(*args: str) -> None:
        subprocess.run([_GIT_BIN, *args], cwd=str(repo), check=True, capture_output=True)  # noqa: S603

    # Configure local git repository
    run_git("init", "-b", "main")
    run_git("config", "user.name", "Test User")
    run_git("config", "user.email", "test@oswald.local")

    # Initial commit
    readme = repo / "README.md"
    readme.write_text("# Test Repo\nInitial content\n", encoding="utf-8")

    src = repo / "src"
    src.mkdir()
    (src / "app.py").write_text("def run():\n    return 42\n", encoding="utf-8")

    run_git("add", ".")
    run_git("commit", "-m", "Initial commit")

    return repo


@pytest.fixture
def manager(git_repo: Path, tmp_path: Path) -> WorktreeManager:
    """Provide a WorktreeManager targeting a temporary worktree root."""
    wt_root = tmp_path / "worktrees"
    return WorktreeManager(repo_path=git_repo, worktree_base_dir=wt_root)


# Creation & Context Tests


async def test_create_worktree_success(manager: WorktreeManager, git_repo: Path) -> None:
    """Creates a worktree with the dedicated branch and correct directory layout."""
    ctx = await manager.create_worktree("task-100")

    assert isinstance(ctx, WorktreeContext)
    assert ctx.task_id == "task-100"
    assert ctx.branch_name == "oswald/task-task-100"
    assert ctx.worktree_path.is_dir()
    assert (ctx.worktree_path / "README.md").exists()
    assert (ctx.worktree_path / "src" / "app.py").exists()


async def test_invalid_task_id_rejected(manager: WorktreeManager) -> None:
    """Rejects malformed task IDs with directory traversal characters."""
    with pytest.raises(WorktreeError, match="Invalid task_id"):
        await manager.create_worktree("../escape_dir")

    with pytest.raises(WorktreeError, match="Invalid task_id"):
        await manager.create_worktree("task with spaces")

    with pytest.raises(WorktreeError, match="Invalid task_id"):
        await manager.create_worktree("")


async def test_invalid_repo_path_raises(tmp_path: Path) -> None:
    """Raises WorktreeError if target path is not a git repository."""
    non_git_dir = tmp_path / "empty_dir"
    non_git_dir.mkdir()

    with pytest.raises(WorktreeError, match="not a valid Git repository"):
        WorktreeManager(repo_path=non_git_dir)


# Concurrency & Worktree Isolation Tests


async def test_concurrent_worktrees_isolation(manager: WorktreeManager) -> None:
    """Two concurrent worktrees mutate files independently without cross-talk."""
    wt1 = await manager.create_worktree("task-A")
    wt2 = await manager.create_worktree("task-B")

    # Modify file in Task A
    (wt1.worktree_path / "src" / "app.py").write_text("def run():\n    return 'from_A'\n")

    # Add new file in Task B
    (wt2.worktree_path / "new_feature.py").write_text("# Feature B")

    # Assert Task A changes do not exist in Task B
    assert (wt1.worktree_path / "src" / "app.py").read_text() == "def run():\n    return 'from_A'\n"
    assert (wt2.worktree_path / "src" / "app.py").read_text() == "def run():\n    return 42\n"
    assert not (wt1.worktree_path / "new_feature.py").exists()
    assert (wt2.worktree_path / "new_feature.py").exists()

    # Cleanup
    await manager.remove_worktree("task-A")
    await manager.remove_worktree("task-B")


# Diff Capture Tests


async def test_get_diff_modified_and_new_files(manager: WorktreeManager) -> None:
    """Captures unified diff for both modified existing files and untracked new files."""
    ctx = await manager.create_worktree("task-diff")

    # Modify existing file
    (ctx.worktree_path / "README.md").write_text("# Modified Header\nNew line\n")

    # Add brand-new file
    (ctx.worktree_path / "src" / "extra.py").write_text("X = 100\n")

    diff_result: DiffResult = await manager.get_diff("task-diff")

    assert diff_result.task_id == "task-diff"
    assert "README.md" in diff_result.files_changed
    assert "src/extra.py" in [f.replace("\\", "/") for f in diff_result.files_changed]
    assert "+# Modified Header" in diff_result.diff
    assert "+X = 100" in diff_result.diff

    await manager.remove_worktree("task-diff")


async def test_get_diff_nonexistent_worktree_raises(manager: WorktreeManager) -> None:
    """Raises WorktreeError if trying to capture diff on nonexistent task."""
    with pytest.raises(WorktreeError, match="worktree directory does not exist"):
        await manager.get_diff("nonexistent-task")


# Listing and Removal Tests


async def test_list_and_remove_worktree(manager: WorktreeManager) -> None:
    """Lists active worktrees and cleans them up completely."""
    wt = await manager.create_worktree("task-listing")

    active_worktrees = await manager.list_worktrees()
    assert any(w.task_id == "task-listing" for w in active_worktrees)

    # Remove worktree
    await manager.remove_worktree("task-listing")

    assert not wt.worktree_path.exists()
    active_after = await manager.list_worktrees()
    assert not any(w.task_id == "task-listing" for w in active_after)


# Context Manager Lifecycle


async def test_session_context_manager(manager: WorktreeManager) -> None:
    """Async session context manager creates and automatically cleans up worktrees."""
    saved_path: Path | None = None

    async with manager.session("task-ctx") as wt:
        assert wt.worktree_path.is_dir()
        saved_path = wt.worktree_path

    assert saved_path is not None
    assert not saved_path.exists()


async def test_session_context_manager_cleans_on_exception(manager: WorktreeManager) -> None:
    """Async session cleans up resources even if an error is raised inside the block."""
    saved_path: Path | None = None

    with pytest.raises(RuntimeError, match="Simulated agent failure"):
        async with manager.session("task-err") as wt:
            saved_path = wt.worktree_path
            assert saved_path.is_dir()
            raise RuntimeError("Simulated agent failure")

    assert saved_path is not None
    assert not saved_path.exists()


async def test_create_worktree_with_stale_branch(manager: WorktreeManager, git_repo: Path) -> None:
    """create_worktree cleans up leftover branch from an aborted run and succeeds."""
    task_id = "task-stale-branch"
    branch_name = manager.get_branch_name(task_id)

    # Manually create a conflicting branch in git repository without a worktree directory
    await manager._run_git(["branch", branch_name])

    # create_worktree should clean the branch up and succeed
    ctx = await manager.create_worktree(task_id)
    assert ctx.branch_name == branch_name
    assert ctx.worktree_path.is_dir()

    await manager.remove_worktree(task_id)
