"""Unit tests for conversation history compaction and lean markdown context digests."""

from pathlib import Path

from oswald.core.agents.coder import CoderAgent, CoderInput
from oswald.core.agents.compaction import compact_tool_history
from oswald.core.agents.tester import TesterAgent, TesterInput
from oswald.core.artifacts.models import CodeChanges, PlanSpec, TaskItem
from oswald.core.llm.adapters.fake import FakeLLMClient
from oswald.core.llm.types import Message, Role
from oswald.core.sandbox.fake import FakeSandboxRuntime
from oswald.core.sandbox.models import SandboxConfig
from oswald.core.vcs.workspace import FakeWorkspaceDiff


def test_compact_tool_history_tombstones_older_than_three_turns() -> None:
    """Verify file-read tool outputs older than 3 turns are replaced with 1-line tombstones."""
    messages = [
        Message(role=Role.USER, content="Initial instruction"),
        Message(
            role=Role.TOOL, content="Line 1\nLine 2\nLine 3\nLine 4\n", tool_call_id="call-turn-1"
        ),
        Message(role=Role.TOOL, content="Turn 2 content\nline b", tool_call_id="call-turn-2"),
        Message(role=Role.TOOL, content="Turn 3 content\nline c", tool_call_id="call-turn-3"),
        Message(role=Role.TOOL, content="Turn 4 edit done", tool_call_id="call-turn-4-edit"),
    ]
    tool_call_turns = {
        "call-turn-1": (1, "read_file_range", "src/module.py"),
        "call-turn-2": (2, "search_code", "some_func"),
        "call-turn-3": (3, "list_files", "src"),
        "call-turn-4-edit": (1, "edit_file", "src/module.py"),
    }

    # At turn 3: turn 1 is 2 turns old (3 - 1 = 2 < 3) -> not tombstoned yet
    compacted_turn3 = compact_tool_history(messages, tool_call_turns, current_turn=3)
    assert "Line 1" in compacted_turn3[1].content
    assert "Historical" not in compacted_turn3[1].content

    # At turn 4: turn 1 is 3 turns old (4 - 1 = 3 >= 3) -> tombstoned!
    compacted_turn4 = compact_tool_history(messages, tool_call_turns, current_turn=4)
    tombstoned_msg = compacted_turn4[1]
    assert (
        "[Historical read_file_range output for 'src/module.py' (4 lines) tombstoned to conserve context]"
        in tombstoned_msg.content
    )
    assert "Line 1" not in tombstoned_msg.content

    # Turn 2 is 2 turns old (4 - 2 = 2 < 3) -> retained
    assert "Turn 2 content" in compacted_turn4[2].content

    # Turn 3 is 1 turn old (4 - 3 = 1 < 3) -> retained
    assert "Turn 3 content" in compacted_turn4[3].content

    # Non-file-read tool (edit_file) from turn 1 -> NEVER tombstoned
    assert compacted_turn4[4].content == "Turn 4 edit done"

    # At turn 5: turn 2 is now 3 turns old (5 - 2 = 3 >= 3) -> tombstoned!
    compacted_turn5 = compact_tool_history(compacted_turn4, tool_call_turns, current_turn=5)
    assert "[Historical search_code output for 'some_func'" in compacted_turn5[2].content
    assert "Turn 2 content" not in compacted_turn5[2].content


