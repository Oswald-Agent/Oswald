"""Bounded async pipeline; agents decide content, this module owns lifecycle and routing."""

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID, uuid4

import structlog
from pydantic import BaseModel, ConfigDict, Field

from oswald.core.agents.coder import CoderInput
from oswald.core.agents.reviewer import ReviewerInput
from oswald.core.agents.telemetry import RunProgress, RunStats, agent_scope
from oswald.core.agents.tester import TesterInput
from oswald.core.artifacts.models import (
    ArtifactRef,
    BaseArtifact,
    CodeChanges,
    Finding,
    PlanSpec,
    Provenance,
    ReviewVerdict,
    TestResults,
)
from oswald.core.artifacts.storage import ArtifactStore
from oswald.core.sandbox.models import SandboxConfig
from oswald.core.sandbox.protocol import SandboxRuntime
from oswald.core.vcs.workspace import WorkspaceDiff, WorktreeWorkspaceDiff
from oswald.core.vcs.worktree import WorktreeManager


class PlannerInput(BaseModel):
    """Planner adapters inspect workspace_root, never the source checkout."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str
    request: str = Field(min_length=1)
    workspace_root: Path
    base_commit: str


class Agent[Input, Output](Protocol):
    async def run(self, data: Input) -> Output: ...


@dataclass(frozen=True)
class Agents:
    planner: Agent[PlannerInput, PlanSpec]
    coder: Agent[CoderInput, CodeChanges]
    tester: Agent[TesterInput, TestResults]
    reviewer: Agent[ReviewerInput, ReviewVerdict]


class RunResult(BaseArtifact):
    status: Literal["SUCCEEDED", "STOPPED", "FAILED", "CANCELLED"]
    stage: str
    reason: str
    attempts: int
    base_commit: str = ""
    workspace_root: str = ""
    patch_path: str = ""
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    cleanup_errors: list[str] = Field(default_factory=list)
    metrics: RunStats = Field(default_factory=RunStats)
    repo_path: str = ""
    image: str = ""
    resume_stage: str = ""
    findings: list[Finding] = Field(default_factory=list)


AgentFactory = Callable[[Path, SandboxRuntime, WorkspaceDiff], Agents]


class Orchestrator:
    """Run one task with at most one correction, persisting before deleting resources.

    A private worktree parent avoids collisions with other WorktreeManager users.
    No commits, pushes, or merges are performed. Reports live outside the worktree.
    """

    def __init__(
        self,
        repo_path: Path,
        output_dir: Path,
        store: ArtifactStore,
        agents: AgentFactory,
        sandbox: Callable[[SandboxConfig], SandboxRuntime],
        *,
        image: str = "python:3.12-slim",
        stage_timeout: float = 1800,
        progress_interval: float = 10,
    ) -> None:
        if stage_timeout <= 0:
            raise ValueError("stage_timeout must be positive")
        self.repo_path = repo_path.resolve()
        self.output_dir = output_dir.resolve()
        self.store = store
        self.agents = agents
        self.sandbox = sandbox
        self.image = image
        self.stage_timeout = stage_timeout
        # Validate before allocating a workspace or starting a run.
        RunProgress("", interval=progress_interval)
        self.progress_interval = progress_interval

    async def continue_task(self, task_id: str, *, image: str | None = None) -> RunResult:
        """Restore a finished invocation and retry its unfinished stage, without replanning."""
        if UUID(task_id).hex != task_id:
            raise ValueError("Expected the task ID printed by the orchestrator")
        report_dir = self.output_dir / task_id
        previous = RunResult.model_validate_json(
            await asyncio.to_thread((report_dir / "run.json").read_text, encoding="utf-8")
        )
        if previous.task_id != task_id:
            raise ValueError("Saved run belongs to another task")
        if previous.status == "SUCCEEDED":
            return previous
        if previous.cleanup_errors:
            raise ValueError("Recover the retained workspace/cleanup errors before continuing")
        data = PlannerInput.model_validate_json(
            await asyncio.to_thread((report_dir / "request.json").read_text, encoding="utf-8")
        )
        if data.task_id != task_id or data.base_commit != previous.base_commit:
            raise ValueError("Saved request does not match the run")
        # Keep the preceding outcome reviewable even after run.json is updated.
        await asyncio.to_thread(
            shutil.copyfile, report_dir / "run.json", report_dir / f"run-{uuid4().hex}.json"
        )
        if image is not None:
            previous = previous.model_copy(update={"image": image})
        return await self._run(data.request, base_ref=data.base_commit, previous=previous)

    async def run(self, request: str, *, base_ref: str = "HEAD") -> RunResult:
        return await self._run(request, base_ref=base_ref)

    async def _run(
        self, request: str, *, base_ref: str, previous: RunResult | None = None
    ) -> RunResult:
        if not request.strip():
            raise ValueError("Task request must not be blank")
        task_id = previous.task_id if previous else uuid4().hex
        progress = RunProgress(task_id, interval=self.progress_interval)
        logger = structlog.get_logger()
        report_dir = self.output_dir / task_id
        await asyncio.to_thread(report_dir.mkdir, parents=True, exist_ok=previous is not None)
        manager: WorktreeManager | None = None
        runtime: SandboxRuntime | None = None
        root: Path | None = None
        base_commit = previous.base_commit if previous else ""
        stage = "workspace"
        attempt = previous.attempts if previous else 0
        refs: list[ArtifactRef] = list(previous.artifacts) if previous else []
        resume_stage = (previous.resume_stage or previous.stage) if previous else "planner"
        findings = list(previous.findings) if previous else []
        workspace_restored = previous is None or not previous.patch_path
        start_attempt = max(previous.attempts, 1) if previous else 1
        repo_path = Path(previous.repo_path) if previous and previous.repo_path else self.repo_path
        image = previous.image if previous and previous.image else self.image
        status: Literal["SUCCEEDED", "STOPPED", "FAILED", "CANCELLED"] = "STOPPED"
        reason = ""
        patch_path = previous.patch_path if previous else ""
        cleanup_errors: list[str] = []
        latest_tests: TestResults | None = None
        latest_review: ReviewVerdict | None = None

        async def save[T: BaseArtifact](name: str, artifact: T, model: type[T]) -> T:
            # Revalidate even model_construct()/model_copy() outputs at every handoff.
            validated = model.model_validate_json(artifact.model_dump_json())
            if validated.task_id != task_id:
                raise ValueError(f"{name} belongs to another task")
            ref = await self.store.save(
                task_id,
                name,
                validated,
                produced_by=Provenance(agent=stage, attempt=max(attempt, 1)),
                inputs=tuple(refs[-1:]),
            )
            refs.append(ref)
            attempt_dir = report_dir / f"attempt-{attempt}"
            await asyncio.to_thread(attempt_dir.mkdir, exist_ok=True)
            await asyncio.to_thread(
                (attempt_dir / f"{name}.json").write_text,
                validated.model_dump_json(indent=2),
                encoding="utf-8",
            )
            return validated

        async def saved[T: BaseArtifact](name: str, model: type[T]) -> T:
            ref = next(ref for ref in reversed(refs) if ref.name == name)
            artifact = await self.store.load(ref, model)
            if artifact.task_id != task_id:
                raise ValueError(f"{name} belongs to another task")
            return artifact

        def corrections(tests: TestResults, review: ReviewVerdict) -> list[Finding]:
            findings = []
            if not (tests.passed and tests.exit_code == 0 and tests.tests_failed == 0):
                findings.append(
                    Finding(
                        source="tester",
                        severity="major",
                        problem=tests.content,
                        evidence=tests.output[-500:],
                        expected_correction="Correct the implementation so the tests pass.",
                    )
                )
            if review.verdict != "APPROVED" or review.blocking_issues:
                findings.extend(
                    Finding(
                        source="reviewer",
                        severity="major",
                        problem=issue,
                        expected_correction=review.content,
                    )
                    for issue in (review.blocking_issues or [review.summary])
                )
            return findings

        async def call[T](operation: Awaitable[T]) -> T:
            async with asyncio.timeout(self.stage_timeout):
                return await operation

        async def invoke[T](operation: Awaitable[T]) -> T:
            async with agent_scope(stage, task_id, max(attempt, 1), run=progress) as agent_progress:
                value = await call(operation)
                agent_progress.result(value)
                return value

        logger.info("run.started", task_id=task_id, image=image)
        try:
            if previous and previous.status == "STOPPED" and resume_stage in {"tester", "reviewer"}:
                findings = corrections(
                    await saved("test_results", TestResults),
                    await saved("review", ReviewVerdict),
                )
                start_attempt += 1
                resume_stage = "coder"
            if previous and resume_stage == "coder" and start_attempt > 1 and not findings:
                findings = corrections(
                    await saved("test_results", TestResults),
                    await saved("review", ReviewVerdict),
                )
            attempt = start_attempt if previous else 0
            manager = WorktreeManager(repo_path, report_dir / "workspace")
            context = await call(manager.create_worktree(task_id, base_ref=base_ref))
            root = context.worktree_path
            base_commit = context.base_ref
            await asyncio.to_thread(
                (report_dir / "request.json").write_text,
                PlannerInput(
                    task_id=task_id, request=request, workspace_root=root, base_commit=base_commit
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )
            if previous and previous.patch_path:
                target = report_dir / "final.patch"
                if await asyncio.to_thread(target.read_bytes):
                    await call(manager.apply_patch(task_id, target))
            workspace_restored = True
            stage = "sandbox"
            runtime = self.sandbox(SandboxConfig(task_id=task_id, workspace_path=root, image=image))
            await call(runtime.start())
            diff = WorktreeWorkspaceDiff(manager)
            stage = "planner"
            agents = self.agents(root, runtime, diff)
            if previous and resume_stage not in {"workspace", "sandbox", "planner"}:
                spec = await saved("spec", PlanSpec)
            else:
                spec = await save(
                    "spec",
                    await invoke(
                        agents.planner.run(
                            PlannerInput(
                                task_id=task_id,
                                request=request,
                                workspace_root=root,
                                base_commit=base_commit,
                            )
                        )
                    ),
                    PlanSpec,
                )
            if spec.has_open_questions or spec.open_questions:
                reason = "Unresolved planning questions: " + "; ".join(spec.open_questions)
            else:
                for attempt in range(start_attempt, start_attempt + 2):
                    stage = "coder"
                    if (
                        previous
                        and attempt == start_attempt
                        and resume_stage in {"tester", "reviewer"}
                    ):
                        changes = await saved("code_changes", CodeChanges)
                    else:
                        changes = await save(
                            "code_changes",
                            await invoke(
                                agents.coder.run(
                                    CoderInput(
                                        task_id=task_id,
                                        attempt=attempt,
                                        spec=spec,
                                        findings=findings,
                                        baseline_context=request,
                                    )
                                )
                            ),
                            CodeChanges,
                        )
                    if changes.status == "ESCALATED":
                        reason = changes.escalation or changes.summary
                        break
                    stage = "tester"
                    if previous and attempt == start_attempt and resume_stage == "reviewer":
                        tests = await saved("test_results", TestResults)
                    else:
                        tests = await save(
                            "test_results",
                            await invoke(
                                agents.tester.run(
                                    TesterInput(
                                        task_id=task_id,
                                        attempt=attempt,
                                        spec=spec,
                                        changes=changes,
                                        baseline_context=request,
                                    )
                                )
                            ),
                            TestResults,
                        )
                    latest_tests = tests
                    if tests.execution_error:
                        status, reason = "FAILED", tests.execution_error
                        break
                    # Tester can author tests: Reviewer must see the current cumulative patch.
                    current_diff = await call(manager.get_diff(task_id))
                    reviewed_changes = changes.model_copy(update={"diff": current_diff.diff})
                    reviewed_changes = await save("review_changes", reviewed_changes, CodeChanges)
                    stage = "reviewer"
                    review = await save(
                        "review",
                        await invoke(
                            agents.reviewer.run(
                                ReviewerInput(
                                    task_id=task_id,
                                    attempt=attempt,
                                    spec=spec,
                                    changes=reviewed_changes,
                                    test_results=tests,
                                    baseline_context=request,
                                )
                            )
                        ),
                        ReviewVerdict,
                    )
                    latest_review = review
                    clean_tests = tests.passed and tests.exit_code == 0 and tests.tests_failed == 0
                    if review.verdict == "REJECTED":
                        reason = review.summary + ": " + "; ".join(review.blocking_issues)
                        break
                    if clean_tests and review.verdict == "APPROVED" and not review.blocking_issues:
                        status, reason = "SUCCEEDED", "Tests passed and Reviewer approved"
                        break
                    if not clean_tests:
                        stage = "tester"
                        reason = "Tests failed: " + tests.content
                    else:
                        reason = review.summary + ": " + "; ".join(review.blocking_issues)
                    if attempt == start_attempt + 1:
                        reason = "Correction limit reached (2 attempts per invocation). " + reason
                        break
                    findings = corrections(tests, review)
        except asyncio.CancelledError:
            status, reason = "CANCELLED", f"Cancelled during {stage}"
            raise
        except TimeoutError:
            status, reason = (
                "FAILED",
                f"TimeoutError: {stage} exceeded stage timeout ({self.stage_timeout:g} seconds)",
            )
        except Exception as exc:
            status, reason = "FAILED", f"{type(exc).__name__}: {exc}"
        finally:
            # Stop even after partial startup; preserve changes if snapshot or teardown fails.
            if runtime is not None:
                try:
                    await call(runtime.stop())
                except Exception as exc:
                    cleanup_errors.append(f"sandbox: {exc}")
            if root is None and manager is not None:
                candidate = manager.get_worktree_path(task_id)
                if await asyncio.to_thread(candidate.is_dir):
                    root = candidate
            if root is not None and not workspace_restored:
                cleanup_errors.append(
                    f"restore failed; saved patch preserved, worktree retained at {root}"
                )
            if root is not None and manager is not None and workspace_restored:
                try:
                    logs = root / ".oswald" / "logs"
                    if await asyncio.to_thread(logs.is_dir):
                        await asyncio.to_thread(
                            shutil.copytree,
                            logs,
                            report_dir / "logs",
                            symlinks=True,
                            dirs_exist_ok=True,
                        )
                    final_diff = await call(manager.get_diff(task_id))
                    target = report_dir / "final.patch"
                    await asyncio.to_thread(target.write_text, final_diff.diff, encoding="utf-8")
                    patch_path = str(target)
                except Exception as exc:
                    cleanup_errors.append(f"patch: {exc}; worktree retained at {root}")
                if patch_path and not cleanup_errors:
                    try:
                        await call(manager.remove_worktree(task_id))
                        if await asyncio.to_thread(root.exists):
                            raise RuntimeError(f"Worktree still exists at {root}")
                    except Exception as exc:
                        cleanup_errors.append(f"worktree: {exc}")
            if cleanup_errors and status != "CANCELLED":
                status = "FAILED"
                reason += "; Cleanup failed: " + "; ".join(cleanup_errors)
            result = RunResult(
                task_id=task_id,
                status=status,
                stage=stage,
                reason=reason,
                attempts=attempt,
                base_commit=base_commit,
                workspace_root=str(root or ""),
                patch_path=patch_path,
                artifacts=refs,
                cleanup_errors=cleanup_errors,
                metrics=progress.snapshot(),
                repo_path=str(repo_path),
                image=image,
                resume_stage=resume_stage if stage in {"workspace", "sandbox"} else stage,
                findings=findings,
            )
            await asyncio.to_thread(
                (report_dir / "run.json").write_text,
                result.model_dump_json(indent=2),
                encoding="utf-8",
            )
            totals = result.metrics.model_dump(exclude={"agents"})
            verification: dict[str, str | int] = {}
            if latest_tests is not None:
                verification.update(
                    tests_run=latest_tests.tests_run, tests_failed=latest_tests.tests_failed
                )
            if latest_review is not None:
                verification["verdict"] = latest_review.verdict
            if status != "SUCCEEDED":
                verification["reason"] = reason[:200]
            logger.log(
                20 if status == "SUCCEEDED" else 40 if status == "FAILED" else 30,
                "run.completed",
                task_id=task_id,
                status=status,
                stage=stage,
                attempts=attempt,
                patch_path=patch_path,
                report_path=str(report_dir / "run.json"),
                cleanup_errors=len(cleanup_errors),
                **totals,
                **verification,
            )
        return result
