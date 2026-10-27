"""Coder Agent implementing PlanSpec via isolated workspace mutations and static checks."""

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import structlog
from pydantic import BaseModel, ConfigDict, Field

from oswald.core.agents.telemetry import agent_scope, complete, tool_call
from oswald.core.artifacts.models import CodeChanges, Finding, PlanSpec
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
from oswald.core.sandbox.protocol import SandboxRuntime
from oswald.core.tools.coder import (
    CODER_TOOL_DEFINITIONS,
    check_declared_imports,
    edit_file,
    is_protected_path,
    is_test_path,
    list_files,
    read_file_range,
    read_symbol,
    run_static_check,
    search_code,
)
from oswald.core.tools.coder.checks import ALLOWLISTED_COMMANDS
from oswald.core.vcs.workspace import WorkspaceDiff

logger = structlog.get_logger(component="agent.coder")


class BudgetExceededError(RuntimeError):
    """Raised when token or compute budget ceiling is exceeded."""


@runtime_checkable
class BudgetGuard(Protocol):
    """Protocol enforcing resource and token ceilings during agent execution."""

    def check(self, task_id: str, est_tokens: int = 0) -> None:
        """Verify task has remaining budget; raises BudgetExceededError if exhausted."""
        ...


class NoOpBudgetGuard:
    """Default pass-through budget guard for testing and unconstrained runs."""

    def check(self, task_id: str, est_tokens: int = 0) -> None:
        pass


class RunEvent(BaseModel):
    """Structured telemetry event recording agent lifecycle and tool invocations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    agent: str = "coder"
    event_type: str
    attempt: int = 1
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


@runtime_checkable
class EventSink(Protocol):
    """Sink for recording agent runtime telemetry events."""

    async def emit(self, event: RunEvent) -> None: ...


class NoOpEventSink:
    """Default sink: emit structured progress logs without retaining raw events."""

    async def emit(self, event: RunEvent) -> None:
        pass


class InMemoryEventSink:
    """In-memory event sink for testing and local trace inspection."""

    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    async def emit(self, event: RunEvent) -> None:
        self.events.append(event)


class CoderInput(BaseModel):
    """Input payload delivered to CoderAgent for an implementation turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    attempt: int = Field(default=1, ge=1)
    spec: PlanSpec
    skeleton: str = ""
    findings: list[Finding] = Field(default_factory=list)
    baseline_context: str = ""


