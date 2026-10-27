"""Pipeline routing and resource ownership using real Git worktrees and stored reports."""

import asyncio

# Tiny fixture file operations are deliberately synchronous.
# ruff: noqa: ASYNC240
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from oswald.cli.__main__ import SavedPlanPlanner
from oswald.core.agents.coder import CoderInput
from oswald.core.agents.reviewer import ReviewerInput
from oswald.core.agents.tester import TesterInput
from oswald.core.artifacts.models import (
    CodeChanges,
    PlanSpec,
    ReviewVerdict,
    ReviewVerdictStatus,
    TestResults,
)
from oswald.core.artifacts.storage import FileSystemArtifactStore
from oswald.core.orchestrator import Agents, Orchestrator, PlannerInput
from oswald.core.sandbox.fake import FakeSandboxRuntime
from oswald.core.sandbox.models import SandboxConfig, SandboxStatus
from oswald.core.sandbox.protocol import SandboxRuntime
from oswald.core.vcs.workspace import WorkspaceDiff


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("value = 1\n")
    git = shutil.which("git") or "git"
    for args in (
        ["init", "-b", "main"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.com"],
        ["add", "."],
        ["commit", "-m", "fixture"],
    ):
        subprocess.run([git, *args], cwd=root, check=True, capture_output=True)  # noqa: S603
    return root


class Harness:
    def __init__(self, repo: Path, output: Path) -> None:
        self.output = output
        self.events: list[str] = []
        self.inputs: list[object] = []
        self.questions = False
        self.escalated = False
        self.fail_tests = 0
        self.review_status: ReviewVerdictStatus = "APPROVED"
        self.execution_error: str | None = None
        self.crash = ""
        self.foreign = ""
        self.runtime: FakeSandboxRuntime | None = None
        self.store = FileSystemArtifactStore(output)
        self.pipeline = Orchestrator(repo, output, self.store, self.factory, self.sandbox)

    def sandbox(self, config: SandboxConfig) -> FakeSandboxRuntime:
        self.runtime = FakeSandboxRuntime(config)
        return self.runtime

    def factory(self, root: Path, sandbox: object, diff: WorkspaceDiff) -> Agents:
        async def plan(data: PlannerInput) -> PlanSpec:
            self.events.append("planner")
            self.inputs.append(data)
            if self.crash == "planner":
                raise RuntimeError("planner unavailable")
            assert data.workspace_root == root
            assert (root / "app.py").read_text() == "value = 1\n"
            return PlanSpec(
                task_id=data.task_id,
                summary="Change value",
                target_files=["app.py"],
                has_open_questions=self.questions,
                open_questions=["Which value?"] if self.questions else [],
                content="Set value to 2",
            )

        async def code(data: CoderInput) -> CodeChanges:
            self.events.append("coder")
            self.inputs.append(data)
            if self.crash == "coder":
                raise RuntimeError("coder unavailable")
            (root / "app.py").write_text("value = 2\n")
            if self.escalated:
                return CodeChanges(
                    task_id=data.task_id,
                    status="ESCALATED",
                    escalation="Need help",
                    summary="Escalated",
                    content="Need help",
                )
            return CodeChanges(
                task_id="foreign" if self.foreign == "coder" else data.task_id,
                summary="Done",
                modified_files=["app.py"],
                diff=await diff.diff_since_base(data.task_id),
                content="Done",
            )

        async def test(data: TesterInput) -> TestResults:
            self.events.append("tester")
            self.inputs.append(data)
            if self.crash == "tester":
                raise RuntimeError("test runtime unavailable")
            (root / "test_app.py").write_text("assert True\n")
            failed = data.attempt <= self.fail_tests
            return TestResults(
                task_id=data.task_id,
                passed=not failed,
                exit_code=int(failed),
                tests_run=1,
                tests_failed=int(failed),
                content="test report",
                output="assert value == 2",
                execution_error=self.execution_error,
            )

        async def review(data: ReviewerInput) -> ReviewVerdict:
            self.events.append("reviewer")
            self.inputs.append(data)
            assert "test_app.py" in data.changes.diff
            if self.crash == "reviewer":
                raise RuntimeError("review unavailable")
            verdict = self.review_status
            if verdict == "CHANGES_REQUESTED" and data.attempt == 2:
                verdict = "APPROVED"
            if not data.test_results.passed:
                verdict = "CHANGES_REQUESTED"
            return ReviewVerdict(
                task_id=data.task_id,
                verdict=verdict,
                summary="Review report",
                blocking_issues=[] if verdict == "APPROVED" else ["Fix behavior"],
                content="Review details",
            )

        return Agents(
            AsyncMock(run=plan), AsyncMock(run=code), AsyncMock(run=test), AsyncMock(run=review)
        )


async def test_success_persists_cumulative_patch_and_lineage(repo: Path, tmp_path: Path) -> None:
    h = Harness(repo, tmp_path / "reports")
    # Dirty checkout must not contaminate the snapshot Planner sees.
    (repo / "app.py").write_text("uncommitted caller work\n")
    result = await h.pipeline.run("Change value")
    assert result.status == "SUCCEEDED"
    assert h.events == ["planner", "coder", "tester", "reviewer"]
    for agent_input in h.inputs[1:]:
        assert isinstance(agent_input, (CoderInput, TesterInput, ReviewerInput))
        assert agent_input.baseline_context == "Change value"
    patch_text = Path(result.patch_path).read_text()
    assert "+value = 2" in patch_text and "test_app.py" in patch_text
    assert (repo / "app.py").read_text() == "uncommitted caller work\n"
    assert not Path(result.workspace_root).exists()
    assert h.runtime and h.runtime.status == SandboxStatus.STOPPED
    assert len(result.artifacts) == 5
    for ref in result.artifacts:
        assert await h.store.exists(result.task_id, ref.name)
    manifest = json.loads((h.output / result.task_id / "run.json").read_text())
    assert manifest["status"] == "SUCCEEDED" and manifest["base_commit"]
    assert len(result.metrics.agents) == 4
    assert [entry.agent for entry in result.metrics.agents] == [
        "planner",
        "coder",
        "tester",
        "reviewer",
    ]
    assert all(entry.outcome == "completed" for entry in result.metrics.agents)
    assert result.metrics.elapsed_seconds > 0
    assert manifest["metrics"] == result.metrics.model_dump()


@pytest.mark.parametrize("source", ["tester", "reviewer"])
async def test_one_correction_gets_findings_and_reruns_both(
    repo: Path, tmp_path: Path, source: str
) -> None:
    h = Harness(repo, tmp_path / "reports")
    h.fail_tests = int(source == "tester")
    h.review_status = "CHANGES_REQUESTED" if source == "reviewer" else "APPROVED"
    result = await h.pipeline.run("Fix")
    assert result.status == "SUCCEEDED" and result.attempts == 2
    assert h.events == ["planner", "coder", "tester", "reviewer", "coder", "tester", "reviewer"]
    coder_input = h.inputs[4]
    assert isinstance(coder_input, CoderInput)
    assert coder_input.attempt == 2
    assert source in {finding.source for finding in coder_input.findings}
    assert [(entry.agent, entry.attempt) for entry in result.metrics.agents] == [
        ("planner", 1),
        ("coder", 1),
        ("tester", 1),
        ("reviewer", 1),
        ("coder", 2),
        ("tester", 2),
        ("reviewer", 2),
    ]
    # The content-addressed store deduplicates identical test reports.
    assert len(await h.store.history(result.task_id, "test_results")) == (
        2 if source == "tester" else 1
    )


async def test_correction_limit(repo: Path, tmp_path: Path) -> None:
    h = Harness(repo, tmp_path / "reports")
    h.fail_tests = 10
    result = await h.pipeline.run("Fix")
    assert result.status == "STOPPED" and result.attempts == 2 and result.stage == "tester"
    assert "Correction limit" in result.reason
    assert h.events.count("coder") == 2


@pytest.mark.parametrize(
    "mode,stage,events",
    [
        ("questions", "planner", ["planner"]),
        ("escalation", "coder", ["planner", "coder"]),
        ("rejection", "reviewer", ["planner", "coder", "tester", "reviewer"]),
        ("runtime", "tester", ["planner", "coder", "tester"]),
        ("foreign", "coder", ["planner", "coder"]),
    ],
)
async def test_explicit_stops(
    repo: Path, tmp_path: Path, mode: str, stage: str, events: list[str]
) -> None:
    h = Harness(repo, tmp_path / "reports")
    h.questions = mode == "questions"
    h.escalated = mode == "escalation"
    h.review_status = "REJECTED" if mode == "rejection" else "APPROVED"
    h.execution_error = "Docker unavailable" if mode == "runtime" else None
    h.foreign = "coder" if mode == "foreign" else ""
    result = await h.pipeline.run("Fix")
    assert result.status in {"STOPPED", "FAILED"}
    assert result.stage == stage and result.reason
    assert h.events == events
    assert not Path(result.workspace_root).exists()
    assert h.runtime and h.runtime.status == SandboxStatus.STOPPED


@pytest.mark.parametrize("stage", ["planner", "coder", "tester", "reviewer"])
async def test_stage_exception(repo: Path, tmp_path: Path, stage: str) -> None:
    h = Harness(repo, tmp_path / "reports")
    h.crash = stage
    result = await h.pipeline.run("Fix")
    assert result.status == "FAILED" and result.stage == stage
    assert "unavailable" in result.reason
    assert result.metrics.agents[-1].agent == stage
    assert result.metrics.agents[-1].outcome == "failed"
    assert h.runtime and h.runtime.status == SandboxStatus.STOPPED
    assert not Path(result.workspace_root).exists()


async def test_partial_startup_still_stops(repo: Path, tmp_path: Path) -> None:
    h = Harness(repo, tmp_path / "reports")
    with (
        patch.object(FakeSandboxRuntime, "start", side_effect=RuntimeError("start failed")),
        patch.object(
            FakeSandboxRuntime,
            "stop",
            new_callable=AsyncMock,
        ) as stop,
    ):
        result = await h.pipeline.run("Fix")
    assert result.stage == "sandbox" and result.status == "FAILED"
    stop.assert_awaited_once()


async def test_failed_patch_preserves_workspace(repo: Path, tmp_path: Path) -> None:
    h = Harness(repo, tmp_path / "reports")
    original = Path.write_text

    def fail_patch(path: Path, *args: Any, **kwargs: Any) -> int:
        if path.name == "final.patch":
            raise OSError("disk full")
        return original(path, *args, **kwargs)

    with patch.object(Path, "write_text", fail_patch):
        result = await h.pipeline.run("Fix")
    assert result.status == "FAILED" and "disk full" in result.reason
    assert Path(result.workspace_root).exists()


async def test_timeout_and_cancellation(repo: Path, tmp_path: Path) -> None:
    h = Harness(repo, tmp_path / "reports")
    entered = asyncio.Event()

    async def hang(_: PlannerInput) -> PlanSpec:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    original = h.pipeline.agents

    def factory(root: Path, runtime: SandboxRuntime, diff: WorkspaceDiff) -> Agents:
        agents = original(root, runtime, diff)
        return Agents(AsyncMock(run=hang), agents.coder, agents.tester, agents.reviewer)

    h.pipeline.agents = factory
    h.pipeline.stage_timeout = 0.1
    result = await h.pipeline.run("Timeout")
    assert result.status == "FAILED" and result.stage == "planner"
    assert "TimeoutError" in result.reason
    h.pipeline.stage_timeout = 10
    entered.clear()
    task = asyncio.create_task(h.pipeline.run("Cancel"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert h.runtime and h.runtime.status == SandboxStatus.STOPPED
    manifests = [json.loads(p.read_text()) for p in h.output.glob("*/run.json")]
    assert any(m["status"] == "CANCELLED" for m in manifests)


async def test_saved_plan_requires_snapshot_match(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    path.write_text(
        json.dumps(
            {
                "base_commit": "abc",
                "spec": {
                    "task_id": "template",
                    "summary": "Plan",
                    "content": "Plan",
                },
            }
        )
    )
    data = PlannerInput(task_id="new", request="Fix", workspace_root=tmp_path, base_commit="abc")
    assert (await SavedPlanPlanner(path).run(data)).task_id == "new"
    with pytest.raises(ValueError, match="snapshot"):
        await SavedPlanPlanner(path).run(data.model_copy(update={"base_commit": "def"}))


async def test_report_store_failure_is_reported_and_cleans_up(repo: Path, tmp_path: Path) -> None:
    h = Harness(repo, tmp_path / "reports")
    with patch.object(h.store, "save", side_effect=OSError("storage unavailable")):
        result = await h.pipeline.run("Fix")
    assert result.status == "FAILED" and result.stage == "planner"
    assert "storage unavailable" in result.reason
    assert h.runtime and h.runtime.status == SandboxStatus.STOPPED
    assert not Path(result.workspace_root).exists()


async def test_teardown_failure_is_visible_and_preserves_workspace(
    repo: Path, tmp_path: Path
) -> None:
    h = Harness(repo, tmp_path / "reports")
    with patch.object(FakeSandboxRuntime, "stop", side_effect=RuntimeError("stop failed")):
        result = await h.pipeline.run("Fix")
    assert result.status == "FAILED" and "stop failed" in result.reason
    assert Path(result.workspace_root).exists() and Path(result.patch_path).exists()
    assert result.cleanup_errors


async def test_invalid_repository_reports_workspace_failure(tmp_path: Path) -> None:
    h = Harness(tmp_path, tmp_path / "reports")
    result = await h.pipeline.run("Fix")
    assert result.status == "FAILED" and result.stage == "workspace"
    assert "not a valid Git repository" in result.reason


async def test_revalidation_rejects_constructed_invalid_artifact(
    repo: Path, tmp_path: Path
) -> None:
    h = Harness(repo, tmp_path / "reports")
    original = h.pipeline.agents

    async def invalid(data: PlannerInput) -> PlanSpec:
        return PlanSpec.model_construct(task_id=data.task_id, summary="", content="")

    def factory(root: Path, runtime: SandboxRuntime, diff: WorkspaceDiff) -> Agents:
        agents = original(root, runtime, diff)
        return Agents(AsyncMock(run=invalid), agents.coder, agents.tester, agents.reviewer)

    h.pipeline.agents = factory
    result = await h.pipeline.run("Fix")
    assert result.status == "FAILED" and result.stage == "planner"
    assert "ValidationError" in result.reason
    assert h.events == []


async def test_tester_runtime_failure_stops_tool_loop(tmp_path: Path) -> None:
    from oswald.core.agents.tester import TesterAgent
    from oswald.core.llm.adapters.fake import FakeLLMClient
    from oswald.core.llm.types import LLMResponse, StopReason, ToolCall
    from oswald.core.sandbox.exceptions import SandboxError

    spec = PlanSpec(task_id="test", summary="Plan", target_files=["app.py"], content="Plan")
    changes = CodeChanges(
        task_id="test", summary="Done", modified_files=["app.py"], diff="patch", content="Done"
    )
    llm = FakeLLMClient()
    llm.enqueue(
        LLMResponse(
            stop_reason=StopReason.TOOL_USE,
            tool_calls=[
                ToolCall(
                    id="test",
                    name="run_test_command",
                    arguments={"command": ["python", "test_app.py"]},
                ),
            ],
        )
    )
    runtime = FakeSandboxRuntime(SandboxConfig(task_id="test", workspace_path=tmp_path))
    with patch.object(runtime, "exec_command", side_effect=SandboxError("daemon failed")):
        result = await TesterAgent(llm, runtime, tmp_path).run(
            TesterInput(task_id="test", spec=spec, changes=changes),
        )
    assert result.execution_error and "daemon failed" in result.execution_error
    assert not result.passed and llm.call_count == 1


@pytest.mark.parametrize("outcome", ["questions", "malformed", "provider_error"])
async def test_real_planner_stops_before_coder(repo: Path, tmp_path: Path, outcome: str) -> None:
    from oswald.core.agents.planner import PlannerAgent
    from oswald.core.artifacts.models import PlanSpec
    from oswald.core.llm.adapters.fake import FakeLLMClient
    from oswald.core.llm.exceptions import LLMError
    from oswald.core.orchestrator import PlannerAdapter

    h = Harness(repo, tmp_path / "reports")
    llm = FakeLLMClient()
    if outcome == "questions":
        llm.enqueue_text(
            json.dumps(
                {
                    "summary": "Clarification required",
                    "content": "Which value?",
                    "has_open_questions": True,
                    "open_questions": ["Which value?"],
                }
            )
        )
    elif outcome == "malformed":
        for _ in range(3):
            llm.enqueue_text("Not a specification")
    else:
        llm.enqueue(LLMError("provider unavailable"))
    original = h.pipeline.agents

    def factory(root: Path, runtime: SandboxRuntime, diff: WorkspaceDiff) -> Agents:
        agents = original(root, runtime, diff)
        return Agents(
            PlannerAdapter(PlannerAgent(llm, root, model="fake/planner")),
            agents.coder,
            agents.tester,
            agents.reviewer,
        )

    h.pipeline.agents = factory
    result = await h.pipeline.run("Fix value")
    assert result.stage == "planner" and result.attempts == 0
    assert result.status == ("FAILED" if outcome == "provider_error" else "STOPPED")
    assert not h.events  # No Coder/Tester/Reviewer execution.
    assert h.runtime and h.runtime.status == SandboxStatus.STOPPED
    assert not Path(result.workspace_root).exists()
    if outcome != "provider_error":
        spec = await h.store.load_latest(result.task_id, "spec", PlanSpec)
        assert spec.has_open_questions and spec.open_questions


async def test_planner_adapter_rejects_wrong_snapshot(tmp_path: Path) -> None:
    from oswald.core.agents.planner import PlannerAgent
    from oswald.core.llm.adapters.fake import FakeLLMClient
    from oswald.core.orchestrator import PlannerAdapter

    llm = FakeLLMClient()
    planner = PlannerAdapter(PlannerAgent(llm, tmp_path, model="fake/planner"))
    data = PlannerInput(
        task_id="task", request="Fix", workspace_root=tmp_path / "other", base_commit="abc"
    )
    with pytest.raises(ValueError, match="isolated task worktree"):
        await planner.run(data)
    assert llm.call_count == 0


@pytest.mark.parametrize(
    "stage,expected",
    [
        ("coder", ["coder", "tester", "reviewer"]),
        ("tester", ["tester", "reviewer"]),
        ("reviewer", ["reviewer"]),
    ],
)
async def test_continue_retries_only_unfinished_stage(
    repo: Path, tmp_path: Path, stage: str, expected: list[str]
) -> None:
    h = Harness(repo, tmp_path / "reports")
    h.crash = stage
    result = await h.pipeline.run("Fix")
    assert result.status == "FAILED"
    h.crash = ""
    h.events.clear()
    # A new instance models a separate CLI invocation; source HEAD may have moved.
    h.pipeline = Orchestrator(repo, h.output, h.store, h.factory, h.sandbox)
    (repo / "app.py").write_text("caller edits\n")
    continued = await h.pipeline.continue_task(result.task_id)
    assert continued.status == "SUCCEEDED"
    assert continued.task_id == result.task_id
    assert continued.base_commit == result.base_commit
    assert h.events == expected
    assert "+value = 2" in Path(continued.patch_path).read_text()
    assert "test_app.py" in Path(continued.patch_path).read_text()
    assert (repo / "app.py").read_text() == "caller edits\n"
    assert not Path(continued.workspace_root).exists()
    assert list((h.output / result.task_id).glob("run-*.json"))


async def test_continue_after_limit_passes_saved_findings(repo: Path, tmp_path: Path) -> None:
    h = Harness(repo, tmp_path / "reports")
    h.fail_tests = 10
    result = await h.pipeline.run("Fix")
    assert result.status == "STOPPED"
    h.fail_tests = 0
    h.events.clear()
    # First continuation fails at Coder, and the next must keep its findings.
    h.crash = "coder"
    failed = await h.pipeline.continue_task(result.task_id)
    assert failed.status == "FAILED" and failed.attempts == 3
    h.crash = ""
    h.events.clear()
    continued = await h.pipeline.continue_task(result.task_id)
    assert continued.status == "SUCCEEDED" and continued.attempts == 3
    assert h.events == ["coder", "tester", "reviewer"]
    coder_input = next(x for x in reversed(h.inputs) if isinstance(x, CoderInput))
    assert {f.source for f in coder_input.findings} == {"tester", "reviewer"}


async def test_continue_execution_error_retries_tester(repo: Path, tmp_path: Path) -> None:
    h = Harness(repo, tmp_path / "reports")
    h.execution_error = "Docker unavailable"
    result = await h.pipeline.run("Fix")
    h.execution_error = None
    h.events.clear()
    continued = await h.pipeline.continue_task(result.task_id)
    assert continued.status == "SUCCEEDED"
    assert h.events == ["tester", "reviewer"]


async def test_continue_succeeded_is_noop(repo: Path, tmp_path: Path) -> None:
    h = Harness(repo, tmp_path / "reports")
    result = await h.pipeline.run("Fix")
    h.events.clear()
    assert await h.pipeline.continue_task(result.task_id) == result
    assert not h.events


async def test_continue_invalid_or_missing_id(repo: Path, tmp_path: Path) -> None:
    h = Harness(repo, tmp_path / "reports")
    with pytest.raises(ValueError):
        await h.pipeline.continue_task("../outside")
    with pytest.raises(FileNotFoundError):
        await h.pipeline.continue_task("0" * 32)


async def test_continue_restore_failure_preserves_patch(repo: Path, tmp_path: Path) -> None:
    h = Harness(repo, tmp_path / "reports")
    h.crash = "tester"
    result = await h.pipeline.run("Fix")
    patch_path = Path(result.patch_path)
    patch_path.write_text("invalid patch\n")
    h.events.clear()
    continued = await h.pipeline.continue_task(result.task_id)
    assert continued.status == "FAILED" and continued.cleanup_errors
    assert patch_path.read_text() == "invalid patch\n"
    assert not h.events
    assert Path(continued.workspace_root).exists()
    with pytest.raises(ValueError, match="cleanup"):
        await h.pipeline.continue_task(result.task_id)


async def test_continue_sandbox_failure_keeps_resume_stage(repo: Path, tmp_path: Path) -> None:
    h = Harness(repo, tmp_path / "reports")
    h.crash = "reviewer"
    result = await h.pipeline.run("Fix")
    h.crash = ""
    with patch.object(FakeSandboxRuntime, "start", side_effect=RuntimeError("Docker unavailable")):
        failed = await h.pipeline.continue_task(result.task_id)
    assert failed.stage == "sandbox" and failed.resume_stage == "reviewer"
    h.events.clear()
    continued = await h.pipeline.continue_task(result.task_id)
    assert continued.status == "SUCCEEDED" and h.events == ["reviewer"]


@pytest.mark.parametrize("override_image", [None, "project:prepared"])
async def test_continue_remembers_repo_and_image(
    repo: Path, tmp_path: Path, override_image: str | None
) -> None:
    h = Harness(repo, tmp_path / "reports")
    h.pipeline.image = "project:test"
    h.crash = "tester"
    result = await h.pipeline.run("Fix")
    h.crash = ""
    h.pipeline = Orchestrator(tmp_path / "wrong-repo", h.output, h.store, h.factory, h.sandbox)
    continued = await h.pipeline.continue_task(result.task_id, image=override_image)
    assert continued.status == "SUCCEEDED"
    assert h.runtime and h.runtime.config.image == (override_image or "project:test")
    assert continued.image == (override_image or "project:test")


async def test_continue_legacy_report_and_pinned_commit(repo: Path, tmp_path: Path) -> None:
    h = Harness(repo, tmp_path / "reports")
    h.crash = "tester"
    result = await h.pipeline.run("Fix")
    manifest = h.output / result.task_id / "run.json"
    legacy = json.loads(manifest.read_text())
    for field in ("repo_path", "image", "resume_stage", "findings"):
        legacy.pop(field)
    manifest.write_text(json.dumps(legacy))
    (repo / "app.py").write_text("value = 999\n")
    git = shutil.which("git") or "git"
    await asyncio.to_thread(
        subprocess.run, [git, "add", "."], cwd=repo, check=True, capture_output=True
    )
    await asyncio.to_thread(
        subprocess.run,
        [git, "commit", "-m", "Moved HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    h.crash = ""
    h.events.clear()
    continued = await h.pipeline.continue_task(result.task_id)
    assert continued.status == "SUCCEEDED" and continued.base_commit == result.base_commit
    assert h.events == ["tester", "reviewer"]
    assert (repo / "app.py").read_text() == "value = 999\n"
    assert "-value = 1" in Path(continued.patch_path).read_text()
