"""Unit tests for CoderAgent execution loop, boundary verification, and error handling."""

import asyncio
from pathlib import Path

import pytest

from oswald.core.agents.coder import (
    BudgetExceededError,
    CoderAgent,
    CoderInput,
    InMemoryEventSink,
)
from oswald.core.artifacts.models import Finding, PlanSpec, TaskItem
from oswald.core.llm.adapters.fake import FakeLLMClient
from oswald.core.llm.types import (
    LLMResponse,
    Role,
    StopReason,
    TokenUsage,
    ToolCall,
)
from oswald.core.sandbox.fake import FakeSandboxRuntime
from oswald.core.sandbox.models import SandboxConfig
from oswald.core.vcs.workspace import FakeWorkspaceDiff


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Fixture providing a mock workspace directory."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (ws / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    return ws


def create_sample_spec() -> PlanSpec:
    return PlanSpec(
        task_id="task-001",
        summary="Add greeting function",
        target_files=["main.py"],
        tasks=[
            TaskItem(
                step=1,
                description="Add greet function",
                target_file="main.py",
                action="modify",
            )
        ],
        acceptance_criteria=["greet('world') returns 'Hello world'"],
        content="# Plan Spec\nImplement greet function in main.py.",
    )


@pytest.mark.asyncio
async def test_coder_agent_happy_path(workspace: Path) -> None:
    """Verify CoderAgent executes tool loop and produces valid CodeChanges."""
    fake_llm = FakeLLMClient()
    config = SandboxConfig(task_id="task-001", workspace_path=workspace)
    fake_sandbox = FakeSandboxRuntime(config)
    fake_diff = FakeWorkspaceDiff(
        diff="--- a/main.py\n+++ b/main.py\n@@ -1 +1,3 @@\n+def greet(): return 'Hello'\n",
        changed_files=["main.py"],
    )
    event_sink = InMemoryEventSink()

    agent = CoderAgent(
        llm=fake_llm,
        sandbox=fake_sandbox,
        workspace_diff=fake_diff,
        workspace_root=workspace,
        event_sink=event_sink,
    )

    # Turn 1: Model calls read_file_range
    fake_llm.enqueue(
        LLMResponse(
            content="Let me inspect main.py first.",
            tool_calls=[
                ToolCall(
                    id="call_read_1",
                    name="read_file_range",
                    arguments={"file_path": "main.py", "start_line": 1, "end_line": 2},
                )
            ],
            stop_reason=StopReason.TOOL_USE,
            usage=TokenUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
        )
    )

    # Turn 2: Model calls edit_file
    fake_llm.enqueue(
        LLMResponse(
            content="Now I will update main.py.",
            tool_calls=[
                ToolCall(
                    id="call_edit_1",
                    name="edit_file",
                    arguments={
                        "file_path": "main.py",
                        "old_str": "print('hello')",
                        "new_str": "def greet(name: str) -> str:\n    return f'Hello {name}'\n",
                    },
                )
            ],
            stop_reason=StopReason.TOOL_USE,
            usage=TokenUsage(prompt_tokens=150, completion_tokens=30, total_tokens=180),
        )
    )

    # Turn 3: Model completes with final JSON
    completion_json = """```json
{
  "status": "COMPLETED",
  "summary": "Implemented greet function in main.py",
  "content": "# Changes\nAdded type-annotated greet function.",
  "deviations": [],
  "known_limitations": [],
  "test_attention": ["Check return value with empty string"]
}
```"""
    fake_llm.enqueue(
        LLMResponse(
            content=completion_json,
            stop_reason=StopReason.END_TURN,
            usage=TokenUsage(prompt_tokens=200, completion_tokens=50, total_tokens=250),
        )
    )

    spec = create_sample_spec()
    inp = CoderInput(task_id="task-001", spec=spec)

    changes = await agent.run(inp)

    assert changes.status == "COMPLETED"
    assert changes.summary == "Implemented greet function in main.py"
    assert changes.modified_files == ["main.py"]
    assert "--- a/main.py" in changes.diff
    assert changes.test_attention == ["Check return value with empty string"]

    # Verify event telemetry
    event_types = [e.event_type for e in event_sink.events]
    assert "coder.started" in event_types
    assert "coder.llm_completion" in event_types
    assert "coder.tool_executed" in event_types
    assert "coder.completed" in event_types


