"""Tester Agent that authors tests and validates changes in an isolated sandbox."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from oswald.core.agents.coder import (
    BudgetGuard,
    EventSink,
    NoOpBudgetGuard,
    NoOpEventSink,
    RunEvent,
)
from oswald.core.agents.telemetry import agent_scope, complete, tool_call
from oswald.core.artifacts.models import CodeChanges, PlanSpec, TestResults
from oswald.core.codeindex.indexer import CodeIndexer
from oswald.core.llm.exceptions import LLMError
from oswald.core.llm.protocol import LLMClient
from oswald.core.llm.types import LLMRequest, Message, Role, StopReason, ToolCall
from oswald.core.sandbox.protocol import SandboxRuntime
from oswald.core.tools.coder import (
    list_files,
    read_file_range,
    read_symbol,
    search_code,
)
from oswald.core.tools.tester import (
    TESTER_TOOL_DEFINITIONS,
    CommandResult,
    WorkspaceSnapshot,
    execute_test_command,
    is_test_related_path,
    write_test_file,
)

logger = structlog.get_logger(component="agent.tester")
_PROMPT_PATH = Path(__file__).parent / "prompts" / "tester_system.md"
_JSON_BLOCK_REGEX = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class TesterInput(BaseModel):
    """Immutable upstream artifacts and optional compact workspace context for testing."""

    __test__ = False

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    attempt: int = Field(default=1, ge=1)
    spec: PlanSpec
    changes: CodeChanges
    skeleton: str = ""
    baseline_context: str = ""


class TesterAgent:
    """Coordinates stack discovery, test authoring, execution, and result reporting."""

    __test__ = False

    def __init__(
        self,
        llm: LLMClient,
        sandbox: SandboxRuntime,
        workspace_root: Path,
        budget_guard: BudgetGuard | None = None,
        event_sink: EventSink | None = None,
        indexer: CodeIndexer | None = None,
        model: str = "gemini/gemini-2.0-flash",
        max_turns: int = 100,
        max_schema_retries: int = 2,
        prompt_path: Path | None = None,
    ) -> None:
        self.llm = llm
        self.sandbox = sandbox
        self.workspace_root = Path(workspace_root).resolve()
        self.budget_guard = budget_guard or NoOpBudgetGuard()
        self.event_sink = event_sink if event_sink is not None else NoOpEventSink()
        self.indexer = indexer
        self.model = model
        self.max_turns = max_turns
        self.max_schema_retries = max_schema_retries
        self.prompt_path = prompt_path or _PROMPT_PATH

    def _load_system_prompt(self) -> str:
        if self.prompt_path.is_file():
            return self.prompt_path.read_text(encoding="utf-8")
        return (
            "You are the Tester Agent in OSWALD. Inspect workspace conventions, author tests "
            "for acceptance criteria, and execute test commands in the sandbox."
        )

    def _build_initial_messages(self, data: TesterInput) -> list[Message]:
        prompt = self._load_system_prompt()
        parts = [
            f"# Test Request: Task {data.task_id} (Attempt {data.attempt})",
            f"## Specification Summary\n{data.spec.summary}",
            f"## Specification Content (spec.md)\n{data.spec.content}",
        ]

        if data.spec.acceptance_criteria:
            parts.append(
                "## Acceptance Criteria\n"
                + "\n".join(f"- {item}" for item in data.spec.acceptance_criteria)
            )

        parts.extend(
            [
                f"## Code Changes Summary\n{data.changes.summary}",
                f"## Code Changes Notes (changes.md)\n{data.changes.content}",
                "## Modified Files (Strict Source Isolation)\n"
                + "\n".join(f"- `{item}`" for item in data.changes.modified_files),
                f"## Unified Diff\n```diff\n{data.changes.diff}\n```",
            ]
        )

        if data.changes.test_attention:
            parts.append(
                "## Required Test Attention\n"
                + "\n".join(f"- {item}" for item in data.changes.test_attention)
            )
        if data.skeleton.strip():
            parts.append(f"## Workspace Skeleton\n```\n{data.skeleton}\n```")
        if data.baseline_context.strip():
            parts.append(f"## Baseline Context\n{data.baseline_context}")

        return [
            Message(role=Role.SYSTEM, content=prompt),
            Message(role=Role.USER, content="\n\n".join(parts)),
        ]

    async def _execute_tool(
        self,
        call: ToolCall,
        forbidden_paths: set[str],
    ) -> tuple[str, bool, CommandResult | None]:
        args = call.arguments
        name = call.name
        try:
            if name == "read_file_range":
                output = read_file_range(
                    self.workspace_root,
                    str(args.get("file_path") or ""),
                    int(args.get("start_line") or 1),
                    int(args["end_line"]) if args.get("end_line") is not None else None,
                )
                return output, not output.startswith("Error:"), None

            elif name == "search_code":
                output = search_code(
                    self.workspace_root,
                    str(args.get("query") or ""),
                    args.get("file_pattern"),
                    bool(args.get("is_regex", False)),
                )
                return output, not output.startswith("Error:"), None

            elif name == "list_files":
                output = list_files(
                    self.workspace_root,
                    str(args.get("directory") or "."),
                    bool(args.get("recursive", True)),
                )
                return output, not output.startswith("Error:"), None

            elif name == "read_symbol":
                output = read_symbol(
                    self.workspace_root, str(args.get("symbol_name") or ""), self.indexer
                )
                return output, not output.startswith("Error:"), None

            elif name == "write_test_file":
                test_root = str(args.get("test_root") or "")
                overwrite = bool(args.get("overwrite", False))
                edit_result = write_test_file(
                    self.workspace_root,
                    str(args.get("file_path") or ""),
                    str(args.get("old_str") or ""),
                    str(args.get("new_str") or ""),
                    overwrite=overwrite,
                    test_root=test_root,
                    forbidden_paths=forbidden_paths,
                )
                if edit_result.success and self.indexer is not None:
                    # Keep code index synchronized with newly authored tests
                    await asyncio.to_thread(self.indexer.index_file, edit_result.file_path)

                msg = edit_result.message
                if edit_result.success and test_root:
                    msg = f"{msg}\nDeclared test root: {test_root}"
                return msg, edit_result.success, None

            elif name in {"run_test_command", "run_diagnostic_command"}:
                raw_command = args.get("command")
                command = [str(c) for c in raw_command] if isinstance(raw_command, list) else []
                if any(re.fullmatch(r"\|\|?|&&?|;|\d*[<>].*", token) for token in command) or (
                    command
                    and Path(command[0]).name
                    in {"sh", "bash", "dash", "zsh", "cmd", "powershell", "pwsh"}
                ):
                    return (
                        "PermissionDenied: commands are argument lists, not shell scripts. "
                        "Remove pipes, redirections such as 2>&1, and shell wrappers. "
                        "stdout and stderr are already captured. Run the test runner directly.",
                        False,
                        None,
                    )
                executable = Path(command[0]).name if command else ""
                pip_command = bool(re.fullmatch(r"pip(?:\d+(?:\.\d+)?)?", executable)) or (
                    executable.startswith(("python", "pypy"))
                    and "-m" in command
                    and "pip" in command
                )
                if pip_command and any(action in command for action in {"install", "uninstall"}):
                    return (
                        "PermissionDenied: do not install packages in the offline sandbox. "
                        "Build required dependencies into an image on the host, then resume with "
                        "continue --image <prepared-image>. Report missing dependencies as a blocker.",
                        False,
                        None,
                    )
                if (
                    len(command) > 1
                    and Path(command[0]).name in {"npm", "npm.cmd"}
                    and command[1] in {"install", "i", "add", "update", "uninstall", "remove"}
                ):
                    return (
                        "Tester cannot run npm commands that modify package manifests or lockfiles. "
                        "For an authorized cached install, use npm ci --offline --ignore-scripts "
                        "--no-audit --no-fund via run_diagnostic_command.",
                        False,
                        None,
                    )
                if command and Path(command[0]).name == "git":
                    return (
                        "Git inspection belongs to the host VCS layer. Use the supplied unified diff "
                        "and modified_files; Git metadata is not available inside the sandbox.",
                        False,
                        None,
                    )
                timeout = float(args.get("timeout_seconds") or 120.0)
                command_result = await execute_test_command(self.sandbox, command, timeout)

                python_inline = bool(
                    command
                    and Path(command[0]).name.startswith(("python", "pypy"))
                    and "-c" in command
                )
                npm_diagnostic = bool(
                    len(command) > 1
                    and Path(command[0]).name in {"npm", "npm.cmd"}
                    and (command[1] in {"ci", "start"} or command[1:3] == ["run", "build"])
                )
                if name == "run_diagnostic_command" or python_inline or npm_diagnostic:
                    return (
                        f"Exit {command_result.exit_code}\n{command_result.output}",
                        command_result.exit_code == 0 and not command_result.execution_error,
                        None,
                    )
                if (
                    command_result.exit_code == 127
                    or "No module named pytest" in command_result.output
                ):
                    return (
                        command_result.output + "\nRequested tool is unavailable. For a standalone "
                        "Python unittest suite, use ['python', '-B', '-m', 'unittest', 'discover', '-v']. "
                        "Do not replace an existing framework's tests with unittest discovery.",
                        False,
                        None,
                    )

                if (
                    re.search(r"\bRan\s+0\s+tests?\b", command_result.output)
                    or "ImportError: Start directory is not importable:" in command_result.output
                ):
                    return (
                        command_result.output + "\nNo tests ran: correct unittest discovery. "
                        "For tests/test_app.py use ['python', '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v'] "
                        "without '-t'. For a standalone app.py, prefer a single root test_app.py.",
                        False,
                        None,
                    )

                # Tool invocation succeeded if the command executed in the sandbox without runtime crash/timeout
                is_tool_success = not (
                    command_result.exit_code == 124
                    or "Sandbox execution error" in command_result.output
                    or "PermissionDenied" in command_result.output
                )
                return (
                    f"Exit {command_result.exit_code} ({command_result.duration_seconds:.2f}s)\n"
                    f"{command_result.output}",
                    is_tool_success,
                    command_result,
                )

            else:
                return f"Error: Unknown tool '{name}'", False, None

        except Exception as exc:
            logger.error("tester.tool_exception", tool=name, error=str(exc))
            return f"ToolExecutionError in '{name}': {exc}", False, None

    @staticmethod
    def _parse_completion_draft(text: str) -> dict[str, Any] | None:
        """Extract and parse completion JSON draft from model output with fallback tolerance."""
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
            if isinstance(value, dict) and "content" in value:
                status = str(value.get("status", "")).upper()
                if status in ("COMPLETED", "ESCALATED"):
                    value["status"] = status
                    return value
                # If status is omitted or unconventional, default based on explicit presence of content
                if "status" in value:
                    return value
        return None

    @staticmethod
    def _extract_metrics(commands: list[CommandResult]) -> tuple[int, int]:
        """Extract quantitative test metrics (tests_run, tests_failed) from latest test executions."""
        tests_run = 0
        tests_failed = 0

        for result in commands:
            text = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
            # Vitest prints file counts before test counts. Prefer the test summary.
            test_summary = re.search(r"^\s*Tests:?\s+([^\n]+)", text, re.MULTILINE)
            if test_summary:
                text = test_summary.group(1)

            # Standard-library unittest: counts are on a separate line from failures.
            unittest_match = re.search(r"\bRan\s+(\d+)\s+tests?\b", text)
            if unittest_match:
                tests_run += int(unittest_match.group(1))
                summary = re.search(r"FAILED \(([^)]+)\)", text)
                if summary:
                    tests_failed += sum(
                        int(n) for n in re.findall(r"(?:failures|errors)=(\d+)", summary.group(1))
                    )
                continue

            # 1. Pytest format (e.g. "= 2 passed, 1 failed in 0.12s =" or "= 1 failed in 0.05s =")
            passed_match = re.search(r"\b(\d+)\s+passed\b", text)
            failed_match = re.search(r"\b(\d+)\s+failed\b", text)
            error_match = re.search(r"\b(\d+)\s+errors?\b", text)

            if passed_match or failed_match or error_match:
                p = int(passed_match.group(1)) if passed_match else 0
                f = int(failed_match.group(1)) if failed_match else 0
                e = int(error_match.group(1)) if error_match else 0
                tests_run += p + f + e
                tests_failed += f + e
                continue

            # 2. Rust Cargo format (e.g. "test result: ok. 4 passed; 0 failed")
            cargo_match = re.search(
                r"test result: (?:ok|FAILED)\.\s+(\d+)\s+passed;\s+(\d+)\s+failed", text
            )
            if cargo_match:
                p = int(cargo_match.group(1))
                f = int(cargo_match.group(2))
                tests_run += p + f
                tests_failed += f
                continue

            # 3. Jest / Vitest format (e.g. "Tests: 1 failed, 2 passed, 3 total")
            jest_match = re.search(
                r"Tests:\s+(?:(\d+)\s+failed,\s*)?(\d+)\s+passed,\s*(\d+)\s+total", text
            )
            if jest_match:
                f = int(jest_match.group(1) or 0)
                tot = int(jest_match.group(3))
                tests_run += tot
                tests_failed += f
                continue

            # 4. Fallback based on exit code if tests ran but output was non-standard
            if result.exit_code != 0 and tests_run == 0:
                tests_failed += 1
                tests_run += 1

        return tests_run, tests_failed

    async def run(self, data: TesterInput) -> TestResults:
        """Run with bounded progress logging and exception-safe timer cleanup."""
        async with agent_scope("tester", data.task_id, data.attempt) as progress:
            result = await self._run(data)
            progress.result(result)
            return result

    async def _run(self, data: TesterInput) -> TestResults:
        """Run the Tester turn loop and return mechanically grounded test results."""

        await self.event_sink.emit(
            RunEvent(
                task_id=data.task_id,
                agent="tester",
                event_type="tester.started",
                attempt=data.attempt,
                payload={
                    "spec_target_files": data.spec.target_files,
                    "modified_files": data.changes.modified_files,
                },
            )
        )

        # Establish immutable source boundary; test files are excluded so pre-existing
        # or multi-attempt tests remain editable by the Tester agent.
        raw_forbidden = set(data.spec.target_files) | set(data.changes.modified_files)
        forbidden_paths = {p for p in raw_forbidden if not is_test_related_path(p)}

        # Capture pre-test workspace contents to detect command side-effects
        snapshot = WorkspaceSnapshot.capture(self.workspace_root)
        messages = self._build_initial_messages(data)

        commands: list[CommandResult] = []
        diagnostics: list[str] = []
        latest_commands: dict[str, CommandResult] = {}
        test_roots: set[str] = set()
        final_draft: dict[str, Any] | None = None
        schema_retries = 0
        tool_failures: dict[str, int] = {}
        verified_workspaces: dict[str, dict[str, str]] = {}
        report_requested = False

        for turn in range(1, self.max_turns + 1):
            self.budget_guard.check(data.task_id)
            report_only = turn == self.max_turns or report_requested
            if report_only:
                messages.append(
                    Message(
                        role=Role.USER,
                        content=(
                            "This turn is reserved for reporting. No tools are available. "
                            "Return completion JSON now using the test evidence already collected. "
                            "Report failed tests honestly; use ESCALATED if testing is blocked or "
                            "no valid test suite has run."
                        ),
                    )
                )

            try:
                response = await complete(
                    self.llm,
                    LLMRequest(
                        model=self.model,
                        messages=messages,
                        tools=[] if report_only else TESTER_TOOL_DEFINITIONS,
                        temperature=0.0,
                    ),
                    turn=turn,
                )
            except LLMError as exc:
                logger.error("tester.llm_error", task_id=data.task_id, turn=turn, error=str(exc))
                final_draft = {
                    "status": "ESCALATED",
                    "content": f"# Test Report\n\nExecution halted due to LLM failure: {exc}",
                }
                break

            await self.event_sink.emit(
                RunEvent(
                    task_id=data.task_id,
                    agent="tester",
                    event_type="tester.llm_completion",
                    attempt=data.attempt,
                    payload={
                        "turn": turn,
                        "tool_calls": len(response.tool_calls),
                        "tokens": response.usage.model_dump(),
                    },
                )
            )

            if response.tool_calls:
                if report_only:
                    final_draft = {
                        "status": "ESCALATED",
                        "content": "# Test Report\n\nTester requested tools during its final "
                        "reporting turn instead of returning completion JSON.",
                    }
                    break
                messages.append(
                    Message(
                        role=Role.ASSISTANT,
                        content=response.content,
                        tool_calls=response.tool_calls,
                    )
                )

                circuit_broken = False
                for call in response.tool_calls:
                    command_key = json.dumps(call.arguments.get("command"))
                    unchanged_success = False
                    if call.name == "run_test_command" and command_key in verified_workspaces:
                        state = WorkspaceSnapshot.capture(self.workspace_root)
                        unchanged_success = verified_workspaces[command_key] == {
                            path: entry.sha256 for path, entry in state.files.items()
                        }
                    with tool_call(call.name) as tool:
                        if unchanged_success:
                            output, tool_success, command_result = (
                                "This exact test command already passed on the unchanged workspace. "
                                "Its evidence is retained; no rerun was performed. "
                                "Return your completion report now.",
                                True,
                                None,
                            )
                            report_requested = True
                        else:
                            output, tool_success, command_result = await self._execute_tool(
                                call, forbidden_paths
                            )
                        tool.success = tool_success

                    if tool_success and call.name in {"write_test_file", "run_diagnostic_command"}:
                        report_requested = False

                    if call.name == "write_test_file" and tool_success:
                        root = str(call.arguments.get("test_root") or "").strip("/")
                        if root:
                            test_roots.add(root)

                    if (
                        call.name in {"run_test_command", "run_diagnostic_command"}
                        and command_result is None
                    ):
                        diagnostics.append(f"[Diagnostic, not a test result]\n{output}")
                    if command_result is not None:
                        commands.append(command_result)
                        cmd_key = " ".join(command_result.command)
                        latest_commands[cmd_key] = command_result
                        if command_result.exit_code == 0:
                            state = WorkspaceSnapshot.capture(self.workspace_root)
                            verified_workspaces[command_key] = {
                                path: entry.sha256 for path, entry in state.files.items()
                            }
                        else:
                            verified_workspaces.pop(command_key, None)

                    # Circuit breaker tracking on repeated genuine tool errors
                    sig = f"{call.name}:{json.dumps(call.arguments, sort_keys=True)}"
                    if tool_success:
                        tool_failures.pop(sig, None)
                    else:
                        tool_failures[sig] = tool_failures.get(sig, 0) + 1
                        if tool_failures[sig] >= 3:
                            circuit_broken = True

                    await self.event_sink.emit(
                        RunEvent(
                            task_id=data.task_id,
                            agent="tester",
                            event_type="tester.tool_executed",
                            attempt=data.attempt,
                            payload={"tool": call.name, "success": tool_success},
                        )
                    )
                    messages.append(Message(role=Role.TOOL, content=output, tool_call_id=call.id))
                    if command_result is not None and command_result.execution_error:
                        final_draft = {
                            "status": "ESCALATED",
                            "content": "# Test Runtime Error\n\n" + command_result.execution_error,
                        }
                        break

                if final_draft is not None:
                    break
                if circuit_broken:
                    logger.warning(
                        "tester.circuit_breaker_tripped", task_id=data.task_id, turn=turn
                    )
                    final_draft = {
                        "status": "ESCALATED",
                        "content": "# Test Report\n\nCircuit breaker tripped: repeated tool execution failures.",
                    }
                    break
                continue

            # Assistant response without tool calls -> parse completion report
            messages.append(Message(role=Role.ASSISTANT, content=response.content))
            draft = self._parse_completion_draft(response.content)

            if draft is not None:
                final_draft = draft
                break

            if response.stop_reason in (StopReason.MAX_TOKENS, StopReason.CONTENT_FILTER):
                final_draft = {
                    "status": "ESCALATED",
                    "content": f"# Test Report\n\nLLM generation halted prematurely: {response.stop_reason}.",
                }
                break

            if schema_retries < self.max_schema_retries:
                schema_retries += 1
                messages.append(
                    Message(
                        role=Role.USER,
                        content=(
                            "Please provide your final test evaluation as a valid JSON block with "
                            "'status' ('COMPLETED' or 'ESCALATED') and 'content' (detailed markdown report)."
                        ),
                    )
                )
                continue

            # Schema retries exhausted
            final_draft = {
                "status": "ESCALATED",
                "content": "# Test Report\n\nTester agent produced unparseable completion output after schema retries.",
            }
            break

        # If turn limit was exceeded without draft
        if final_draft is None:
            final_draft = {
                "status": "ESCALATED",
                "content": f"# Test Report\n\nTester agent reached maximum turns limit ({self.max_turns}) without completion.",
            }

        # Mechanical validation & non-test workspace restore
        changed = snapshot.restore_non_test_changes(self.workspace_root, test_roots)
        current = WorkspaceSnapshot.capture(self.workspace_root).files

        changed_test_files = {
            path
            for path in set(snapshot.files) | set(current)
            if (
                is_test_related_path(path)
                or any(path == r or path.startswith(f"{r}/") for r in test_roots)
            )
            and (
                snapshot.files.get(path) is None
                or current.get(path) is None
                or snapshot.files[path].sha256 != current[path].sha256
            )
        }

        # Collect executed test files from commands and workspace test directory
        executed_test_files = {
            arg.replace("\\", "/").strip("/")
            for cmd in commands
            for arg in cmd.command
            if is_test_related_path(arg)
            or any(arg == r or arg.startswith(f"{r}/") for r in test_roots)
        }

        # If full test suite command ran (e.g. pytest without file args), discover all test files in workspace
        all_workspace_tests = {path for path in current if is_test_related_path(path)}
        test_files = sorted(changed_test_files | executed_test_files)
        if not test_files and commands and all_workspace_tests:
            test_files = sorted(all_workspace_tests)

        # Quantitative metrics parsed from the latest command invocations
        latest_cmd_list = list(latest_commands.values()) if latest_commands else commands
        tests_run, tests_failed = self._extract_metrics(latest_cmd_list)

        # Deterministic outcome calculation:
        # 1. At least one test command must have been executed.
        # 2. All final/latest distinct test commands must have exited cleanly with 0.
        # 3. No unauthorized non-test mutations were detected.
        # 4. Final completion status must be COMPLETED.
        has_commands = bool(commands)
        all_latest_clean = has_commands and all(
            res.exit_code == 0 for res in latest_commands.values()
        )
        no_unauthorized_mutations = not bool(changed)
        is_completed = (final_draft or {}).get("status") == "COMPLETED"

        passed = has_commands and all_latest_clean and no_unauthorized_mutations and is_completed

        # Exit code determination
        if passed:
            exit_code = 0
        elif latest_commands:
            exit_code = next(
                (res.exit_code for res in latest_commands.values() if res.exit_code != 0),
                0 if passed else 1,
            )
        else:
            exit_code = 1

        # Format execution output summary
        output_parts = diagnostics + [
            f"$ {' '.join(res.command)}\n{res.output}" for res in commands
        ]
        if changed:
            output_parts.append(
                f"Unauthorized non-test mutations were restored: {', '.join(changed)}"
            )
        if not commands:
            output_parts.append("No test command was executed.")
        output_text = "\n\n".join(output_parts).strip()

        content = final_draft.get(
            "content", "# Test Report\n\nTester did not produce a completion report."
        )

        execution_error = next(
            (res.execution_error for res in commands if res.execution_error), None
        )
        if not is_completed:
            execution_error = execution_error or content
        elif changed:
            execution_error = "Unauthorized non-test mutations were restored: " + ", ".join(changed)
        elif not has_commands:
            execution_error = "No test command was executed."
        if execution_error:
            passed = False
            exit_code = exit_code or 1

        result = TestResults(
            execution_error=execution_error,
            task_id=data.task_id,
            passed=passed,
            exit_code=exit_code,
            tests_run=tests_run,
            tests_failed=tests_failed,
            test_files=test_files,
            output=output_text,
            content=content,
        )

        await self.event_sink.emit(
            RunEvent(
                task_id=data.task_id,
                agent="tester",
                event_type="tester.completed",
                attempt=data.attempt,
                payload={
                    "status": final_draft.get("status", "COMPLETED" if passed else "ESCALATED"),
                    "passed": passed,
                    "exit_code": exit_code,
                    "tests_run": tests_run,
                    "tests_failed": tests_failed,
                    "test_files": test_files,
                },
            )
        )

        return result
