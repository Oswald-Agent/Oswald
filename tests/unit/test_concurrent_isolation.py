"""Step 6: Concurrent task isolation tests.

Verifies that multiple tasks can operate concurrently against the same repository
without sharing mutable state, leaking changes, or conflicting during cleanup.
"""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from oswald.core.orchestrator.workspace import TaskWorkspace, TaskWorkspaceCoordinator
from oswald.core.sandbox.fake import FakeSandboxRuntime
from oswald.core.sandbox.models import SandboxConfig, SandboxStatus
from oswald.core.vcs import WorktreeManager

_GIT_BIN = shutil.which("git") or "git"


@pytest.fixture
def base_repo(tmp_path: Path) -> Path:
    """Create a git repository with base files."""
    repo = tmp_path / "concurrent_repo"
    repo.mkdir()

    def run_git(*args: str) -> None:
        subprocess.run([_GIT_BIN, *args], cwd=str(repo), check=True, capture_output=True)  # noqa: S603

    run_git("init", "-b", "main")
    run_git("config", "user.name", "Test User")
    run_git("config", "user.email", "test@oswald.local")

    (repo / "common.py").write_text("# Common baseline\nBASE_VERSION = 1\n", encoding="utf-8")
    run_git("add", ".")
    run_git("commit", "-m", "Initial commit")

    return repo


async def test_concurrent_tasks_isolation(base_repo: Path, tmp_path: Path) -> None:
    """Two tasks run concurrently on distinct worktrees with zero cross-contamination."""
    wt_root = tmp_path / "worktrees"
    worktree_mgr = WorktreeManager(repo_path=base_repo, worktree_base_dir=wt_root)

    sandboxes: dict[str, FakeSandboxRuntime] = {}

    def make_fake_sandbox(cfg: SandboxConfig) -> FakeSandboxRuntime:
        rt = FakeSandboxRuntime(config=cfg)
        sandboxes[cfg.task_id] = rt
        return rt

    coordinator = TaskWorkspaceCoordinator(
        worktree_manager=worktree_mgr,
        sandbox_factory=make_fake_sandbox,
    )

    alpha_task_id = "task-alpha"
    beta_task_id = "task-beta"

    async def run_alpha(barrier: asyncio.Barrier) -> TaskWorkspace:
        async with coordinator.session(task_id=alpha_task_id) as ws:
            # Synchronize both tasks to ensure they are concurrently active
            await barrier.wait()

            # Task Alpha writes alpha.txt
            alpha_file = ws.worktree.worktree_path / "alpha.txt"
            alpha_file.write_text("Alpha output\n", encoding="utf-8")

            # Execute command inside alpha's sandbox
            sandboxes[alpha_task_id].queue_response(exit_code=0, stdout="Alpha test passed")
            res = await ws.exec_command(["pytest", "tests/test_alpha.py"])
            assert res.stdout == "Alpha test passed"

            # Wait again before finishing to ensure both are inspecting simultaneously
            await barrier.wait()

            # Alpha worktree must NOT see beta.txt
            assert not (ws.worktree.worktree_path / "beta.txt").exists()

            # Alpha diff must only include alpha.txt
            diff = await ws.get_diff()
            assert diff.files_changed == ["alpha.txt"]
            assert "+Alpha output" in diff.diff

            return ws

    async def run_beta(barrier: asyncio.Barrier) -> TaskWorkspace:
        async with coordinator.session(task_id=beta_task_id) as ws:
            # Synchronize both tasks to ensure they are concurrently active
            await barrier.wait()

            # Task Beta writes beta.txt
            beta_file = ws.worktree.worktree_path / "beta.txt"
            beta_file.write_text("Beta output\n", encoding="utf-8")

            # Execute command inside beta's sandbox
            sandboxes[beta_task_id].queue_response(exit_code=0, stdout="Beta test passed")
            res = await ws.exec_command(["pytest", "tests/test_beta.py"])
            assert res.stdout == "Beta test passed"

            # Wait again before finishing
            await barrier.wait()

            # Beta worktree must NOT see alpha.txt
            assert not (ws.worktree.worktree_path / "alpha.txt").exists()

            # Beta diff must only include beta.txt
            diff = await ws.get_diff()
            assert diff.files_changed == ["beta.txt"]
            assert "+Beta output" in diff.diff

            return ws

    sync_barrier = asyncio.Barrier(2)
    alpha_ws, beta_ws = await asyncio.gather(
        run_alpha(sync_barrier),
        run_beta(sync_barrier),
    )

    # 1. Verification of task-specific diff attribution
    assert alpha_ws.diff_result is not None
    assert alpha_ws.diff_result.files_changed == ["alpha.txt"]

    assert beta_ws.diff_result is not None
    assert beta_ws.diff_result.files_changed == ["beta.txt"]

    # 2. Verification of distinct sandboxes
    assert sandboxes[alpha_task_id].status is SandboxStatus.STOPPED
    assert sandboxes[beta_task_id].status is SandboxStatus.STOPPED

    # 3. Verification of worktree directory cleanup
    assert not alpha_ws.worktree.worktree_path.exists()
    assert not beta_ws.worktree.worktree_path.exists()

    # 4. Git repository worktree list is clean, while branches remain for merge
    remaining = await worktree_mgr.list_worktrees()
    assert len(remaining) == 0

    _, branches_out, _ = await worktree_mgr._run_git(["branch", "--list"])
    assert worktree_mgr.get_branch_name(alpha_task_id) in branches_out
    assert worktree_mgr.get_branch_name(beta_task_id) in branches_out
