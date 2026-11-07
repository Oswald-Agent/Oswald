"""Conversation history compaction to prevent unbounded context growth."""

from __future__ import annotations

from oswald.core.llm.types import Message, Role

FILE_READ_TOOLS: frozenset[str] = frozenset(
    {
        "read_file_range",
        "search_code",
        "list_files",
        "read_symbol",
        "get_file_summary",
    }
)


def compact_tool_history(
    messages: list[Message],
    tool_call_turns: dict[str, tuple[int, str, str]],
    current_turn: int,
    retention_turns: int = 3,
) -> list[Message]:
    """Tombstone file-reading tool outputs older than retention_turns turns.

    Args:
        messages: Conversation message list to inspect and compact.
        tool_call_turns: Mapping from tool_call_id to (turn_number, tool_name, target_arg).
        current_turn: Current turn number in the agent interaction loop.
        retention_turns: Number of recent turns to retain before tombstoning (default 3).

    Returns:
        List of messages where historical file-reading outputs older than retention_turns
        turns are replaced with concise one-line tombstones.
    """
    compacted: list[Message] = []
    for msg in messages:
        if msg.role == Role.TOOL and msg.tool_call_id and msg.tool_call_id in tool_call_turns:
            turn_num, tool_name, target_arg = tool_call_turns[msg.tool_call_id]
            if (
                tool_name in FILE_READ_TOOLS
                and (current_turn - turn_num) >= retention_turns
                and not msg.content.startswith("[Historical ")
            ):
                lines = len(msg.content.splitlines())
                tombstone = (
                    f"[Historical {tool_name} output for '{target_arg}' "
                    f"({lines} lines) tombstoned to conserve context]"
                )
                compacted.append(msg.model_copy(update={"content": tombstone}))
                continue
        compacted.append(msg)
    return compacted