@pytest.mark.asyncio
async def test_coder_agent_protocol_correctness(workspace: Path) -> None:
    """Verify assistant messages and tool responses contain correct roles and IDs."""
    fake_llm = FakeLLMClient()
    fake_sandbox = FakeSandboxRuntime(SandboxConfig(task_id="t-01", workspace_path=workspace))
    fake_diff = FakeWorkspaceDiff(diff="+foo", changed_files=["main.py"])

    agent = CoderAgent(
        llm=fake_llm,
        sandbox=fake_sandbox,
        workspace_diff=fake_diff,
        workspace_root=workspace,
    )

    fake_llm.enqueue(
        LLMResponse(
            content="Reading file",
            tool_calls=[
                ToolCall(
                    id="call_abc_123",
                    name="read_file_range",
                    arguments={"file_path": "main.py"},
                )
            ],
            stop_reason=StopReason.TOOL_USE,
        )
    )
    fake_llm.enqueue(
        LLMResponse(
            content='```json\n{"status": "COMPLETED", "summary": "Done", "content": "Done"}\n```',
            stop_reason=StopReason.END_TURN,
        )
    )

    await agent.run(CoderInput(task_id="t-01", spec=create_sample_spec()))

    # Second LLM request should contain the previous assistant tool call and tool result
    req2 = fake_llm.requests[1]
    tool_resp_msgs = [m for m in req2.messages if m.role == Role.TOOL]
    assert len(tool_resp_msgs) == 1
    assert tool_resp_msgs[0].tool_call_id == "call_abc_123"
    assert "print('hello')" in tool_resp_msgs[0].content


@pytest.mark.asyncio
async def test_coder_agent_scope_violation_detection(workspace: Path) -> None:
    """Verify diff touching unauthorized files results in scope violation escalation."""
    fake_llm = FakeLLMClient()
    fake_sandbox = FakeSandboxRuntime(SandboxConfig(task_id="t-02", workspace_path=workspace))
    # Simulated diff modifying unapproved file 'unauthorized.py'
    fake_diff = FakeWorkspaceDiff(
        diff="+unauthorized",
        changed_files=["main.py", "unauthorized.py"],
    )

    agent = CoderAgent(
        llm=fake_llm,
        sandbox=fake_sandbox,
        workspace_diff=fake_diff,
        workspace_root=workspace,
    )

    fake_llm.enqueue(
        LLMResponse(
            content='```json\n{"status": "COMPLETED", "summary": "Done", "content": "Done"}\n```',
            stop_reason=StopReason.END_TURN,
        )
    )

    spec = create_sample_spec()
    res = await agent.run(CoderInput(task_id="t-02", spec=spec))

    assert res.status == "ESCALATED"
    assert "Scope violation" in res.summary
    assert res.escalation is not None
    assert "unauthorized.py" in res.escalation


@pytest.mark.asyncio
async def test_coder_agent_circuit_breaker(workspace: Path) -> None:
    """Verify circuit breaker trips when the exact same tool call fails 3 consecutive times."""
    fake_llm = FakeLLMClient()
    fake_sandbox = FakeSandboxRuntime(SandboxConfig(task_id="t-03", workspace_path=workspace))
    fake_diff = FakeWorkspaceDiff()

    agent = CoderAgent(
        llm=fake_llm,
        sandbox=fake_sandbox,
        workspace_diff=fake_diff,
        workspace_root=workspace,
    )

    # Enqueue 3 repeated failing tool calls (editing a non-existent file)
    bad_call = ToolCall(
        id="bad_1",
        name="edit_file",
        arguments={
            "file_path": "non_existent.py",
            "old_str": "foo",
            "new_str": "bar",
        },
    )

    for _ in range(3):
        fake_llm.enqueue(
            LLMResponse(
                content="Trying bad edit",
                tool_calls=[bad_call],
                stop_reason=StopReason.TOOL_USE,
            )
        )

    spec = create_sample_spec()
    res = await agent.run(CoderInput(task_id="t-03", spec=spec))

    assert res.status == "ESCALATED"
    assert "Circuit breaker" in res.summary
    assert res.escalation is not None
    assert "Circuit breaker" in res.escalation


@pytest.mark.asyncio
async def test_coder_agent_budget_guard(workspace: Path) -> None:
    """Verify BudgetGuard halts execution immediately when budget ceiling is exceeded."""
    fake_llm = FakeLLMClient()
    fake_sandbox = FakeSandboxRuntime(SandboxConfig(task_id="t-04", workspace_path=workspace))
    fake_diff = FakeWorkspaceDiff()

    class StrictBudgetGuard:
        def check(self, task_id: str, est_tokens: int = 0) -> None:
            raise BudgetExceededError("Token limit exceeded")

    agent = CoderAgent(
        llm=fake_llm,
        sandbox=fake_sandbox,
        workspace_diff=fake_diff,
        workspace_root=workspace,
        budget_guard=StrictBudgetGuard(),
    )

    with pytest.raises(BudgetExceededError):
        await agent.run(CoderInput(task_id="t-04", spec=create_sample_spec()))

    assert fake_llm.call_count == 0


