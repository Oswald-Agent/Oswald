"""Unit tests for pipeline artifact schemas, versioned storage, and identifiers."""

import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from oswald.core.artifacts import (
    ArtifactConcurrencyError,
    ArtifactNotFoundError,
    CodeChanges,
    FileSystemArtifactStore,
    Finding,
    InMemoryArtifactStore,
    PlanSpec,
    Provenance,
    ReviewVerdict,
    TaskItem,
    TestResults,
    generate_task_id,
    uuid7,
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


def test_planspec_tasks_whitelist_validation() -> None:
    """Verify PlanSpec rejects tasks whose target_file is not in target_files whitelist."""
    task = TaskItem(
        step=1,
        description="Add health endpoint",
        target_file="src/routes/health.py",
        action="create",
    )
    spec = PlanSpec(
        task_id="task-001",
        summary="Plan with tasks",
        target_files=["src/routes/health.py"],
        tasks=[task],
        content="# Spec",
    )
    assert len(spec.tasks) == 1
    assert spec.tasks[0].target_file == "src/routes/health.py"

    # Unauthorized target file in task should raise ValidationError
    unauthorized_task = TaskItem(
        step=2,
        description="Edit unauthorized file",
        target_file="src/unauthorized.py",
        action="modify",
    )
    with pytest.raises(ValidationError):
        PlanSpec(
            task_id="task-001",
            summary="Invalid task target",
            target_files=["src/routes/health.py"],
            tasks=[unauthorized_task],
            content="# Spec",
        )


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
    assert changes.status == "COMPLETED"
    assert len(changes.modified_files) == 1
    assert "--- a/src/routes/health.py" in changes.diff
    assert changes.content.startswith("# Implementation Notes")


def test_codechanges_requires_modified_files() -> None:
    """Verify CodeChanges rejects an empty modified_files list when COMPLETED."""
    with pytest.raises(ValidationError):
        CodeChanges(
            task_id="task-002",
            summary="Empty change",
            modified_files=[],
            diff="",
            content="No changes made",
        )


def test_codechanges_escalated_status() -> None:
    """Verify CodeChanges supports ESCALATED status with escalation message."""
    changes = CodeChanges(
        task_id="task-002",
        status="ESCALATED",
        summary="Requirement ambiguous",
        modified_files=[],
        diff="",
        content="# Escalation Notes\nCannot determine database schema.",
        escalation="Ambiguity in spec requirements requires human clarification",
    )
    assert changes.status == "ESCALATED"
    assert changes.escalation is not None
    assert "Ambiguity" in changes.escalation

    # Escalated without explanation should fail validation
    with pytest.raises(ValidationError):
        CodeChanges(
            task_id="task-002",
            status="ESCALATED",
            summary="Escalation without explanation",
            content="# Notes",
            escalation="",
        )


def test_finding_schema_and_evidence_cap() -> None:
    """Verify Finding fields, severity enum, and evidence character cap."""
    finding = Finding(
        source="tester",
        severity="blocker",
        problem="AssertionError on 404 response",
        location="tests/test_api.py:42",
        test_id="test_not_found",
        why_it_matters="Client expects proper JSON error shape",
        expected_correction="Return detail message in response body",
        evidence="assert response.status_code == 404",
    )
    assert finding.source == "tester"
    assert finding.severity == "blocker"
    assert finding.location == "tests/test_api.py:42"

    # Evidence longer than 500 chars should fail validation
    with pytest.raises(ValidationError):
        Finding(
            source="reviewer",
            severity="minor",
            problem="Too verbose",
            evidence="x" * 501,
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

    invalid_payload = {
        "task_id": "task-004",
        "verdict": "LGTM",
        "summary": "Invalid status",
        "content": "Invalid review",
    }
    with pytest.raises(ValidationError):
        ReviewVerdict.model_validate(invalid_payload)


@pytest.mark.asyncio
async def test_in_memory_artifact_store_lifecycle_and_versioning() -> None:
    """Verify InMemoryArtifactStore save, versioning, deduplication, and concurrency."""
    store = InMemoryArtifactStore()
    prov = Provenance(agent="planner", model="test-model")
    spec1 = PlanSpec(task_id="task-100", summary="Test plan v1", content="# Spec v1")

    assert not await store.exists("task-100", "spec")
    ref1 = await store.save("task-100", "spec", spec1, produced_by=prov)
    assert await store.exists("task-100", "spec")
    assert ref1.version == 1

    # Deduplication test: re-saving identical content returns ref1 without bumping version
    ref1_dup = await store.save("task-100", "spec", spec1, produced_by=prov)
    assert ref1_dup == ref1
    history = await store.history("task-100", "spec")
    assert len(history) == 1

    # Saving modified spec bumps version and sets lineage
    spec2 = PlanSpec(task_id="task-100", summary="Test plan v2", content="# Spec v2")
    ref2 = await store.save(
        "task-100",
        "spec",
        spec2,
        produced_by=prov,
        expected_parent=ref1.sha256,
    )
    assert ref2.version == 2
    assert ref2.sha256 != ref1.sha256

    # Optimistic concurrency check: invalid expected parent raises error
    spec3 = PlanSpec(task_id="task-100", summary="Test plan v3", content="# Spec v3")
    with pytest.raises(ArtifactConcurrencyError):
        await store.save(
            "task-100",
            "spec",
            spec3,
            produced_by=prov,
            expected_parent="non-matching-parent-hash",
        )

    # Load tests
    loaded1 = await store.load(ref1, PlanSpec)
    assert loaded1.summary == "Test plan v1"
    loaded_lat = await store.load_latest("task-100", "spec", PlanSpec)
    assert loaded_lat.summary == "Test plan v2"

    with pytest.raises(ArtifactNotFoundError):
        await store.load_latest("task-100", "missing", PlanSpec)


@pytest.mark.asyncio
async def test_filesystem_artifact_store_lifecycle(tmp_path: Path) -> None:
    """Verify FileSystemArtifactStore persists and loads JSON from disk."""
    store = FileSystemArtifactStore(base_dir=tmp_path)
    prov = Provenance(agent="coder", attempt=1)
    changes = CodeChanges(
        task_id="task-200",
        summary="Filesystem test changes",
        modified_files=["foo.py"],
        diff="+foo",
        content="# Changes",
    )

    assert not await store.exists("task-200", "code_changes")
    ref1 = await store.save("task-200", "code_changes", changes, produced_by=prov)
    assert await store.exists("task-200", "code_changes")
    assert ref1.version == 1

    loaded = await store.load(ref1, CodeChanges)
    assert loaded.task_id == changes.task_id
    assert loaded.summary == changes.summary
    assert loaded.diff == changes.diff

    loaded_latest = await store.load_latest("task-200", "code_changes", CodeChanges)
    assert loaded_latest.summary == changes.summary

    with pytest.raises(ArtifactNotFoundError):
        await store.load_latest("task-200", "nonexistent", PlanSpec)


def test_uuid7_generation() -> None:
    """Verify UUIDv7 adheres to RFC 9562 format and is time-ordered."""
    u1 = uuid7()
    u2 = uuid7()

    assert u1.version == 7
    assert u1.variant == uuid.RFC_4122
    assert str(u1) <= str(u2)

    task_id = generate_task_id()
    assert isinstance(task_id, str)
    parsed = uuid.UUID(task_id)
    assert parsed.version == 7