def test_compaction_multi_turn_reading_and_rereading_cycles() -> None:
    """Verify compaction across 15+ turns with multiple cycles of reading and re-reading the same path."""
    messages: list[Message] = [Message(role=Role.USER, content="Start")]
    tool_turns: dict[str, tuple[int, str, str]] = {}

    for turn in range(1, 17):
        messages = compact_tool_history(messages, tool_turns, current_turn=turn)
        if turn == 1:
            call_id = f"c-{turn}"
            messages.append(
                Message(role=Role.TOOL, content="initial app code\nline2", tool_call_id=call_id)
            )
            tool_turns[call_id] = (turn, "read_file_range", "app.py")
        elif turn == 3:
            call_id = f"c-{turn}"
            messages.append(
                Message(role=Role.TOOL, content="second app read\nline2", tool_call_id=call_id)
            )
            tool_turns[call_id] = (turn, "read_file_range", "app.py")
        elif turn == 10:
            call_id = f"c-{turn}"
            messages.append(
                Message(role=Role.TOOL, content="tenth turn read\nline2", tool_call_id=call_id)
            )
            tool_turns[call_id] = (turn, "read_file_range", "app.py")
        elif turn == 16:
            call_id = f"c-{turn}"
            messages.append(
                Message(role=Role.TOOL, content="fresh turn 16 read", tool_call_id=call_id)
            )
            tool_turns[call_id] = (turn, "read_file_range", "app.py")

    t1_msg = next(m for m in messages if m.tool_call_id == "c-1")
    assert "[Historical read_file_range output for 'app.py'" in t1_msg.content

    t3_msg = next(m for m in messages if m.tool_call_id == "c-3")
    assert "[Historical read_file_range output for 'app.py'" in t3_msg.content

    t10_msg = next(m for m in messages if m.tool_call_id == "c-10")
    assert "[Historical read_file_range output for 'app.py'" in t10_msg.content

    t16_msg = next(m for m in messages if m.tool_call_id == "c-16")
    assert t16_msg.content == "fresh turn 16 read"
    assert "Historical" not in t16_msg.content


def test_coder_initial_messages_passes_lean_digest(tmp_path: Path) -> None:
    """Verify CoderAgent omits redundant full specification markdown when tasks are present."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")

    spec = PlanSpec(
        task_id="t-digest",
        summary="Update app module",
        target_files=["app.py"],
        tasks=[
            TaskItem(
                step=1,
                description="Modify app.py",
                target_file="app.py",
                action="modify",
            )
        ],
        acceptance_criteria=["Criterion 1: x is 2"],
        content="# Full Specification\nThis is a long redundant markdown specification document.",
    )

    agent = CoderAgent(
        llm=FakeLLMClient(),
        sandbox=FakeSandboxRuntime(SandboxConfig(task_id="t-digest", workspace_path=workspace)),
        workspace_diff=FakeWorkspaceDiff(),
        workspace_root=workspace,
    )

    messages = agent._build_initial_messages(CoderInput(task_id="t-digest", spec=spec))
    user_prompt = messages[1].content

    # Structured digest elements must be present
    assert "## Summary\nUpdate app module" in user_prompt
    assert "1. [MODIFY] `app.py`: Modify app.py" in user_prompt
    assert "- Criterion 1: x is 2" in user_prompt

    # Redundant full spec dump must be excluded to conserve tokens
    assert "## Full Specification (spec.md)" not in user_prompt
    assert "This is a long redundant markdown specification document." not in user_prompt


def test_tester_initial_messages_passes_lean_digest(tmp_path: Path) -> None:
    """Verify TesterAgent omits redundant full specification markdown when criteria are present."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    spec = PlanSpec(
        task_id="t-digest-tester",
        summary="Verification of app",
        target_files=["app.py"],
        acceptance_criteria=["Criterion A: test passes"],
        content="# Full Spec\nRedundant spec markdown text.",
    )
    changes = CodeChanges(
        task_id="t-digest-tester",
        summary="Changed app.py",
        content="Subjective commentary",
        modified_files=["app.py"],
        diff="--- a/app.py\n+++ b/app.py\n",
        test_attention=["Check behavior"],
    )

    agent = TesterAgent(
        llm=FakeLLMClient(),
        sandbox=FakeSandboxRuntime(
            SandboxConfig(task_id="t-digest-tester", workspace_path=workspace)
        ),
        workspace_root=workspace,
    )

    messages = agent._build_initial_messages(
        TesterInput(task_id="t-digest-tester", spec=spec, changes=changes)
    )
    user_prompt = messages[1].content

    # Structured digest elements must be present
    assert "## Specification Summary\nVerification of app" in user_prompt
    assert "## Acceptance Criteria\n- Criterion A: test passes" in user_prompt

    # Redundant full spec markdown dump must be excluded
    assert "## Specification Content (spec.md)" not in user_prompt
    assert "Redundant spec markdown text." not in user_prompt
