"""Git worktree manager for task-level workspace and branch isolation.

Creates, inspects, diffs, and cleans up task-specific Git worktrees in a designated
temporary directory, allowing concurrent agent tasks on the same repository with zero
filesystem collision or lock contention.
"""

import asyncio
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog

from oswald.core.vcs.exceptions import WorktreeError
from oswald.core.vcs.models import DiffResult, WorktreeContext

_TASK_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.]+$")


class WorktreeManager:
    """Manages task-isolated Git worktrees and branches.

    Each task gets its own dedicated worktree directory and branch
    (oswald/task-<task_id>). All mutations and tests occur strictly inside this
    isolated folder.
    """

    def __init__(
        self,
        repo_path: Path,
        worktree_base_dir: Path | None = None,
        command_timeout_seconds: float = 30.0,
    ) -> None:
        self.repo_path = repo_path.resolve()
        if not (self.repo_path / ".git").exists() and not self._is_git_repo():
            raise WorktreeError(f"Provided path is not a valid Git repository: {self.repo_path}")

        if worktree_base_dir is not None:
            self.worktree_base_dir = worktree_base_dir.resolve()
        else:
            self.worktree_base_dir = Path(tempfile.gettempdir()) / "oswald_worktrees"

        self.worktree_base_dir.mkdir(parents=True, exist_ok=True)
        self._timeout = command_timeout_seconds
        self._log = structlog.get_logger().bind(component="vcs.worktree")

    def _is_git_repo(self) -> bool:
        """Check if repo_path is inside a git work tree or repository."""
        return (self.repo_path / ".git").is_dir() or (self.repo_path / ".git").is_file()

    def _validate_task_id(self, task_id: str) -> None:
        """Enforce strict safe naming for task IDs to prevent path traversal."""
        clean = task_id.strip()
        if not clean or not _TASK_ID_PATTERN.match(clean):
            raise WorktreeError(
                f"Invalid task_id '{task_id}'. Must be alphanumeric with dashes/underscores/dots only."
            )

    def get_worktree_path(self, task_id: str) -> Path:
        """Derive the deterministic filesystem path for a task worktree."""
        self._validate_task_id(task_id)
        return self.worktree_base_dir / f"task-{task_id}"

    def get_branch_name(self, task_id: str) -> str:
        """Derive the deterministic Git branch name for a task worktree."""
        self._validate_task_id(task_id)
        return f"oswald/task-{task_id}"

    async def _run_git(
        self,
        args: list[str],
        cwd: Path | None = None,
        check: bool = True,
    ) -> tuple[int, str, str]:
        """Execute a git command asynchronously with timeout and error capturing."""
        working_dir = cwd or self.repo_path
        cmd = ["git", *args]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(working_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._timeout,
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise WorktreeError(
                f"Git command timed out after {self._timeout}s: {' '.join(cmd)}"
            ) from exc
        except OSError as exc:
            raise WorktreeError(f"Failed to execute git process: {exc}") from exc

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        exit_code = proc.returncode if proc.returncode is not None else 1

        if check and exit_code != 0:
            raise WorktreeError(
                f"Git command failed ({exit_code}): {' '.join(cmd)}\nStderr: {stderr.strip()}",
                details=stderr.strip(),
            )

        return exit_code, stdout, stderr

    async def create_worktree(
        self,
        task_id: str,
        base_ref: str = "HEAD",
    ) -> WorktreeContext:
        """Create a dedicated Git worktree and branch for a task.

        Args:
            task_id: Unique task identifier.
            base_ref: Commit SHA, branch, or tag to branch from (defaults to HEAD).

        Returns:
            WorktreeContext representing the active worktree.

        Raises:
            WorktreeError: If creation fails or task_id is invalid.
        """
        self._validate_task_id(task_id)
        worktree_path = self.get_worktree_path(task_id)
        branch_name = self.get_branch_name(task_id)

        # Clean up any leftover previous run with the same task_id
        await self.remove_worktree(task_id)

        # Resolve base commit SHA
        _, base_sha, _ = await self._run_git(["rev-parse", base_ref])
        base_commit = base_sha.strip()

        self._log.info(
            "worktree.creating",
            task_id=task_id,
            branch=branch_name,
            path=str(worktree_path),
            base_commit=base_commit,
        )

        try:
            await self._run_git(
                [
                    "worktree",
                    "add",
                    "-b",
                    branch_name,
                    str(worktree_path),
                    base_commit,
                ]
            )
        except WorktreeError as exc:
            # Rollback: delete branch if created and prune
            await self._run_git(["branch", "-D", branch_name], check=False)
            await self._run_git(["worktree", "prune"], check=False)
            if worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)
            raise WorktreeError(
                f"Failed to create worktree for task '{task_id}': {exc.message}",
                task_id=task_id,
                details=exc.details,
            ) from exc

        return WorktreeContext(
            task_id=task_id,
            worktree_path=worktree_path,
            branch_name=branch_name,
            base_ref=base_commit,
        )

    async def get_diff(self, task_id: str) -> DiffResult:
        """Capture the unified git diff and modified files list for a task.

        Runs inside the task worktree. Uses `git add -N .` to ensure newly
        created untracked files are included in the diff.

        Args:
            task_id: Unique task identifier.

        Returns:
            DiffResult containing unified diff text and list of touched files.
        """
        worktree_path = self.get_worktree_path(task_id)
        if not worktree_path.is_dir():
            raise WorktreeError(
                f"Cannot capture diff: worktree directory does not exist for task '{task_id}'",
                task_id=task_id,
            )

        # Intent-to-add so git diff includes newly added files
        await self._run_git(["add", "-N", "."], cwd=worktree_path, check=False)

        # Capture raw unified diff against HEAD
        _, diff_text, _ = await self._run_git(["diff", "HEAD"], cwd=worktree_path)

        # Capture file names
        _, name_list, _ = await self._run_git(
            ["diff", "--name-only", "HEAD"],
            cwd=worktree_path,
        )
        files_changed = [f.strip() for f in name_list.splitlines() if f.strip()]

        return DiffResult(
            task_id=task_id,
            diff=diff_text,
            files_changed=files_changed,
        )

    async def remove_worktree(
        self,
        task_id: str,
        delete_branch: bool = True,
    ) -> None:
        """Remove a task worktree and optionally delete its branch.

        Args:
            task_id: Unique task identifier.
            delete_branch: Whether to delete the task branch (defaults to True).
        """
        self._validate_task_id(task_id)
        worktree_path = self.get_worktree_path(task_id)
        branch_name = self.get_branch_name(task_id)

        self._log.info("worktree.removing", task_id=task_id, path=str(worktree_path))

        # Attempt clean git worktree remove
        await self._run_git(
            ["worktree", "remove", "--force", str(worktree_path)],
            check=False,
        )

        # Filesystem fallback if git worktree remove left artifacts
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)

        # Prune dead worktree metadata
        await self._run_git(["worktree", "prune"], check=False)

        # Delete task branch
        if delete_branch:
            await self._run_git(["branch", "-D", branch_name], check=False)

    async def list_worktrees(self) -> list[WorktreeContext]:
        """List all currently active OSWALD task worktrees."""
        _, output, _ = await self._run_git(["worktree", "list", "--porcelain"])

        results: list[WorktreeContext] = []
        current_path: Path | None = None
        current_branch: str = ""
        current_head: str = ""

        for line in output.splitlines():
            line = line.strip()
            if not line:
                if current_path and current_branch.startswith("refs/heads/oswald/task-"):
                    branch_clean = current_branch.removeprefix("refs/heads/")
                    task_id = branch_clean.removeprefix("oswald/task-")
                    results.append(
                        WorktreeContext(
                            task_id=task_id,
                            worktree_path=current_path,
                            branch_name=branch_clean,
                            base_ref=current_head,
                        )
                    )
                current_path = None
                current_branch = ""
                current_head = ""
                continue

            if line.startswith("worktree "):
                current_path = Path(line.split(" ", 1)[1])
            elif line.startswith("HEAD "):
                current_head = line.split(" ", 1)[1]
            elif line.startswith("branch "):
                current_branch = line.split(" ", 1)[1]

        # Handle final entry if output didn't end with empty line
        if current_path and current_branch.startswith("refs/heads/oswald/task-"):
            branch_clean = current_branch.removeprefix("refs/heads/")
            task_id = branch_clean.removeprefix("oswald/task-")
            results.append(
                WorktreeContext(
                    task_id=task_id,
                    worktree_path=current_path,
                    branch_name=branch_clean,
                    base_ref=current_head,
                )
            )

        return results

    @asynccontextmanager
    async def session(
        self,
        task_id: str,
        base_ref: str = "HEAD",
    ) -> AsyncIterator[WorktreeContext]:
        """Async context manager handling creation and safe teardown of a worktree."""
        context = await self.create_worktree(task_id, base_ref=base_ref)
        try:
            yield context
        finally:
            await self.remove_worktree(task_id)
