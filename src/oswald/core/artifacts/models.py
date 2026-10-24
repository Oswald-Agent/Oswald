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


class TaskItem(BaseModel):
    """Discrete sub-task produced by Planner inside PlanSpec."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step: int = Field(ge=1, description="1-indexed step number")
    description: str = Field(min_length=1, description="Description of the task")
    target_file: str = Field(
        min_length=1, description="Relative path of file to create/modify/delete"
    )
    action: Literal["create", "modify", "delete"] = Field(
        description="Action to perform on target_file"
    )
    details: str = Field(default="", description="Optional additional instructions or context")


class PlanSpec(BaseArtifact):
    """Planner Agent output artifact (spec.md contract).

    Control fields:
        target_files: Whitelist of files to touch, enforcing sandbox boundaries.
        tasks: Ordered list of discrete implementation tasks.
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
    tasks: list[TaskItem] = Field(
        default_factory=list,
        description="Ordered sequence of implementation sub-tasks",
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

    def model_post_init(self, __context: object) -> None:
        if self.target_files and self.tasks:
            allowed = set(self.target_files)
            for t in self.tasks:
                if t.target_file not in allowed:
                    raise ValueError(
                        f"Task target_file '{t.target_file}' not permitted by target_files whitelist"
                    )


CodeChangesStatus = Literal["COMPLETED", "ESCALATED"]


class CodeChanges(BaseArtifact):
    """Coder Agent output artifact (changes.md + diff contract).

    Control fields:
        status: Execution status (COMPLETED or ESCALATED).
        modified_files: Actual files created or edited in the sandbox.
        deviations: Intentional deviations from specification with technical justification.
        known_limitations: Unimplemented edge cases or limitations.
        test_attention: Areas requiring focused testing by Tester.
        dependency_changes: Manifests or lockfiles modified during implementation.
        escalation: Rationale if execution could not complete.
    Raw content:
        diff: Standard unified git diff of code changes.
        content: Markdown summary (changes.md) explaining implementation decisions.
    """

    status: CodeChangesStatus = Field(
        default="COMPLETED",
        description="Execution status (COMPLETED or ESCALATED)",
    )
    summary: str = Field(
        min_length=1,
        description="Summary of the code changes implemented or escalation reason",
    )
    modified_files: list[str] = Field(
        default_factory=list,
        description="List of files that were modified or created",
    )
    diff: str = Field(
        default="",
        description="Unified git diff representing the changes made in sandbox",
    )
    content: str = Field(
        min_length=1,
        description="Full markdown change log (changes.md) for Tester and Reviewer",
    )
    deviations: list[str] = Field(
        default_factory=list,
        description="Deviations from spec with rationale",
    )
    known_limitations: list[str] = Field(
        default_factory=list,
        description="Known edge cases or limitations",
    )
    test_attention: list[str] = Field(
        default_factory=list,
        description="Areas the Tester should specifically verify",
    )
    dependency_changes: list[str] = Field(
        default_factory=list,
        description="Dependencies or manifests touched",
    )
    escalation: str | None = Field(
        default=None,
        description="Reason for escalation if status is ESCALATED",
    )

    def model_post_init(self, __context: object) -> None:
        if self.status == "COMPLETED":
            if not self.modified_files:
                raise ValueError("COMPLETED CodeChanges requires at least one modified file")
            if not self.diff:
                raise ValueError("COMPLETED CodeChanges requires a non-empty diff")
            if self.escalation is not None:
                raise ValueError("COMPLETED CodeChanges must not set escalation")
        elif self.status == "ESCALATED":
            if not self.escalation or not self.escalation.strip():
                raise ValueError("ESCALATED CodeChanges requires a non-empty escalation message")


FindingSource = Literal["tester", "reviewer"]
FindingSeverity = Literal["blocker", "major", "minor"]


class Finding(BaseModel):
    """Structured finding emitted by Tester or Reviewer on fix loops."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: FindingSource = Field(description="Originating agent reporting this finding")
    severity: FindingSeverity = Field(description="Severity classification of the defect")
    problem: str = Field(min_length=1, description="Concise description of the observed defect")
    location: str | None = Field(default=None, description="Optional path:line location pointer")
    test_id: str | None = Field(default=None, description="Identifier of failing test (Tester)")
    why_it_matters: str = Field(default="", description="Impact or rationale for fixing this issue")
    expected_correction: str = Field(default="", description="Prescribed fix or expected behavior")
    evidence: str | None = Field(
        default=None,
        max_length=500,
        description="Short assertion failure or snippet (<= 500 chars)",
    )


class ArtifactRef(BaseModel):
    """Content-addressed reference to an immutable artifact version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(description="Task identifier associated with artifact")
    name: str = Field(description="Logical artifact name (e.g. 'spec', 'code_changes')")
    version: int = Field(ge=1, description="Monotonically increasing version number")
    sha256: str = Field(description="Hexadecimal SHA-256 hash of canonical artifact payload")


class Provenance(BaseModel):
    """Execution provenance metadata recording who produced an artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent: str = Field(description="Agent identifier (e.g. 'coder', 'planner')")
    attempt: int = Field(default=1, ge=1, description="Attempt number for fix loop")
    model: str = Field(default="", description="LLM model identifier used")
    prompt_version: str = Field(default="", description="Version or hash of system prompt")
    sandbox_image_digest: str = Field(default="", description="Digest of sandbox container image")


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
