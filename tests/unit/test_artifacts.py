"""Unit tests for pipeline artifact schemas and storage protocols."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from oswald.core.artifacts import (
    CodeChanges,
    FileSystemArtifactStore,
    InMemoryArtifactStore,
    PlanSpec,
    ReviewVerdict,
    TestResults,
)


def test_planspec_valid_creation_and_immutability() -> None:
    """Verify PlanSpec creates successfully and enforces immutability (frozen=True)."""
    spec = PlanSpec(
        task_id="task-001",
        summary="Create health endpoint",
        target_files=["src/routes/health.py"],
        acceptance_criteria=["Returns HTTP 200 with ok status"],
        content="# Plan Spec\nImplement the health endpoint.",
    )

    assert spec.task_id == "task-001"
    assert spec.summary == "Create health endpoint"
    assert spec.target_files == ["src/routes/health.py"]
    assert spec.has_open_questions is False
    assert spec.open_questions == []
    assert "# Plan Spec" in spec.content

    # Immutability check
    with pytest.raises(ValidationError):
        spec.summary = "Attempted mutation"


def test_planspec_rejects_extra_fields() -> None:
    """Verify PlanSpec rejects unexpected keys at runtime (extra='forbid')."""
    payload = {
        "task_id": "task-001",
        "summary": "Test",
        "content": "Spec content",
        "unwanted_field": "hallucinated",
    }
    with pytest.raises(ValidationError) as exc_info:
        PlanSpec.model_validate(payload)
    assert "Extra inputs are not permitted" in str(exc_info.value)


def test_codechanges_creation_and_diff() -> None:
    """Verify CodeChanges schema and diff field validation."""
    changes = CodeChanges(
        task_id="task-002",
        summary="Added health router",
        modified_files=["src/routes/health.py"],
        diff="--- a/src/routes/health.py\n+++ b/src/routes/health.py\n@@ -0,0 +1,5 @@\n+ok",
        content="# Implementation Notes\nAdded APIRouter for /healthz.",
    )

    assert changes.task_id == "task-002"
    assert len(changes.modified_files) == 1
    assert "--- a/src/routes/health.py" in changes.diff
    assert changes.content.startswith("# Implementation Notes")


def test_codechanges_requires_modified_files() -> None:
    """Verify CodeChanges rejects an empty modified_files list."""
    with pytest.raises(ValidationError):
        CodeChanges(
            task_id="task-002",
            summary="Empty change",
            modified_files=[],
            diff="",
            content="No changes made",
        )


def test_testresults_metrics_and_status() -> None:
    """Verify TestResults validates pass/fail status and test metrics."""
    results = TestResults(
        task_id="task-003",
        passed=True,
        exit_code=0,
        tests_run=5,
        tests_failed=0,
        test_files=["tests/unit/test_health.py"],
        output="5 passed in 0.05s",
        content="# Test Report\nAll 5 unit tests passed cleanly.",
    )

    assert results.passed is True
    assert results.exit_code == 0
    assert results.tests_run == 5
    assert results.tests_failed == 0

    # Negative test count is rejected
    with pytest.raises(ValidationError):
        TestResults(
            task_id="task-003",
            passed=False,
            tests_run=-1,
            content="Invalid",
        )


def test_reviewverdict_status_validation() -> None:
    """Verify ReviewVerdict enforces Literal verdict statuses."""
    verdict = ReviewVerdict(
        task_id="task-004",
        verdict="APPROVED",
        summary="Clean implementation with high test coverage",
        content="# Review Report\nApproved for merge.",
    )
    assert verdict.verdict == "APPROVED"
    assert verdict.blocking_issues == []

    # Invalid status strings should be rejected at runtime
    invalid_payload = {
        "task_id": "task-004",
        "verdict": "LGTM",
        "summary": "Invalid status",
        "content": "Invalid review",
    }
    with pytest.raises(ValidationError):
        ReviewVerdict.model_validate(invalid_payload)


def test_in_memory_artifact_store() -> None:
    """Verify InMemoryArtifactStore save, load, and exists lifecycle."""
    store = InMemoryArtifactStore()
    spec = PlanSpec(
        task_id="task-100",
        summary="In-memory test plan",
        content="# Spec",
    )

    assert not store.exists("task-100", "spec")
    store.save("task-100", "spec", spec)
    assert store.exists("task-100", "spec")

    loaded = store.load("task-100", "spec", PlanSpec)
    assert loaded.task_id == spec.task_id
    assert loaded.summary == spec.summary
    assert loaded.content == spec.content

    with pytest.raises(FileNotFoundError):
        store.load("task-100", "missing", PlanSpec)


def test_filesystem_artifact_store(tmp_path: Path) -> None:
    """Verify FileSystemArtifactStore persists and loads JSON from disk."""
    store = FileSystemArtifactStore(base_dir=tmp_path)
    spec = PlanSpec(
        task_id="task-200",
        summary="Filesystem test plan",
        content="# Spec file",
    )

    assert not store.exists("task-200", "spec")
    store.save("task-200", "spec", spec)
    assert store.exists("task-200", "spec")

    # Verify physical file exists on disk
    expected_file = tmp_path / "task-200" / "spec.json"
    assert expected_file.is_file()
    assert '"summary": "Filesystem test plan"' in expected_file.read_text()

    loaded = store.load("task-200", "spec", PlanSpec)
    assert loaded.task_id == spec.task_id
    assert loaded.summary == spec.summary
    assert loaded.content == spec.content

    with pytest.raises(FileNotFoundError):
        store.load("task-200", "nonexistent", PlanSpec)
