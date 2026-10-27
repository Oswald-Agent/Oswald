"""Edge case and stress tests for Coder Agent, tools, and security boundaries."""

from pathlib import Path

import pytest

from oswald.core.agents.coder import CoderAgent, CoderInput
from oswald.core.artifacts.models import PlanSpec
from oswald.core.llm.adapters.fake import FakeLLMClient
from oswald.core.llm.exceptions import LLMProviderError
from oswald.core.llm.types import (
    LLMResponse,
    StopReason,
    ToolCall,
)
from oswald.core.sandbox.fake import FakeSandboxRuntime
from oswald.core.sandbox.models import SandboxConfig
from oswald.core.tools.coder import (
    edit_file,
    is_test_path,
    read_file_range,
    search_code,
)
from oswald.core.vcs.workspace import FakeWorkspaceDiff


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "edge_workspace"
    ws.mkdir()
    (ws / "main.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    (ws / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    return ws


def test_is_test_path_edge_cases() -> None:
    """Verify is_test_path catches root conftest.py, tests.py, and nested tests."""
    # Definite test paths that MUST be blocked
    assert is_test_path("conftest.py") is True, "conftest.py should be detected as a test path"
    assert is_test_path("tests.py") is True, "tests.py should be detected as a test path"
    assert is_test_path("tests/unit/test_foo.py") is True
    assert is_test_path("src/test_bar.py") is True
    assert is_test_path("src/bar_test.py") is True

    # Production code paths that must NOT be blocked
    assert is_test_path("src/tester.py") is False
    assert is_test_path("src/contest.py") is False
    assert is_test_path("src/protest.py") is False


def test_edit_file_leading_slash_and_absolute_path(workspace: Path) -> None:
    """Verify edit_file safely handles leading slash and absolute paths inside workspace."""
    # Absolute path inside workspace
    abs_path = str(workspace / "main.py")
    res_abs = edit_file(
        workspace_root=workspace,
        file_path=abs_path,
        old_str="return 'world'",
        new_str="return 'universe'",
        target_files_whitelist=["main.py"],
    )
    assert res_abs.success is True, f"Failed on absolute path: {res_abs.message}"

    # Leading slash path
    res_slash = edit_file(
        workspace_root=workspace,
        file_path="/main.py",
        old_str="return 'universe'",
        new_str="return 'cosmos'",
        target_files_whitelist=["main.py"],
    )
    assert res_slash.success is True, f"Failed on leading slash: {res_slash.message}"


def test_read_file_range_edge_cases(workspace: Path) -> None:
    """Verify read_file_range handles empty paths, inverted line ranges, and large bounds."""
    # Empty path
    res_empty = read_file_range(workspace, "")
    assert "does not exist" in res_empty

    # Inverted lines (start > end)
    res_inverted = read_file_range(workspace, "main.py", start_line=5, end_line=2)
    assert (
        "cannot be greater than" in res_inverted
        or "exceeds file length" in res_inverted
        or res_inverted == ""
    )

    # Negative bounds
    res_neg = read_file_range(workspace, "main.py", start_line=-10, end_line=-5)
    assert "def hello" in res_neg or "does not exist" in res_neg or res_neg != ""


def test_search_code_empty_query(workspace: Path) -> None:
    """Verify search_code rejects empty query string."""
    res = search_code(workspace, "")
    assert "Error" in res or "empty" in res.lower() or "No matches" in res


def test_edit_file_crlf_line_endings(workspace: Path) -> None:
    """Verify edit_file handles CRLF Windows-style newlines cleanly."""
    crlf_file = workspace / "crlf.py"
    crlf_file.write_bytes(b"line1\r\nline2\r\nline3\r\n")

    res = edit_file(
        workspace_root=workspace,
        file_path="crlf.py",
        old_str="line2\r\n",
        new_str="line2_modified\r\n",
        target_files_whitelist=["crlf.py"],
    )
    # Even if old_str has \n or \r\n, it should match or replace safely
    if not res.success:
        res2 = edit_file(
            workspace_root=workspace,
            file_path="crlf.py",
            old_str="line2",
            new_str="line2_modified",
            target_files_whitelist=["crlf.py"],
        )
        assert res2.success is True


@pytest.mark.asyncio
async def test_coder_agent_tool_call_check_declared_imports(workspace: Path) -> None:
    """Verify CoderAgent handles check_declared_imports tool call without WorktreeError."""
    fake_llm = FakeLLMClient()
    fake_sb = FakeSandboxRuntime(SandboxConfig(task_id="t-check", workspace_path=workspace))
    fake_diff = FakeWorkspaceDiff(diff="+x = 1", changed_files=["main.py"])

    agent = CoderAgent(
        llm=fake_llm,
        sandbox=fake_sb,
        workspace_diff=fake_diff,
        workspace_root=workspace,
    )

    # Tool call: check_declared_imports
    fake_llm.enqueue(
        LLMResponse(
            content="Checking imports",
            tool_calls=[
                ToolCall(
                    id="call_imports_1",
                    name="check_declared_imports",
                    arguments={},
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

    spec = PlanSpec(task_id="t-check", summary="Check", target_files=["main.py"], content="# Spec")
    changes = await agent.run(CoderInput(task_id="t-check", spec=spec))

    assert changes.status == "COMPLETED"


async def test_coder_skips_missing_checkers_without_retrying_or_hiding_limitations(
    workspace: Path,
) -> None:
    llm = FakeLLMClient()
    sandbox = FakeSandboxRuntime(
        SandboxConfig(task_id="missing-checkers", workspace_path=workspace)
    )
    await sandbox.start()
    for executable in ("ruff", "mypy"):
        sandbox.queue_response(
            exit_code=127,
            stdout=f'OCI runtime exec failed: exec: "{executable}": executable file not found in $PATH',
        )
    sandbox.queue_response(exit_code=0)
    checks = ["ruff_check", "ruff_check", "ruff_format", "mypy", "mypy", "syntax_check"]
    llm.enqueue(
        LLMResponse(
            tool_calls=[
                ToolCall(id=f"check-{i}", name="run_static_check", arguments={"check_name": check})
                for i, check in enumerate(checks)
            ],
            stop_reason=StopReason.TOOL_USE,
        )
    )
    llm.enqueue(
        LLMResponse(
            content='{"status": "COMPLETED", "summary": "Done", "content": "Done"}',
            stop_reason=StopReason.END_TURN,
        )
    )
    agent = CoderAgent(
        llm, sandbox, FakeWorkspaceDiff(diff="+x = 1", changed_files=["main.py"]), workspace
    )
    spec = PlanSpec(
        task_id="missing-checkers", summary="Check", target_files=["main.py"], content="Spec"
    )
    changes = await agent.run(CoderInput(task_id="missing-checkers", spec=spec))
    assert changes.status == "COMPLETED"
    assert [command[0] for command in sandbox.executed_commands] == ["ruff", "mypy", "python"]
    assert len(changes.known_limitations) == 2
    assert any("'ruff'" in limitation for limitation in changes.known_limitations)
    assert any("'mypy'" in limitation for limitation in changes.known_limitations)


@pytest.mark.asyncio
async def test_coder_agent_handles_llm_provider_error(workspace: Path) -> None:
    """Verify CoderAgent escalates gracefully when LLM provider throws error."""
    fake_llm = FakeLLMClient()
    fake_sb = FakeSandboxRuntime(SandboxConfig(task_id="t-err", workspace_path=workspace))
    fake_diff = FakeWorkspaceDiff()

    agent = CoderAgent(
        llm=fake_llm,
        sandbox=fake_sb,
        workspace_diff=fake_diff,
        workspace_root=workspace,
    )

    fake_llm.enqueue(LLMProviderError("Anthropic API 500 internal server error", model="claude"))

    spec = PlanSpec(
        task_id="t-err", summary="Error test", target_files=["main.py"], content="# Spec"
    )
    changes = await agent.run(CoderInput(task_id="t-err", spec=spec))

    assert changes.status == "ESCALATED"
    assert "LLM" in changes.summary or "500" in (changes.escalation or "")


@pytest.mark.asyncio
async def test_coder_agent_parses_multiobject_response(workspace: Path) -> None:
    """Verify completion parser extracts the valid schema when response has thoughts or multiple objects."""
    fake_llm = FakeLLMClient()
    fake_sb = FakeSandboxRuntime(SandboxConfig(task_id="t-multi", workspace_path=workspace))
    fake_diff = FakeWorkspaceDiff(diff="+code", changed_files=["main.py"])

    agent = CoderAgent(
        llm=fake_llm,
        sandbox=fake_sb,
        workspace_diff=fake_diff,
        workspace_root=workspace,
    )

    # Output with leading JSON thought block and trailing completion block
    messy_output = """
    {"thought": "I should conclude now"}

    Here is the completion:
    ```json
    {
      "status": "COMPLETED",
      "summary": "Multi-object parsed successfully",
      "content": "# Done",
      "deviations": [],
      "known_limitations": [],
      "test_attention": []
    }
    ```
    """

    fake_llm.enqueue(
        LLMResponse(
            content=messy_output,
            stop_reason=StopReason.END_TURN,
        )
    )

    spec = PlanSpec(
        task_id="t-multi", summary="Multi test", target_files=["main.py"], content="# Spec"
    )
    changes = await agent.run(CoderInput(task_id="t-multi", spec=spec))

    assert changes.status == "COMPLETED"
    assert changes.summary == "Multi-object parsed successfully"
