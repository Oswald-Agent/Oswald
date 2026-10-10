"""Immutable artifact schemas for OSWALD pipeline handoffs.

Each artifact represents the immutable output of a pipeline stage.
Architecture Rules:
    - Pure Pydantic v2 schemas.
    - Zero imports of sqlalchemy or ORM models.
    - Control fields are parsed for state machine routing; markdown content
      is preserved as raw text for downstream agent prompts.
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BaseArtifact(BaseModel):
    """Base model enforcing strict validation and immutability for all artifacts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
    )

    task_id: str = Field(description="Unique task or run correlation identifier")
    created_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
        description="ISO 8601 UTC timestamp of artifact creation",
    )


class PlanSpec(BaseArtifact):
    """Planner Agent output artifact (spec.md contract).

    Control fields:
        target_files: Whitelist of files to touch, enforcing sandbox boundaries.
        has_open_questions: Flag allowing the state machine to pause for human clarification.
    Raw content:
        content: Markdown specification passed directly into Coder prompt.
    """

    summary: str = Field(
        min_length=1,
        description="One-sentence executive summary of the planned work",
    )
    target_files: list[str] = Field(
        default_factory=list,
        description="List of file paths the Coder is authorized to create or modify",
    )
    acceptance_criteria: list[str] = Field(
        default_factory=list,
        description="Specific verifiable requirements for completion",
    )
    has_open_questions: bool = Field(
        default=False,
        description="True if planning cannot proceed without human clarification",
    )
    open_questions: list[str] = Field(
        default_factory=list,
        description="Unresolved questions for the requester if has_open_questions is True",
    )
    content: str = Field(
        min_length=1,
        description="Full markdown specification (spec.md) passed to Coder",
    )


class CodeChanges(BaseArtifact):
    """Coder Agent output artifact (changes.md + diff contract).

    Control fields:
        modified_files: Actual files created or edited in the sandbox.
    Raw content:
        diff: Standard unified git diff of code changes.
        content: Markdown summary (changes.md) explaining implementation decisions.
    """

    summary: str = Field(
        min_length=1,
        description="Summary of the code changes implemented",
    )
    modified_files: list[str] = Field(
        min_length=1,
        description="List of files that were modified or created",
    )
    diff: str = Field(
        description="Unified git diff representing the changes made in sandbox",
    )
    content: str = Field(
        min_length=1,
        description="Full markdown change log (changes.md) for Tester and Reviewer",
    )


class TestResults(BaseArtifact):
    """Tester Agent output artifact (test-results.md contract).

    Control fields:
        passed: Deterministic boolean deciding whether the run proceeds or loops back.
        exit_code: Process exit code from the test suite runner.
        tests_run / tests_failed: Quantitative metrics for observability and thresholds.
    Raw content:
        output: Raw stdout and stderr output from the test execution.
        content: Markdown report (test-results.md) explaining test coverage and edge cases.
    """

    __test__ = False

    passed: bool = Field(
        description="Whether all tests executed cleanly and passed",
    )
    exit_code: int = Field(
        default=0,
        description="Exit status code returned by the test runner process",
    )
    tests_run: int = Field(
        ge=0,
        default=0,
        description="Total number of tests executed",
    )
    tests_failed: int = Field(
        ge=0,
        default=0,
        description="Number of failed tests",
    )
    test_files: list[str] = Field(
        default_factory=list,
        description="Test files created, updated, or executed",
    )
    output: str = Field(
        default="",
        description="Raw terminal stdout/stderr output from the test execution",
    )
    content: str = Field(
        min_length=1,
        description="Full markdown report (test-results.md) for Reviewer",
    )


ReviewVerdictStatus = Literal["APPROVED", "CHANGES_REQUESTED", "REJECTED"]


class ReviewVerdict(BaseArtifact):
    """Reviewer Agent output artifact (review.md contract).

    Control fields:
        verdict: Routing decision for the state machine (unlocks approval gate if APPROVED).
        blocking_issues: Explicit list of blocking defects if not approved.
    Raw content:
        content: Markdown review report (review.md) surfaced to human reviewer and PR.
    """

    verdict: ReviewVerdictStatus = Field(
        description="Final evaluation status for the code changes",
    )
    summary: str = Field(
        min_length=1,
        description="Executive summary of the review findings",
    )
    blocking_issues: list[str] = Field(
        default_factory=list,
        description="List of blocking concerns if changes were requested or rejected",
    )
    content: str = Field(
        min_length=1,
        description="Full markdown evaluation report (review.md) for human technical sign-off",
    )
