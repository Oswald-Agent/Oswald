"""Read-only review of pipeline artifacts through the LLMClient protocol."""

import json
import re
from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from oswald.core.agents.coder import (
    BudgetGuard,
    EventSink,
    NoOpBudgetGuard,
    NoOpEventSink,
    RunEvent,
)
from oswald.core.agents.telemetry import agent_scope, complete
from oswald.core.artifacts.models import (
    CodeChanges,
    PlanSpec,
    ReviewVerdict,
    ReviewVerdictStatus,
    TestResults,
)
from oswald.core.llm.exceptions import LLMError
from oswald.core.llm.protocol import LLMClient
from oswald.core.llm.types import LLMRequest, Message, Role, StopReason

logger = structlog.get_logger(component="agent.reviewer")

_DEFAULT_PROMPT_PATH = Path(__file__).parent / "prompts" / "reviewer_system.md"
_JSON_BLOCK_REGEX = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class ReviewerInput(BaseModel):
    """Immutable upstream artifacts delivered to the Reviewer for one attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    attempt: int = Field(default=1, ge=1)
    spec: PlanSpec
    changes: CodeChanges
    test_results: TestResults
    baseline_context: str = ""


class _ReviewResponse(BaseModel):
    """Model-authored fields; task identity and timestamps belong to the caller."""

    model_config = ConfigDict(extra="forbid", strict=True)

    verdict: ReviewVerdictStatus
    summary: str = Field(min_length=1)
    blocking_issues: list[str]
    content: str = Field(min_length=1)


class ReviewerAgent:
    """Evaluate a specification, diff, and test report without executing tools.

    Inject an LLMClient and the model identifier from Settings.reviewer_model.
    The caller owns artifact persistence and the separate human approval gate.
    """

    def __init__(
        self,
        llm: LLMClient,
        *,
        model: str,
        budget_guard: BudgetGuard | None = None,
        event_sink: EventSink | None = None,
        prompt_path: Path | None = None,
        max_schema_retries: int = 2,
    ) -> None:
        if not model.strip():
            raise ValueError("Reviewer model must not be blank")
        self.llm = llm
        self.model = model
        self.budget_guard = budget_guard or NoOpBudgetGuard()
        self.event_sink = event_sink if event_sink is not None else NoOpEventSink()
        self.prompt_path = prompt_path or _DEFAULT_PROMPT_PATH
        self.max_schema_retries = max_schema_retries

    @classmethod
    def _extract_candidates(cls, text: str) -> list[str]:
        """Extract JSON candidates from markdown fences, outer braces, or raw text."""
        clean = text.strip()
        candidates: list[str] = []
        for match in _JSON_BLOCK_REGEX.finditer(clean):
            candidates.append(match.group(1).strip())
        first_brace = clean.find("{")
        last_brace = clean.rfind("}")
        if first_brace != -1 and last_brace > first_brace:
            outer = clean[first_brace : last_brace + 1].strip()
            if outer not in candidates:
                candidates.append(outer)
        if clean not in candidates:
            candidates.append(clean)
        return candidates

    @classmethod
    def _extract_json_text(cls, text: str) -> str:
        """Strip markdown code block wrappers or extract outermost JSON object."""
        candidates = cls._extract_candidates(text)
        for cand in candidates:
            try:
                _ReviewResponse.model_validate_json(cand)
                return cand
            except ValidationError:
                continue
        return candidates[0] if candidates else text.strip()

    async def run(self, data: ReviewerInput) -> ReviewVerdict:
        """Run with bounded progress logging and exception-safe timer cleanup."""
        async with agent_scope("reviewer", data.task_id, data.attempt) as progress:
            result = await self._run(data)
            progress.result(result)
            return result

    async def _run(self, data: ReviewerInput) -> ReviewVerdict:
        """Return a validated review for artifacts belonging to the same task.

        Raises:
            BudgetExceededError: The shared budget guard rejects the review attempt.
            ValueError: Task identifiers differ, the completion is incomplete,
                or the verdict contradicts blocking evidence.
            ValidationError: The completion does not match the response schema.
            LLMError: The injected client cannot complete the request.
        """
        spec, changes, test_results = data.spec, data.changes, data.test_results
        if any(artifact.task_id != data.task_id for artifact in (spec, changes, test_results)):
            raise ValueError("Reviewer input artifacts must belong to the same task")

        self.budget_guard.check(data.task_id)
        await self.event_sink.emit(
            RunEvent(
                task_id=data.task_id,
                agent="reviewer",
                event_type="reviewer.started",
                attempt=data.attempt,
            )
        )
        messages = [
            Message(role=Role.SYSTEM, content=self.prompt_path.read_text(encoding="utf-8")),
            Message(
                role=Role.USER,
                content=json.dumps(
                    {
                        "spec": spec.model_dump(mode="json"),
                        "changes": changes.model_dump(mode="json"),
                        "test_results": test_results.model_dump(mode="json"),
                        **(
                            {"original_request": data.baseline_context}
                            if data.baseline_context.strip()
                            else {}
                        ),
                    },
                    ensure_ascii=False,
                ),
            ),
        ]

        review: _ReviewResponse | None = None
        last_schema_error: ValidationError | None = None
        for retry in range(self.max_schema_retries + 1):
            if retry > 0:
                logger.info(
                    "agent.schema_retry",
                    agent="reviewer",
                    task_id=data.task_id,
                    attempt=data.attempt,
                    retry=retry,
                )
            request = LLMRequest(model=self.model, messages=messages)
            try:
                response = await complete(self.llm, request)
            except LLMError as err:
                if retry > 0 and last_schema_error is not None:
                    raise last_schema_error from err
                raise
            await self.event_sink.emit(
                RunEvent(
                    task_id=data.task_id,
                    agent="reviewer",
                    event_type="reviewer.llm_completion",
                    attempt=data.attempt,
                    payload={
                        "stop_reason": str(response.stop_reason),
                        "tool_calls": len(response.tool_calls),
                        "tokens": response.usage.model_dump(),
                    },
                )
            )
            if response.stop_reason != StopReason.END_TURN or response.tool_calls:
                raise ValueError("Reviewer requires a complete response without tool calls")

            for cand in self._extract_candidates(response.content):
                try:
                    review = _ReviewResponse.model_validate_json(cand)
                    break
                except ValidationError as err:
                    last_schema_error = err

            if review is not None:
                break

            if retry < self.max_schema_retries:
                messages.append(Message(role=Role.ASSISTANT, content=response.content))
                messages.append(
                    Message(
                        role=Role.USER,
                        content=(
                            f"Review response validation failed: {last_schema_error}. Please return a valid JSON object "
                            "with 'verdict' ('APPROVED', 'CHANGES_REQUESTED', or 'REJECTED'), 'summary', "
                            "'blocking_issues', and 'content'."
                        ),
                    )
                )
                continue
            if last_schema_error is not None:
                raise last_schema_error

        if review is None:
            raise ValueError("Reviewer failed to produce a valid response")
        if not review.summary.strip() or not review.content.strip():
            raise ValueError("Reviewer summary and content must not be blank")
        if any(not issue.strip() for issue in review.blocking_issues):
            raise ValueError("Reviewer blocking issues must not be blank")
        failing_tests = (
            not test_results.passed
            or bool(test_results.execution_error)
            or test_results.exit_code != 0
            or test_results.tests_failed > 0
        )
        if review.verdict == "APPROVED" and failing_tests:
            blocking = list(review.blocking_issues)
            issue = "Changes cannot be approved while tests fail"
            if issue not in blocking:
                blocking.append(issue)
            review = review.model_copy(
                update={
                    "verdict": "CHANGES_REQUESTED",
                    "blocking_issues": blocking,
                }
            )

        if review.verdict == "APPROVED":
            if review.blocking_issues:
                raise ValueError("Reviewer cannot approve changes with blocking issues")
            if changes.status != "COMPLETED":
                raise ValueError("Reviewer cannot approve escalated code changes")
            if spec.has_open_questions or spec.open_questions:
                raise ValueError("Reviewer cannot approve a specification with open questions")
            # Match the relative path normalization used by the Coder's scope check.
            targets = {
                path.strip().removeprefix("./").removeprefix("/") for path in spec.target_files
            }
            modified = {
                path.strip().removeprefix("./").removeprefix("/") for path in changes.modified_files
            }
            if modified - targets:
                raise ValueError(
                    "Reviewer cannot approve changes outside the specification targets"
                )
        elif not review.blocking_issues:
            raise ValueError("Reviewer must explain blocking issues when changes are not approved")

        verdict = ReviewVerdict(task_id=data.task_id, **review.model_dump())
        await self.event_sink.emit(
            RunEvent(
                task_id=data.task_id,
                agent="reviewer",
                event_type="reviewer.completed",
                attempt=data.attempt,
                payload={"verdict": verdict.verdict, "blocking_issues": verdict.blocking_issues},
            )
        )
        return verdict
