"""Unit tests for the TaskWorkspaceCoordinator."""

import shutil
import subprocess
from pathlib import Path

import pytest

from oswald.core.orchestrator.workspace import TaskWorkspaceCoordinator
from oswald.core.sandbox.fake import FakeSandboxRuntime
from oswald.core.sandbox.models import SandboxConfig, SandboxStatus
from oswald.core.vcs import WorktreeManager

_GIT_BIN = shutil.which("git") or "git"


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a clean local git repository with an initial commit."""
    repo = tmp_path / "test_repo"
    repo.mkdir()

    def run_git(*args: str) -> None:
        subprocess.run([_GIT_BIN, *args], cwd=str(repo), check=True, capture_output=True)  # noqa: S603

    run_git("init", "-b", "main")
    run_git("config", "user.name", "Test User")
    run_git("config", "user.email", "test@oswald.local")

    readme = repo / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    run_git("add", ".")
    run_git("commit", "-m", "Initial commit")

    return repo


@pytest.fixture
def worktree_mgr(git_repo: Path, tmp_path: Path) -> WorktreeManager:
    wt_root = tmp_path / "worktrees"
    return WorktreeManager(repo_path=git_repo, worktree_base_dir=wt_root)


async def test_coordinator_session_lifecycle(worktree_mgr: WorktreeManager) -> None:
    """Coordinator properly creates worktree and sandbox, routes commands, and cleans up."""
    fake_sandboxes: list[FakeSandboxRuntime] = []

    def make_fake_sandbox(cfg: SandboxConfig) -> FakeSandboxRuntime:
        rt = FakeSandboxRuntime(config=cfg)
        fake_sandboxes.append(rt)
        return rt

    coordinator = TaskWorkspaceCoordinator(
        worktree_manager=worktree_mgr,
        sandbox_factory=make_fake_sandbox,
    )

    task_id = "task-session-1"

    async with coordinator.session(task_id=task_id) as ws:
        assert ws.task_id == task_id
        assert ws.worktree.worktree_path.is_dir()
        assert len(fake_sandboxes) == 1
        sandbox = fake_sandboxes[0]
        running_status = sandbox.status
        assert running_status == SandboxStatus.RUNNING
        assert sandbox.config.workspace_path == ws.worktree.worktree_path

        # Test command execution routing
        sandbox.queue_response(exit_code=0, stdout="test output")
        result = await ws.exec_command(["python", "--version"])
        assert result.exit_code == 0
        assert result.stdout == "test output"
        assert sandbox.executed_commands == [["python", "--version"]]

        # Test mutating a file and capturing diff
        new_file = ws.worktree.worktree_path / "patch.py"
        new_file.write_text("print('hello')\n", encoding="utf-8")

        diff = await ws.get_diff()
        assert "patch.py" in diff.files_changed
        assert "+print('hello')" in diff.diff
        assert ws.diff_result == diff

    # After session exits:
    # 1. Container must be stopped
    stopped_status = sandbox.status
    assert stopped_status == SandboxStatus.STOPPED
    # 2. Worktree directory must be removed
    assert not ws.worktree.worktree_path.exists()
    # 3. Final diff must still be preserved on workspace handle
    assert ws.diff_result is not None
    assert "patch.py" in ws.diff_result.files_changed
    # 4. Task branch is preserved by default for PR/merge
    branch_name = worktree_mgr.get_branch_name(task_id)
    _, branch_check, _ = await worktree_mgr._run_git(["branch", "--list", branch_name])
    assert branch_name in branch_check


async def test_coordinator_session_delete_branch(worktree_mgr: WorktreeManager) -> None:
    """Coordinator deletes branch when delete_branch=True."""
    coordinator = TaskWorkspaceCoordinator(
        worktree_manager=worktree_mgr,
        sandbox_factory=lambda cfg: FakeSandboxRuntime(config=cfg),
    )
    task_id = "task-del-br"
    async with coordinator.session(task_id=task_id, delete_branch=True) as ws:
        assert ws.worktree.worktree_path.is_dir()

    branch_name = worktree_mgr.get_branch_name(task_id)
    _, branch_check, _ = await worktree_mgr._run_git(["branch", "--list", branch_name])
    assert branch_name not in branch_check


async def test_coordinator_cleanup_on_error(worktree_mgr: WorktreeManager) -> None:
    """Coordinator guarantees container stop and worktree removal even when an error occurs."""
    fake_sandboxes: list[FakeSandboxRuntime] = []

    def make_fake_sandbox(cfg: SandboxConfig) -> FakeSandboxRuntime:
        rt = FakeSandboxRuntime(config=cfg)
        fake_sandboxes.append(rt)
        return rt

    coordinator = TaskWorkspaceCoordinator(
        worktree_manager=worktree_mgr,
        sandbox_factory=make_fake_sandbox,
    )

    task_id = "task-err-test"

    with pytest.raises(RuntimeError, match="pipeline crashed"):
        async with coordinator.session(task_id=task_id) as ws:
            wt_path = ws.worktree.worktree_path
            assert wt_path.is_dir()
            raise RuntimeError("pipeline crashed")

    # Assert resources are freed
    assert len(fake_sandboxes) == 1
    assert fake_sandboxes[0].status == SandboxStatus.STOPPED
    assert not wt_path.exists()