@pytest.mark.asyncio
async def test_coder_agent_schema_retry(workspace: Path) -> None:
    """Verify CoderAgent retries when model returns invalid JSON and succeeds when corrected."""
    fake_llm = FakeLLMClient()
    fake_sandbox = FakeSandboxRuntime(SandboxConfig(task_id="t-05", workspace_path=workspace))
    fake_diff = FakeWorkspaceDiff(diff="+fixed", changed_files=["main.py"])

    agent = CoderAgent(
        llm=fake_llm,
        sandbox=fake_sandbox,
        workspace_diff=fake_diff,
        workspace_root=workspace,
        max_schema_retries=2,
    )

    # 1. Invalid non-JSON answer
    fake_llm.enqueue(
        LLMResponse(
            content="I have completed the changes. Everything looks great!",
            stop_reason=StopReason.END_TURN,
        )
    )

    # 2. Corrected valid JSON
    fake_llm.enqueue(
        LLMResponse(
            content='```json\n{"status": "COMPLETED", "summary": "Fixed", "content": "Done"}\n```',
            stop_reason=StopReason.END_TURN,
        )
    )

    spec = create_sample_spec()
    res = await agent.run(CoderInput(task_id="t-05", spec=spec))

    assert res.status == "COMPLETED"
    assert res.summary == "Fixed"
    assert fake_llm.call_count == 2


@pytest.mark.asyncio
async def test_coder_agent_fix_loop_context_isolation(workspace: Path) -> None:
    """Verify fix loop input (DoD #4) contains only structured findings and no other agent logs."""
    fake_llm = FakeLLMClient()
    fake_sandbox = FakeSandboxRuntime(SandboxConfig(task_id="t-06", workspace_path=workspace))
    fake_diff = FakeWorkspaceDiff(diff="+fix", changed_files=["main.py"])

    agent = CoderAgent(
        llm=fake_llm,
        sandbox=fake_sandbox,
        workspace_diff=fake_diff,
        workspace_root=workspace,
    )

    fake_llm.enqueue(
        LLMResponse(
            content='```json\n{"status": "COMPLETED", "summary": "Fixed finding", "content": "Done"}\n```',
            stop_reason=StopReason.END_TURN,
        )
    )

    finding = Finding(
        source="tester",
        severity="blocker",
        problem="DivisionByZeroError on empty input",
        location="main.py:10",
        test_id="test_empty",
        why_it_matters="Crashes worker process",
        expected_correction="Guard against empty list",
        evidence="ZeroDivisionError: division by zero",
    )

    inp = CoderInput(
        task_id="t-06",
        attempt=2,
        spec=create_sample_spec(),
        findings=[finding],
    )

    await agent.run(inp)

    # Inspect initial user prompt delivered to LLM
    first_req = fake_llm.requests[0]
    user_msg = next(m for m in first_req.messages if m.role == Role.USER)

    assert "Fix Loop Findings" in user_msg.content
    assert "DivisionByZeroError on empty input" in user_msg.content
    assert "TESTER | BLOCKER" in user_msg.content
    assert "main.py:10" in user_msg.content


@pytest.mark.asyncio
async def test_coder_agent_concurrency_isolation(tmp_path: Path) -> None:
    """Verify two concurrent CoderAgent executions in separate workspaces do not interfere (DoD #6)."""
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    (ws1 / "main.py").write_text("ws1\n", encoding="utf-8")
    (ws2 / "main.py").write_text("ws2\n", encoding="utf-8")

    llm1 = FakeLLMClient()
    llm2 = FakeLLMClient()

    sb1 = FakeSandboxRuntime(SandboxConfig(task_id="task-c1", workspace_path=ws1))
    sb2 = FakeSandboxRuntime(SandboxConfig(task_id="task-c2", workspace_path=ws2))

    diff1 = FakeWorkspaceDiff(diff="+ws1_mod", changed_files=["main.py"])
    diff2 = FakeWorkspaceDiff(diff="+ws2_mod", changed_files=["main.py"])

    agent1 = CoderAgent(llm=llm1, sandbox=sb1, workspace_diff=diff1, workspace_root=ws1)
    agent2 = CoderAgent(llm=llm2, sandbox=sb2, workspace_diff=diff2, workspace_root=ws2)

    llm1.enqueue(
        LLMResponse(
            content='```json\n{"status": "COMPLETED", "summary": "Done 1", "content": "1"}\n```',
            stop_reason=StopReason.END_TURN,
        )
    )
    llm2.enqueue(
        LLMResponse(
            content='```json\n{"status": "COMPLETED", "summary": "Done 2", "content": "2"}\n```',
            stop_reason=StopReason.END_TURN,
        )
    )

    spec1 = PlanSpec(task_id="task-c1", summary="Plan 1", target_files=["main.py"], content="# 1")
    spec2 = PlanSpec(task_id="task-c2", summary="Plan 2", target_files=["main.py"], content="# 2")

    res1, res2 = await asyncio.gather(
        agent1.run(CoderInput(task_id="task-c1", spec=spec1)),
        agent2.run(CoderInput(task_id="task-c2", spec=spec2)),
    )

    assert res1.task_id == "task-c1"
    assert res1.summary == "Done 1"
    assert res1.diff == "+ws1_mod"

    assert res2.task_id == "task-c2"
    assert res2.summary == "Done 2"
    assert res2.diff == "+ws2_mod"
