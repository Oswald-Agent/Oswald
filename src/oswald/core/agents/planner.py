"""Planner Agent that scopes tasks, investigates workspace, and produces PlanSpec specifications."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from oswald.config import Settings
from oswald.core.agents.coder import (
    BudgetGuard,
    EventSink,
    NoOpBudgetGuard,
    NoOpEventSink,
    RunEvent,
)
from oswald.core.agents.compaction import compact_tool_history
from oswald.core.agents.telemetry import agent_scope, complete, tool_call
from oswald.core.artifacts.models import PlanSpec, TaskItem
from oswald.core.codeindex.indexer import CodeIndexer
from oswald.core.llm.exceptions import LLMError
from oswald.core.llm.protocol import LLMClient
from oswald.core.llm.types import (
    LLMRequest,
    Message,
    Role,
    StopReason,
    ToolCall,
)
from oswald.core.tools.planner import (
    PLANNER_TOOL_DEFINITIONS,
    list_files,
    read_file_range,
    read_symbol,
    search_code,
)

logger = structlog.get_logger(component="agent.planner")
_DEFAULT_PROMPT_PATH = Path(__file__).parent / "prompts" / "planner_system.md"
_JSON_BLOCK_REGEX = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class PlannerInput(BaseModel):
    """Input payload delivered to PlannerAgent for scoping a task."""

    __test__ = False

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    prompt: str = Field(
        min_length=1, description="Natural language problem statement or feature request"
    )
    attempt: int = Field(default=1, ge=1)
    skeleton: str = Field(default="", description="Optional workspace structural skeleton")
    context: str = Field(default="", description="Optional additional context or requirements")


class PlannerAgent:
    """Coordinates workspace inspection and LLM planning turns to produce a validated PlanSpec."""

    __test__ = False

    def __init__(
        self,
        llm: LLMClient,
        workspace_root: Path,
        budget_guard: BudgetGuard | None = None,
        event_sink: EventSink | None = None,
        indexer: CodeIndexer | None = None,
        model: str | None = None,
        max_turns: int = 15,
        max_schema_retries: int = 2,
        prompt_path: Path | None = None,
    ) -> None:
        self.llm = llm
        self.workspace_root = Path(workspace_root).resolve()
        self.budget_guard: BudgetGuard = budget_guard or NoOpBudgetGuard()
        self.event_sink: EventSink = event_sink if event_sink is not None else NoOpEventSink()
        self.indexer = indexer
        self.model = model or Settings().planner_model
        self.max_turns = max_turns
        self.max_schema_retries = max_schema_retries
        self.prompt_path = prompt_path or _DEFAULT_PROMPT_PATH

    def _load_system_prompt(self) -> str:
        if self.prompt_path.is_file():
            return self.prompt_path.read_text(encoding="utf-8")
        return (
            "You are the Planner Agent. Inspect workspace with read-only tools and produce "
            "a structured PlanSpec specification JSON."
        )

    def _build_initial_messages(self, input_data: PlannerInput) -> list[Message]:
        system_content = self._load_system_prompt()
        user_parts = [
            f"# Planning Request for Task {input_data.task_id} (Attempt {input_data.attempt})",
            f"## Request / Problem Statement\n{input_data.prompt}",
        ]
        if input_data.context:
            user_parts.append(f"## Additional Context\n{input_data.context}")
        if input_data.skeleton:
            user_parts.append(f"## Workspace Skeleton\n```\n{input_data.skeleton}\n```")

        user_parts.append(
            "\nInspect the workspace using read-only tools if necessary. When ready, output "
            "the final specification as a valid JSON block conforming to the PlanSpec schema."
        )

        return [
            Message(role=Role.SYSTEM, content=system_content),
            Message(role=Role.USER, content="\n\n".join(user_parts)),
        ]

    def _execute_read_only_tool(self, tool_call: ToolCall) -> str:
        """Execute read-only inspection tools strictly within workspace bounds."""
        name = tool_call.name
        args = tool_call.arguments

        try:
            if name == "read_file_range":
                return read_file_range(
                    workspace_root=self.workspace_root,
                    file_path=str(args.get("file_path", "")),
                    start_line=int(args.get("start_line", 1)),
                    end_line=int(args["end_line"])
                    if "end_line" in args and args["end_line"] is not None
                    else None,
                )
            if name == "search_code":
                return search_code(
                    workspace_root=self.workspace_root,
                    query=str(args.get("query", "")),
                    file_pattern=str(args["file_pattern"]) if args.get("file_pattern") else None,
                    is_regex=bool(args.get("is_regex", False)),
                )
            if name == "list_files":
                return list_files(
                    workspace_root=self.workspace_root,
                    directory=str(args.get("directory", ".")),
                    recursive=bool(args.get("recursive", True)),
                )
            if name == "read_symbol":
                return read_symbol(
                    workspace_root=self.workspace_root,
                    symbol_name=str(args.get("symbol_name", "")),
                    indexer=self.indexer,
                )
            return f"Error: Unknown tool '{name}'. Available tools: read_file_range, search_code, list_files, read_symbol."
        except Exception as exc:
            logger.warning("planner.tool_execution_failed", tool=name, error=str(exc))
            return f"Tool Execution Error ({name}): {exc}"

    @staticmethod
    def _parse_completion_draft(text: str) -> dict[str, Any] | None:
        """Extract and parse completion JSON draft from model output."""
        clean = text.strip()
        candidates: list[str] = []

        # 1. Blocks within markdown json tags
        for match in _JSON_BLOCK_REGEX.finditer(clean):
            candidates.append(match.group(1))

        # 2. Outer brace slice fallback
        first_brace = clean.find("{")
        last_brace = clean.rfind("}")
        if first_brace != -1 and last_brace > first_brace:
            candidates.append(clean[first_brace : last_brace + 1])

        # 3. Raw text candidate
        candidates.append(clean)

        for candidate in candidates:
            try:
                value = json.loads(candidate, strict=False)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(value, dict) and "summary" in value and "content" in value:
                return value
        return None

    def _build_planspec(self, task_id: str, draft: dict[str, Any]) -> PlanSpec:
        """Construct and validate a PlanSpec from the raw completion draft."""
        summary = str(draft.get("summary", "")).strip() or "Specification planned"
        content = (
            str(draft.get("content", "")).strip() or f"# Specification for {task_id}\n\n{summary}"
        )
        target_files = [str(f).strip() for f in draft.get("target_files", []) if str(f).strip()]
        acceptance_criteria = [
            str(c).strip() for c in draft.get("acceptance_criteria", []) if str(c).strip()
        ]
        has_open_questions = bool(draft.get("has_open_questions", False))
        open_questions = [str(q).strip() for q in draft.get("open_questions", []) if str(q).strip()]

        raw_tasks = draft.get("tasks", [])
        tasks: list[TaskItem] = []
        if isinstance(raw_tasks, list):
            for i, item in enumerate(raw_tasks, start=1):
                if isinstance(item, dict):
                    step = int(item.get("step", i))
                    desc = str(item.get("description", f"Step {step}")).strip()
                    target = str(item.get("target_file", "")).strip()
                    act = str(item.get("action", "modify")).lower()
                    if act not in ("create", "modify", "delete"):
                        act = "modify"
                    details = str(item.get("details", "")).strip()
                    if target:
                        # Ensure target is registered in target_files whitelist
                        if target not in target_files:
                            target_files.append(target)
                        tasks.append(
                            TaskItem(
                                step=step,
                                description=desc or f"Execute step {step}",
                                target_file=target,
                                action=act,  # type: ignore[arg-type]
                                details=details,
                            )
                        )

        return PlanSpec(
            task_id=task_id,
            summary=summary,
            target_files=target_files,
            tasks=tasks,
            acceptance_criteria=acceptance_criteria,
            has_open_questions=has_open_questions,
            open_questions=open_questions,
            content=content,
        )

    async def run(self, input_data: PlannerInput) -> PlanSpec:
        """Run with bounded progress logging and exception-safe timer cleanup."""
        async with agent_scope("planner", input_data.task_id, input_data.attempt) as progress:
            result = await self._run(input_data)
            progress.result(result)
            return result

    async def _run(self, input_data: PlannerInput) -> PlanSpec:
        """Run the Planner agent turn loop to investigate workspace and return a PlanSpec."""
        await self.event_sink.emit(
            RunEvent(
                task_id=input_data.task_id,
                agent="planner",
                event_type="planner.started",
                attempt=input_data.attempt,
                payload={"prompt": input_data.prompt},
            )
        )

        messages = self._build_initial_messages(input_data)
        schema_retries = 0
        final_draft: dict[str, Any] | None = None
        tool_call_history: dict[str, tuple[int, str, str]] = {}

        for turn in range(1, self.max_turns + 1):
            self.budget_guard.check(input_data.task_id)
            messages = compact_tool_history(messages, tool_call_history, turn)

            try:
                response = await complete(
                    self.llm,
                    LLMRequest(
                        model=self.model,
                        messages=messages,
                        tools=PLANNER_TOOL_DEFINITIONS,
                        temperature=0.2,
                    ),
                    turn=turn,
                )
            except LLMError as err:
                logger.error("planner.llm_error", turn=turn, error=str(err))
                await self.event_sink.emit(
                    RunEvent(
                        task_id=input_data.task_id,
                        agent="planner",
                        event_type="planner.error",
                        attempt=input_data.attempt,
                        payload={"error": str(err), "turn": turn},
                    )
                )
                raise

            tool_calls = response.tool_calls or []
            if tool_calls:
                messages.append(
                    Message(
                        role=Role.ASSISTANT,
                        content=response.content or "",
                        tool_calls=tool_calls,
                    )
                )

                for tc in tool_calls:
                    await self.event_sink.emit(
                        RunEvent(
                            task_id=input_data.task_id,
                            agent="planner",
                            event_type="planner.tool_call",
                            attempt=input_data.attempt,
                            payload={"tool": tc.name, "arguments": tc.arguments},
                        )
                    )
                    with tool_call(tc.name) as tool:
                        tool_output = self._execute_read_only_tool(tc)
                        tool.success = not tool_output.startswith(
                            ("Error:", "Tool Execution Error")
                        )
                    target_arg = (
                        tc.arguments.get("file_path")
                        or tc.arguments.get("query")
                        or tc.arguments.get("directory")
                        or tc.arguments.get("symbol_name")
                        or ""
                    )
                    tool_call_history[tc.id] = (turn, tc.name, str(target_arg))
                    messages.append(
                        Message(
                            role=Role.TOOL,
                            content=tool_output,
                            tool_call_id=tc.id,
                        )
                    )
                continue

            # Assistant emitted a text response without tool calls
            messages.append(Message(role=Role.ASSISTANT, content=response.content))
            final_draft = self._parse_completion_draft(response.content)

            if final_draft is not None:
                try:
                    spec = self._build_planspec(input_data.task_id, final_draft)
                    await self.event_sink.emit(
                        RunEvent(
                            task_id=input_data.task_id,
                            agent="planner",
                            event_type="planner.completed",
                            attempt=input_data.attempt,
                            payload={
                                "summary": spec.summary,
                                "target_files": spec.target_files,
                                "has_open_questions": spec.has_open_questions,
                            },
                        )
                    )
                    return spec
                except Exception as exc:
                    logger.warning("planner.spec_validation_failed", error=str(exc))
                    if schema_retries < self.max_schema_retries:
                        schema_retries += 1
                        messages.append(
                            Message(
                                role=Role.USER,
                                content=(
                                    f"PlanSpec validation failed: {exc}. Please return a valid JSON object "
                                    "matching the PlanSpec schema."
                                ),
                            )
                        )
                        continue

            if response.stop_reason in (StopReason.MAX_TOKENS, StopReason.CONTENT_FILTER):
                logger.warning("planner.premature_termination", stop_reason=response.stop_reason)
                break

            if schema_retries < self.max_schema_retries:
                schema_retries += 1
                messages.append(
                    Message(
                        role=Role.USER,
                        content=(
                            "Your response could not be parsed as a valid PlanSpec specification. "
                            "Please output a valid JSON code block containing 'summary', 'target_files', "
                            "'tasks', 'acceptance_criteria', 'has_open_questions', 'open_questions', and 'content'."
                        ),
                    )
                )
                continue
            break

        # Fallback PlanSpec if model failed to provide valid schema after retries
        logger.warning("planner.fallback_spec_generated", task_id=input_data.task_id)
        fallback = PlanSpec(
            task_id=input_data.task_id,
            summary=f"Plan generated for: {input_data.prompt[:100]}",
            target_files=[],
            tasks=[],
            acceptance_criteria=[],
            has_open_questions=True,
            open_questions=[
                "Model output could not be parsed into a structured PlanSpec; review required."
            ],
            content=f"# Specification for {input_data.task_id}\n\n## Request\n{input_data.prompt}\n",
        )
        await self.event_sink.emit(
            RunEvent(
                task_id=input_data.task_id,
                agent="planner",
                event_type="planner.completed",
                attempt=input_data.attempt,
                payload={"summary": fallback.summary, "fallback": True},
            )
        )
        return fallback
