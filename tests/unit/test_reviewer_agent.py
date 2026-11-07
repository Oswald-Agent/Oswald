"""Unit tests for read-only review of pipeline artifacts."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from oswald.core.agents import (
    BudgetExceededError,
    CoderAgent,
    CoderInput,
    InMemoryEventSink,
    ReviewerAgent,
    ReviewerInput,
    TesterAgent,
    TesterInput,
)
from oswald.core.artifacts import (
    CodeChanges,
    FileSystemArtifactStore,
    PlanSpec,
    Provenance,
    ReviewVerdict,
    ReviewVerdictStatus,
    TaskItem,
    TestResults,
)
from oswald.core.llm.adapters.fake import FakeLLMClient
from oswald.core.llm.exceptions import LLMProviderError
from oswald.core.llm.types import LLMResponse, Role, StopReason, ToolCall
from oswald.core.sandbox.fake import FakeSandboxRuntime
from oswald.core.sandbox.models import SandboxConfig
from oswald.core.vcs.workspace import FakeWorkspaceDiff


@pytest.fixture
def review_inputs() -> tuple[PlanSpec, CodeChanges, TestResults]:
    """Provide a consistent plan, implementation, and passing test report."""
    return (
        PlanSpec(
            task_id="task-001",
            summary="Add a health check",
            target_files=["src/health.py"],
            tasks=[
                TaskItem(
                    step=1, description="Add health", target_file="src/health.py", action="create"
                )
            ],
            acceptance_criteria=["Health check returns ok"],
            content="# Specification\nReturn ok from health().",
        ),
        CodeChanges(
            task_id="task-001",
            summary="Added health()",
            modified_files=["src/health.py"],
            diff="--- /dev/null\n+++ b/src/health.py\n@@ -0,0 +1,2 @@\n+def health():\n+    return 'ok'",
            content="# Changes\nAdded the health check.",
            deviations=["Used the existing health module"],
            known_limitations=["Health check does not probe backing services"],
            test_attention=["Verify health check return value"],
        ),
        TestResults(
            task_id="task-001",
            passed=True,
            tests_run=1,
            test_files=["tests/test_health.py"],
            output="1 passed",
            content="# Test Results\nHealth check returns ok.",
        ),
    )


def _input(spec: PlanSpec, changes: CodeChanges, results: TestResults) -> ReviewerInput:
    return ReviewerInput(task_id=spec.task_id, spec=spec, changes=changes, test_results=results)


def _response(
    verdict: ReviewVerdictStatus = "APPROVED", *, blocking_issues: list[str] | None = None
) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "summary": "Reviewed the health check",
            "blocking_issues": blocking_issues if blocking_issues is not None else [],
            "content": "# Review\nThe health check satisfies the specification.",
        }
    )


@pytest.mark.parametrize("verdict", ["APPROVED", "CHANGES_REQUESTED", "REJECTED"])
async def test_reviewer_returns_verdict(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults], verdict: ReviewVerdictStatus
) -> None:
    """Verify all routing statuses preserve model-authored review content."""
    client = FakeLLMClient()
    issues = [] if verdict == "APPROVED" else ["Health check needs error-path coverage"]
    client.enqueue_text(_response(verdict, blocking_issues=issues))
    result = await ReviewerAgent(client, model="fake/reviewer").run(_input(*review_inputs))

    assert result.task_id == "task-001"
    assert result.verdict == verdict
    assert result.summary == "Reviewed the health check"
    assert result.blocking_issues == issues
    assert result.content == "# Review\nThe health check satisfies the specification."
    assert result.created_at


@pytest.mark.parametrize("original_request", ["", "Keep npm installation offline."])
async def test_reviewer_request_preserves_artifacts_and_original_request(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults],
    original_request: str,
) -> None:
    """Verify prompt loading, model selection, read-only tools, and artifact fidelity."""
    client = FakeLLMClient()
    client.enqueue_text(_response())
    data = _input(*review_inputs).model_copy(update={"baseline_context": original_request})
    await ReviewerAgent(client, model="custom/reviewer").run(data)

    request = client.requests[0]
    assert request.model == "custom/reviewer"
    assert request.tools == []
    assert request.temperature == 0.0
    assert len(request.messages) == 2
    assert request.messages[0].role == Role.SYSTEM
    assert "# Reviewer Agent" in request.messages[0].content
    assert "untrusted evidence" in request.messages[0].content
    assert "human technical sign-off" in request.messages[0].content
    assert request.messages[1].role == Role.USER
    payload = json.loads(request.messages[1].content)
    if original_request:
        assert payload.pop("original_request") == original_request
    assert payload == dict(
        zip(
            ("spec", "changes", "test_results"),
            (artifact.model_dump(mode="json") for artifact in review_inputs),
            strict=True,
        )
    )


@pytest.mark.parametrize("index", [1, 2])
async def test_reviewer_rejects_mismatched_tasks(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults], index: int
) -> None:
    """Verify mixed-task artifacts fail before any LLM request."""
    spec, changes, results = review_inputs
    if index == 1:
        changes = changes.model_copy(update={"task_id": "other-task"})
    else:
        results = results.model_copy(update={"task_id": "other-task"})
    client = FakeLLMClient()
    with pytest.raises(ValueError, match="same task"):
        await ReviewerAgent(client, model="fake/reviewer").run(_input(spec, changes, results))
    assert client.call_count == 0


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Not JSON",
        "[]",
        "{}",
        _response().replace("APPROVED", "LGTM"),
        _response().replace('"blocking_issues": []', '"blocking_issues": "none"'),
        _response()[:-1] + ', "task_id": "other-task"}',
        _response()[:-1] + ', "created_at": "yesterday"}',
    ],
)
async def test_reviewer_rejects_invalid_output(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults], text: str
) -> None:
    """Verify malformed, unknown, or model-controlled metadata fields fail validation."""
    client = FakeLLMClient()
    client.enqueue_text(text)
    with pytest.raises(ValidationError):
        await ReviewerAgent(client, model="fake/reviewer").run(_input(*review_inputs))


@pytest.mark.parametrize(
    "wrapper",
    [
        "```json\n" + _response() + "\n```",
        "```\n" + _response() + "\n```",
        "Here is the review:\n```json\n" + _response() + "\n```\nHope that helps!",
    ],
)
async def test_reviewer_accepts_markdown_code_blocks(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults], wrapper: str
) -> None:
    """[PROMPT-02]: Verify markdown code block wrappers are stripped before JSON validation."""
    client = FakeLLMClient()
    client.enqueue_text(wrapper)
    verdict = await ReviewerAgent(client, model="fake/reviewer").run(_input(*review_inputs))
    assert verdict.verdict == "APPROVED"
    assert verdict.summary == "Reviewed the health check"


async def test_reviewer_accepts_multiple_code_blocks_with_explanatory_snippet(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults],
) -> None:
    """[PROMPT-02]: Verify multi-block responses with an explanatory snippet parse the verdict block."""
    multi_block = (
        "Here is an explanatory snippet:\n"
        "```json\n"
        '{\n  "note": "inspecting changes"\n}\n'
        "```\n"
        "And here is the final review verdict:\n"
        "```json\n" + _response() + "\n```\n"
    )
    client = FakeLLMClient()
    client.enqueue_text(multi_block)
    agent = ReviewerAgent(client, model="fake/reviewer", max_schema_retries=0)
    verdict = await agent.run(_input(*review_inputs))
    assert verdict.verdict == "APPROVED"
    assert verdict.summary == "Reviewed the health check"
    assert client.call_count == 1


async def test_reviewer_recovers_via_schema_retry(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults],
) -> None:
    """[PROMPT-02]: Verify Reviewer prompts the model on schema failure and recovers on retry."""
    client = FakeLLMClient()
    # Turn 1: Invalid schema
    client.enqueue_text("I cannot provide JSON yet")
    # Turn 2: Recovered with valid schema
    client.enqueue_text(_response())

    agent = ReviewerAgent(client, model="fake/reviewer", max_schema_retries=2)
    verdict = await agent.run(_input(*review_inputs))
    assert verdict.verdict == "APPROVED"
    assert client.call_count == 2
    # Verify retry prompt was sent
    retry_msg = client.requests[1].messages[-1].content
    assert "validation failed" in retry_msg


async def test_reviewer_fails_after_exhausting_schema_retries(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults],
) -> None:
    """[PROMPT-02]: Verify ValidationError is raised when schema retries are exhausted."""
    client = FakeLLMClient()
    client.enqueue_text("invalid 1")
    client.enqueue_text("invalid 2")

    agent = ReviewerAgent(client, model="fake/reviewer", max_schema_retries=1)
    with pytest.raises(ValidationError):
        await agent.run(_input(*review_inputs))
    assert client.call_count == 2


@pytest.mark.parametrize(
    "reason", [reason for reason in StopReason if reason != StopReason.END_TURN]
)
async def test_reviewer_rejects_incomplete_completion(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults], reason: StopReason
) -> None:
    """Verify even valid-looking JSON cannot approve an incomplete response."""
    client = FakeLLMClient()
    client.enqueue(LLMResponse(content=_response(), stop_reason=reason))
    with pytest.raises(ValueError, match="complete response"):
        await ReviewerAgent(client, model="fake/reviewer").run(_input(*review_inputs))


async def test_reviewer_rejects_tool_calls(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults],
) -> None:
    """Verify tool calls are never executed or accepted as a review."""
    client = FakeLLMClient()
    client.enqueue(
        LLMResponse(
            content=_response(),
            tool_calls=[ToolCall(id="call-001", name="shell", arguments={"command": "pytest"})],
        )
    )
    with pytest.raises(ValueError, match="without tool calls"):
        await ReviewerAgent(client, model="fake/reviewer").run(_input(*review_inputs))


@pytest.mark.parametrize(
    ("verdict", "issues"),
    [("APPROVED", ["A blocking defect"]), ("CHANGES_REQUESTED", []), ("REJECTED", [])],
)
async def test_reviewer_rejects_inconsistent_verdict(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults],
    verdict: ReviewVerdictStatus,
    issues: list[str],
) -> None:
    """Verify routing decisions agree with their blocking issue lists."""
    client = FakeLLMClient()
    client.enqueue_text(_response(verdict, blocking_issues=issues))
    with pytest.raises(ValueError, match="blocking issues"):
        await ReviewerAgent(client, model="fake/reviewer").run(_input(*review_inputs))


@pytest.mark.parametrize("field", ["summary", "content", "blocking_issues"])
async def test_reviewer_rejects_blank_text(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults], field: str
) -> None:
    """Verify whitespace cannot substitute for review findings."""
    payload = json.loads(_response("CHANGES_REQUESTED", blocking_issues=["Missing coverage"]))
    payload[field] = [" \n"] if field == "blocking_issues" else " \n"
    client = FakeLLMClient()
    client.enqueue_text(json.dumps(payload))
    with pytest.raises(ValueError, match="blank"):
        await ReviewerAgent(client, model="fake/reviewer").run(_input(*review_inputs))


@pytest.mark.parametrize("field", ["passed", "execution_error", "exit_code", "tests_failed"])
async def test_reviewer_cannot_approve_failing_tests(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults], field: str
) -> None:
    """Verify each deterministic failure signal coerces approval to CHANGES_REQUESTED."""
    spec, changes, results = review_inputs
    results = results.model_copy(
        update={
            field: False if field == "passed" else ("crash" if field == "execution_error" else 1)
        }
    )
    client = FakeLLMClient()
    client.enqueue_text(_response())
    verdict = await ReviewerAgent(client, model="fake/reviewer").run(_input(spec, changes, results))
    assert verdict.verdict == "CHANGES_REQUESTED"
    assert "Changes cannot be approved while tests fail" in verdict.blocking_issues


@pytest.mark.parametrize("field", ["has_open_questions", "open_questions"])
async def test_reviewer_cannot_approve_open_questions(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults], field: str
) -> None:
    """Verify unresolved planning questions block approval."""
    spec, changes, results = review_inputs
    spec = spec.model_copy(
        update={field: True if field == "has_open_questions" else ["Which API?"]}
    )
    client = FakeLLMClient()
    client.enqueue_text(_response())
    with pytest.raises(ValueError, match="open questions"):
        await ReviewerAgent(client, model="fake/reviewer").run(_input(spec, changes, results))


async def test_reviewer_cannot_approve_out_of_scope_changes(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults],
) -> None:
    """Verify the specification's file whitelist is enforced on approval."""
    spec, changes, results = review_inputs
    changes = changes.model_copy(update={"modified_files": ["src/unrelated.py"]})
    client = FakeLLMClient()
    client.enqueue_text(_response())
    with pytest.raises(ValueError, match="outside the specification"):
        await ReviewerAgent(client, model="fake/reviewer").run(_input(spec, changes, results))