_DEFAULT_PROMPT_PATH = Path(__file__).parent / "prompts" / "coder_system.md"
_JSON_BLOCK_REGEX = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class CoderAgent:
    """Coordinates LLM turns, tool executions, and diff assembly for the Coder role."""

    def __init__(
        self,
        llm: LLMClient,
        sandbox: SandboxRuntime,
        workspace_diff: WorkspaceDiff,
        workspace_root: Path,
        budget_guard: BudgetGuard | None = None,
        event_sink: EventSink | None = None,
        indexer: CodeIndexer | None = None,
        model: str = "gemini/gemini-2.0-flash",
        max_turns: int = 25,
        max_schema_retries: int = 2,
        prompt_path: Path | None = None,
    ) -> None:
        self.llm = llm
        self.sandbox = sandbox
        self.workspace_diff = workspace_diff
        self.workspace_root = Path(workspace_root).resolve()
        self.budget_guard: BudgetGuard = budget_guard or NoOpBudgetGuard()
        self.event_sink: EventSink = event_sink if event_sink is not None else NoOpEventSink()
        self.indexer = indexer
        self.model = model
        self.max_turns = max_turns
        self.max_schema_retries = max_schema_retries
        self.prompt_path = prompt_path or _DEFAULT_PROMPT_PATH
        self._unavailable_static_tools: dict[str, str] = {}

    def _load_system_prompt(self) -> str:
        if self.prompt_path.is_file():
            return self.prompt_path.read_text(encoding="utf-8")
        return "You are the Coder Agent. Implement the specification in target_files."

    def _build_initial_messages(self, input_data: CoderInput) -> list[Message]:
        system_content = self._load_system_prompt()
        source_targets = [path for path in input_data.spec.target_files if not is_test_path(path)]

        user_parts = [
            f"# Implementation Request: Task {input_data.task_id} (Attempt {input_data.attempt})",
            f"## Summary\n{input_data.spec.summary}",
            "## Stage Assignment\nImplement production changes only. The shared plan may include test tasks; "
            "Tester authors and runs those after your stage. Do not attempt test-file edits or escalate "
            "solely because test authoring remains. Record test requirements in test_attention.",
            f"## Authorized Target Files (Strict Whitelist)\n{json.dumps(source_targets, indent=2)}",
        ]

        if input_data.spec.tasks:
            tasks_md = "\n".join(
                f"{t.step}. [{t.action.upper()}] `{t.target_file}`: {t.description} ({t.details})"
                for t in input_data.spec.tasks
                if not is_test_path(t.target_file)
            )
            user_parts.append(f"## Planned Tasks\n{tasks_md}")

        if input_data.spec.acceptance_criteria:
            crit_md = "\n".join(f"- {c}" for c in input_data.spec.acceptance_criteria)
            user_parts.append(f"## Acceptance Criteria\n{crit_md}")

        if input_data.skeleton.strip():
            user_parts.append(f"## Scoped Repository Skeleton\n```\n{input_data.skeleton}\n```")

        if input_data.baseline_context.strip():
            user_parts.append(f"## Baseline Context\n{input_data.baseline_context}")

        if input_data.findings:
            findings_lines = []
            for idx, f in enumerate(input_data.findings, start=1):
                findings_lines.append(
                    f"- **Finding {idx}** [{f.source.upper()} | {f.severity.upper()}] "
                    f"Location: `{f.location or 'N/A'}` | Test: `{f.test_id or 'N/A'}`\n"
                    f"  *Problem*: {f.problem}\n"
                    f"  *Why it matters*: {f.why_it_matters}\n"
                    f"  *Expected correction*: {f.expected_correction}\n"
                    f"  *Evidence*: `{f.evidence or 'None'}`"
                )
            user_parts.append(
                "## Fix Loop Findings (from Tester/Reviewer)\n" + "\n".join(findings_lines)
            )

        user_parts.append(f"## Full Specification (spec.md)\n{input_data.spec.content}")

        return [
            Message(role=Role.SYSTEM, content=system_content),
            Message(role=Role.USER, content="\n\n".join(user_parts)),
        ]

    async def _execute_tool_call(
        self,
        call: ToolCall,
        target_files: list[str],
        task_id: str,
    ) -> tuple[str, bool]:
        """Dispatch a single tool call and return (output_text, is_success)."""
        args = call.arguments
        name = call.name

        try:
            if name == "read_file_range":
                file_path = str(args.get("file_path") or "")
                start = int(args.get("start_line") or 1)
                end_val = args.get("end_line")
                end = int(end_val) if end_val is not None else None
                out = read_file_range(self.workspace_root, file_path, start, end)
                return out, not out.startswith("Error:")

            elif name == "search_code":
                query = str(args.get("query") or "")
                file_pat = args.get("file_pattern")
                is_re = bool(args.get("is_regex", False))
                out = search_code(self.workspace_root, query, file_pat, is_re)
                return out, not out.startswith("Error:")

            elif name == "list_files":
                directory = str(args.get("directory") or ".")
                recursive = bool(args.get("recursive", True))
                out = list_files(self.workspace_root, directory, recursive)
                return out, not out.startswith("Error:")

            elif name == "read_symbol":
                symbol = str(args.get("symbol_name") or "")
                out = read_symbol(self.workspace_root, symbol, self.indexer)
                return out, not out.startswith("Error:")

            elif name == "edit_file":
                file_path = str(args.get("file_path") or "")
                old_val = args.get("old_str")
                old_str = "" if old_val is None else str(old_val)
                new_val = args.get("new_str")
                new_str = "" if new_val is None else str(new_val)
                res = edit_file(
                    self.workspace_root,
                    file_path,
                    old_str,
                    new_str,
                    target_files,
                )
                if res.success and self.indexer is not None:
                    # Update symbol index asynchronously
                    await asyncio.to_thread(self.indexer.index_file, res.file_path)
                return res.message, res.success

            elif name == "run_static_check":
                check_name = str(args.get("check_name") or "").strip().lower()
                command = ALLOWLISTED_COMMANDS.get(check_name)
                executable = command[0] if command else ""
                if executable in self._unavailable_static_tools:
                    return (
                        f"Check: {check_name} | SKIPPED (not passed). "
                        f"{self._unavailable_static_tools[executable]} "
                        "Do not retry this checker. If implementation and available checks are "
                        "complete, return completion JSON now and hand off to Tester.",
                        True,
                    )
                check_res = await run_static_check(self.sandbox, check_name)
                if check_res.skipped:
                    self._unavailable_static_tools[executable] = check_res.output
                    logger.warning(
                        "coder.static_check_skipped", check=check_name, reason=check_res.output
                    )
                    return (
                        f"Check: {check_name} | SKIPPED (not passed). {check_res.output} "
                        "Do not retry this checker or install dependencies. Run any remaining "
                        "available checks, then return completion JSON and hand off to Tester.",
                        True,
                    )
                return (
                    f"Check: {check_res.check_name} | Passed: {check_res.passed} (exit {check_res.exit_code})\n{check_res.output}",
                    check_res.passed,
                )

            elif name == "check_declared_imports":
                changed = await self.workspace_diff.changed_files(task_id)
                warnings = check_declared_imports(self.workspace_root, changed)
                if warnings:
                    return "\n".join(warnings), False
                return "All imports verified against declared packages.", True

            else:
                return f"Error: Unknown tool '{name}'", False

        except Exception as exc:
            return f"ToolExecutionError in '{name}': {exc}", False

    def _parse_completion_draft(self, text: str) -> dict[str, Any] | None:
        """Extract and parse completion JSON draft from model output."""
        clean = text.strip()
        candidates: list[str] = []

        for match in _JSON_BLOCK_REGEX.finditer(clean):
            candidates.append(match.group(1))

        first_brace = clean.find("{")
        last_brace = clean.rfind("}")
        if first_brace != -1 and last_brace > first_brace:
            candidates.append(clean[first_brace : last_brace + 1])

        candidates.append(clean)

        for cand in candidates:
            try:
                parsed = json.loads(cand, strict=False)
                if isinstance(parsed, dict) and "status" in parsed and "summary" in parsed:
                    return parsed
            except (json.JSONDecodeError, TypeError):
                continue
        return None

    async def run(self, input_data: CoderInput) -> CodeChanges:
        """Run with bounded progress logging and exception-safe timer cleanup."""
        async with agent_scope("coder", input_data.task_id, input_data.attempt) as progress:
            result = await self._run(input_data)
            progress.result(result)
            return result

    async def _run(self, input_data: CoderInput) -> CodeChanges:
        """Run the Coder agent turn loop to implement spec changes."""
        await self.event_sink.emit(
            RunEvent(
                task_id=input_data.task_id,
                event_type="coder.started",
                attempt=input_data.attempt,
                payload={"target_files": input_data.spec.target_files},
            )
        )

        messages = self._build_initial_messages(input_data)
        schema_retries = 0
        final_draft: dict[str, Any] | None = None

        consecutive_tool_failures: dict[str, int] = {}

        for turn in range(1, self.max_turns + 1):
            self.budget_guard.check(input_data.task_id)

            try:
                response = await complete(
                    self.llm,
                    LLMRequest(
                        model=self.model,
                        messages=messages,
                        tools=CODER_TOOL_DEFINITIONS,
                        temperature=0.0,
                    ),
                    turn=turn,
                )
            except LLMError as exc:
                final_draft = {
                    "status": "ESCALATED",
                    "summary": f"LLM error encountered: {exc}",
                    "content": f"# LLM Error\nExecution halted due to LLM failure: {exc}",
                    "escalation": f"LLM call raised {type(exc).__name__}: {exc}",
                }
                break

            await self.event_sink.emit(
                RunEvent(
                    task_id=input_data.task_id,
                    event_type="coder.llm_completion",
                    attempt=input_data.attempt,
                    payload={
                        "turn": turn,
                        "stop_reason": str(response.stop_reason),
                        "tool_calls": len(response.tool_calls),
                        "tokens": response.usage.model_dump(),
                    },
                )
            )

            if response.tool_calls:
                # Append assistant tool invocation message
                messages.append(
                    Message(
                        role=Role.ASSISTANT,
                        content=response.content,
                        tool_calls=response.tool_calls,
                    )
                )

                circuit_broken = False
                for tc in response.tool_calls:
                    with tool_call(tc.name) as tool:
                        output_text, success = await self._execute_tool_call(
                            tc, input_data.spec.target_files, input_data.task_id
                        )
                        tool.success = success

                    # Circuit breaker check: detect 3 repeated identical failures
                    call_sig = f"{tc.name}:{json.dumps(tc.arguments, sort_keys=True)}"
                    if not success:
                        consecutive_tool_failures[call_sig] = (
                            consecutive_tool_failures.get(call_sig, 0) + 1
                        )
                        if consecutive_tool_failures[call_sig] >= 3:
                            circuit_broken = True
                    else:
                        consecutive_tool_failures.pop(call_sig, None)

                    await self.event_sink.emit(
                        RunEvent(
                            task_id=input_data.task_id,
                            event_type="coder.tool_executed",
                            attempt=input_data.attempt,
                            payload={"tool": tc.name, "success": success},
                        )
                    )

                    messages.append(
                        Message(
                            role=Role.TOOL,
                            content=output_text,
                            tool_call_id=tc.id,
                        )
                    )

                if circuit_broken:
                    final_draft = {
                        "status": "ESCALATED",
                        "summary": "Circuit breaker tripped on repeated tool failures",
                        "content": "# Circuit Breaker Escalation\nRepeated tool execution failures occurred.",
                        "escalation": "Circuit breaker: same tool call failed 3 consecutive times without progress.",
                    }
                    break

                continue

            # No tool calls: check for completion contract
            messages.append(Message(role=Role.ASSISTANT, content=response.content))
            draft = self._parse_completion_draft(response.content)

            if draft is not None:
                final_draft = draft
                break

            if response.stop_reason in (StopReason.MAX_TOKENS, StopReason.CONTENT_FILTER):
                final_draft = {
                    "status": "ESCALATED",
                    "summary": f"LLM terminated with stop reason {response.stop_reason}",
                    "content": "# LLM Execution Error\nGeneration terminated prematurely.",
                    "escalation": f"LLM generation halted due to {response.stop_reason}.",
                }
                break

            if schema_retries < self.max_schema_retries:
                schema_retries += 1
                messages.append(
                    Message(
                        role=Role.USER,
                        content=(
                            "Your response did not include a valid completion JSON block. "
                            "Please provide your final status as a JSON block with 'status' ('COMPLETED' or 'ESCALATED'), "
                            "'summary', and 'content'."
                        ),
                    )
                )
                continue

            # Exhausted schema retries
            final_draft = {
                "status": "ESCALATED",
                "summary": "Invalid completion schema received from LLM",
                "content": "# Schema Error\nUnable to parse final JSON artifact.",
                "escalation": "Coder agent produced unparseable completion output after schema retries.",
            }
            break

        if final_draft is None:
            final_draft = {
                "status": "ESCALATED",
                "summary": f"Exceeded maximum turns ceiling ({self.max_turns})",
                "content": "# Turn Ceiling Escalation\nCoder loop exhausted turn budget.",
                "escalation": f"Coder agent reached maximum turn limit ({self.max_turns}) without completion.",
            }

        # Post-loop mechanical assembly and boundary verification
        diff = await self.workspace_diff.diff_since_base(input_data.task_id)
        changed_files = await self.workspace_diff.changed_files(input_data.task_id)

        # Handle ESCALATED draft
        if final_draft.get("status") == "ESCALATED":
            changes = CodeChanges(
                task_id=input_data.task_id,
                status="ESCALATED",
                summary=final_draft.get("summary", "Task escalated by Coder"),
                modified_files=[],
                diff="",
                content=final_draft.get("content", "# Escalation"),
                escalation=final_draft.get("escalation", "Escalated by Coder agent"),
                known_limitations=list(self._unavailable_static_tools.values()),
            )
            await self.event_sink.emit(
                RunEvent(
                    task_id=input_data.task_id,
                    event_type="coder.completed",
                    attempt=input_data.attempt,
                    payload={"status": "ESCALATED", "summary": changes.summary},
                )
            )
            return changes

        # COMPLETED path: verify scope whitelist against git diff
        whitelist_set = {
            f.strip().removeprefix("./").removeprefix("/") for f in input_data.spec.target_files
        }
        clean_changed = [f.strip().removeprefix("./").removeprefix("/") for f in changed_files]

        # Ignore test files in worktree diff (authored by Tester in previous attempts);
        # Coder only implements and validates production source files.
        source_changed = [f for f in clean_changed if not is_test_path(f)]
        scope_violations = [f for f in source_changed if f not in whitelist_set]

        if scope_violations:
            changes = CodeChanges(
                task_id=input_data.task_id,
                status="ESCALATED",
                summary="Scope violation detected in workspace diff",
                modified_files=[],
                diff="",
                content="# Scope Violation Detected\nUnapproved files were touched during execution.",
                escalation=(
                    f"Scope violation: Git diff reveals modifications to files outside the target_files whitelist: "
                    f"{scope_violations}. Permitted whitelist: {sorted(whitelist_set)}."
                ),
            )
            await self.event_sink.emit(
                RunEvent(
                    task_id=input_data.task_id,
                    event_type="coder.completed",
                    attempt=input_data.attempt,
                    payload={"status": "ESCALATED", "scope_violations": scope_violations},
                )
            )
            return changes

        # Detect touched protected paths
        protected_touched = [f for f in source_changed if is_protected_path(f)]

        # Run undeclared imports check
        undeclared_warnings = check_declared_imports(self.workspace_root, source_changed)
        deviations = list(final_draft.get("deviations", []))
        if undeclared_warnings:
            deviations.extend([f"Import warning: {w}" for w in undeclared_warnings])

        # If completed but git diff has no source changes
        if not source_changed or not diff.strip():
            changes = CodeChanges(
                task_id=input_data.task_id,
                status="ESCALATED",
                summary="No changes were detected in the workspace diff",
                modified_files=[],
                diff="",
                content="# No Changes Detected\nImplementation did not produce git modifications.",
                escalation="Coder marked work COMPLETED but no workspace modifications or diffs were captured.",
            )
            await self.event_sink.emit(
                RunEvent(
                    task_id=input_data.task_id,
                    event_type="coder.completed",
                    attempt=input_data.attempt,
                    payload={"status": "ESCALATED", "reason": "empty_diff"},
                )
            )
            return changes

        changes = CodeChanges(
            task_id=input_data.task_id,
            status="COMPLETED",
            summary=final_draft.get("summary", "Changes implemented successfully"),
            modified_files=source_changed,
            diff=diff,
            content=final_draft.get("content", "# Implementation Notes"),
            deviations=deviations,
            known_limitations=list(final_draft.get("known_limitations", []))
            + list(self._unavailable_static_tools.values()),
            test_attention=list(final_draft.get("test_attention", [])),
            dependency_changes=protected_touched,
        )

        await self.event_sink.emit(
            RunEvent(
                task_id=input_data.task_id,
                event_type="coder.completed",
                attempt=input_data.attempt,
                payload={
                    "status": "COMPLETED",
                    "modified_files": source_changed,
                    "dependency_changes": protected_touched,
                },
            )
        )
        return changes