async def test_reviewer_can_request_changes_for_failing_tests(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults],
) -> None:
    """Verify failing evidence can still produce an actionable review artifact."""
    spec, changes, results = review_inputs
    results = results.model_copy(update={"passed": False, "exit_code": 1, "tests_failed": 1})
    client = FakeLLMClient()
    client.enqueue_text(_response("CHANGES_REQUESTED", blocking_issues=["Fix the failing test"]))
    result = await ReviewerAgent(client, model="fake/reviewer").run(_input(spec, changes, results))
    assert result.verdict == "CHANGES_REQUESTED"


async def test_reviewer_propagates_provider_errors(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults],
) -> None:
    """Verify provider failures cannot become approval artifacts or silent retries."""
    client = FakeLLMClient()
    error = LLMProviderError("Provider unavailable", model="fake/reviewer")
    client.enqueue(error)
    with pytest.raises(LLMProviderError) as exc_info:
        await ReviewerAgent(client, model="fake/reviewer").run(_input(*review_inputs))
    assert exc_info.value is error
    assert client.call_count == 1


@pytest.mark.parametrize("model", ["", " \n"])
def test_reviewer_requires_model(model: str) -> None:
    """Verify an empty model identifier fails before a request is attempted."""
    with pytest.raises(ValueError, match="model must not be blank"):
        ReviewerAgent(FakeLLMClient(), model=model)


async def test_reviewer_artifact_roundtrip(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults], tmp_path: Path
) -> None:
    """Verify the review uses the current asynchronous, versioned artifact store."""
    client = FakeLLMClient()
    client.enqueue_text(_response())
    result = await ReviewerAgent(client, model="fake/reviewer").run(_input(*review_inputs))
    store = FileSystemArtifactStore(tmp_path)
    ref = await store.save(result.task_id, "review", result)

    assert await store.load(ref, ReviewVerdict) == result
    assert await store.load_latest(result.task_id, "review", ReviewVerdict) == result
    assert await store.history(result.task_id, "review") == [ref]


async def test_reviewer_resets_context_between_runs(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults],
) -> None:
    """Verify a reused agent never forwards a previous review conversation."""
    client = FakeLLMClient()
    client.enqueue_text(_response("CHANGES_REQUESTED", blocking_issues=["Add coverage"]))
    client.enqueue_text(_response())
    agent = ReviewerAgent(client, model="fake/reviewer")

    first = await agent.run(_input(*review_inputs))
    second = await agent.run(_input(*review_inputs))

    assert first.verdict == "CHANGES_REQUESTED"
    assert second.verdict == "APPROVED"
    assert client.requests[0] == client.requests[1]


async def test_reviewer_uses_shared_budget_guard(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults],
) -> None:
    """Verify the same budget protocol used by Coder and Tester gates LLM work."""

    class ExhaustedBudget:
        def check(self, task_id: str, est_tokens: int = 0) -> None:
            raise BudgetExceededError(f"Budget exhausted for {task_id}")

    client = FakeLLMClient()
    agent = ReviewerAgent(client, model="fake/reviewer", budget_guard=ExhaustedBudget())
    with pytest.raises(BudgetExceededError, match="task-001"):
        await agent.run(_input(*review_inputs))
    assert client.call_count == 0


async def test_reviewer_emits_shared_events(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults],
) -> None:
    """Verify reviewer telemetry preserves task, attempt, usage, and routing status."""
    client = FakeLLMClient()
    client.enqueue_text(_response())
    sink = InMemoryEventSink()
    data = _input(*review_inputs).model_copy(update={"attempt": 3})
    await ReviewerAgent(client, model="fake/reviewer", event_sink=sink).run(data)

    assert [event.event_type for event in sink.events] == [
        "reviewer.started",
        "reviewer.llm_completion",
        "reviewer.completed",
    ]
    assert all(event.agent == "reviewer" for event in sink.events)
    assert all(event.task_id == data.task_id and event.attempt == 3 for event in sink.events)
    assert sink.events[1].payload["tokens"]["total_tokens"] > 0
    assert sink.events[2].payload == {"verdict": "APPROVED", "blocking_issues": []}


async def test_reviewer_rejects_outer_task_mismatch(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults],
) -> None:
    """Verify the orchestration task identifier must also match every artifact."""
    client = FakeLLMClient()
    data = _input(*review_inputs).model_copy(update={"task_id": "other-task"})
    with pytest.raises(ValueError, match="same task"):
        await ReviewerAgent(client, model="fake/reviewer").run(data)
    assert client.call_count == 0


def test_reviewer_input_is_frozen_and_validated(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults],
) -> None:
    """Verify ReviewerInput follows the immutable agent input convention."""
    data = _input(*review_inputs)
    with pytest.raises(ValidationError):
        data.attempt = 2
    for update in ({"attempt": 0}, {"scratchpad": "upstream conversation"}):
        with pytest.raises(ValidationError):
            ReviewerInput.model_validate({**data.model_dump(), **update})


async def test_reviewer_custom_prompt(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults], tmp_path: Path
) -> None:
    """Verify the shared prompt_path constructor convention."""
    prompt = tmp_path / "custom.md"
    prompt.write_text("Custom review instructions", encoding="utf-8")
    client = FakeLLMClient()
    client.enqueue_text(_response())
    await ReviewerAgent(client, model="fake/reviewer", prompt_path=prompt).run(
        _input(*review_inputs)
    )
    assert client.requests[0].messages[0].content == "Custom review instructions"


async def test_reviewer_missing_prompt_fails_closed(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults], tmp_path: Path
) -> None:
    """Verify a missing system prompt cannot silently remove review rules."""
    client = FakeLLMClient()
    agent = ReviewerAgent(client, model="fake/reviewer", prompt_path=tmp_path / "missing.md")
    with pytest.raises(FileNotFoundError):
        await agent.run(_input(*review_inputs))
    assert client.call_count == 0


@pytest.mark.parametrize("verdict", ["APPROVED", "CHANGES_REQUESTED"])
async def test_reviewer_handles_coder_escalation(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults], verdict: ReviewVerdictStatus
) -> None:
    """Verify an escalated Coder artifact cannot pass review, even with passing tests."""
    spec, _, results = review_inputs
    changes = CodeChanges(
        task_id=spec.task_id,
        status="ESCALATED",
        summary="Implementation stopped",
        escalation="Scope violation",
        content="# Escalation",
    )
    client = FakeLLMClient()
    client.enqueue_text(
        _response(verdict, blocking_issues=[] if verdict == "APPROVED" else ["Resolve escalation"])
    )
    agent = ReviewerAgent(client, model="fake/reviewer")
    if verdict == "APPROVED":
        with pytest.raises(ValueError, match="escalated"):
            await agent.run(_input(spec, changes, results))
    else:
        result = await agent.run(_input(spec, changes, results))
        assert result.verdict == "CHANGES_REQUESTED"


async def test_reviewer_matches_coder_path_normalization(
    review_inputs: tuple[PlanSpec, CodeChanges, TestResults],
) -> None:
    """Verify Coder-normalized relative paths remain within the plan's scope."""
    spec, changes, results = review_inputs
    spec = spec.model_copy(update={"target_files": [" ./src/health.py "]})
    client = FakeLLMClient()
    client.enqueue_text(_response())
    result = await ReviewerAgent(client, model="fake/reviewer").run(_input(spec, changes, results))
    assert result.verdict == "APPROVED"


@pytest.mark.parametrize("test_exit_code", [0, 1])
async def test_coder_tester_reviewer_handoff(tmp_path: Path, test_exit_code: int) -> None:
    """Exercise real Coder/Tester artifact production before read-only review."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("def health():\n    return 'ok'\n", encoding="utf-8")
    spec = PlanSpec(
        task_id="handoff-1",
        summary="Add health check",
        target_files=["app.py"],
        acceptance_criteria=["health returns ok"],
        content="# Specification\nReturn ok.",
    )
    sandbox = FakeSandboxRuntime(SandboxConfig(task_id=spec.task_id, workspace_path=workspace))
    sink = InMemoryEventSink()
    coder_llm = FakeLLMClient()
    coder_llm.enqueue_text(
        json.dumps(
            {
                "status": "COMPLETED",
                "summary": "Added health",
                "content": "# Changes",
                "test_attention": ["Verify health returns ok"],
            }
        )
    )
    coder = CoderAgent(
        llm=coder_llm,
        sandbox=sandbox,
        workspace_root=workspace,
        event_sink=sink,
        workspace_diff=FakeWorkspaceDiff(
            diff="--- a/app.py\n+++ b/app.py\n@@ -0,0 +1,2 @@\n+def health():\n+    return 'ok'",
            changed_files=["app.py"],
        ),
    )
    changes = await coder.run(CoderInput(task_id=spec.task_id, spec=spec))
    assert changes.status == "COMPLETED"

    tester_llm = FakeLLMClient()
    tester_llm.enqueue(
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="test-call",
                    name="run_test_command",
                    arguments={"command": ["pytest"]},
                )
            ]
        )
    )
    tester_llm.enqueue_text(json.dumps({"status": "COMPLETED", "content": "# Test Report"}))
    sandbox.queue_response(
        exit_code=test_exit_code, stdout="1 passed" if test_exit_code == 0 else "1 failed"
    )
    async with sandbox:
        results = await TesterAgent(
            llm=tester_llm,
            sandbox=sandbox,
            workspace_root=workspace,
            event_sink=sink,
        ).run(TesterInput(task_id=spec.task_id, spec=spec, changes=changes))
    assert results.passed == (test_exit_code == 0)

    before = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    commands_before = list(sandbox.executed_commands)
    reviewer_llm = FakeLLMClient()
    expected: ReviewVerdictStatus = "APPROVED" if results.passed else "CHANGES_REQUESTED"
    reviewer_llm.enqueue_text(
        _response(expected, blocking_issues=[] if results.passed else ["Fix failing test"])
    )
    verdict = await ReviewerAgent(reviewer_llm, model="fake/reviewer", event_sink=sink).run(
        _input(spec, changes, results)
    )
    assert verdict.verdict == expected
    assert sandbox.executed_commands == commands_before
    assert {
        p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()
    } == before
    assert {event.agent for event in sink.events} == {"coder", "tester", "reviewer"}
    assert reviewer_llm.requests[0].tools == []

    store = FileSystemArtifactStore(tmp_path / "runs")
    inputs = [
        await store.save(spec.task_id, name, artifact)
        for name, artifact in (
            ("spec", spec),
            ("code_changes", changes),
            ("test_results", results),
        )
    ]
    ref = await store.save(
        spec.task_id,
        "review",
        verdict,
        inputs=inputs,
        produced_by=Provenance(agent="reviewer", model="fake/reviewer"),
    )
    assert await store.load(ref, ReviewVerdict) == verdict
