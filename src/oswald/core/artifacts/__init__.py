"""Core artifacts package.

Responsibility:
    Pydantic schemas and a storage Protocol for agent artifact passing.

Forbidden imports:
    Must not import from `oswald.api`, `oswald.workers`, `oswald.db`, or `sqlalchemy`.
"""

from oswald.core.artifacts.identifiers import generate_task_id, uuid7
from oswald.core.artifacts.models import (
    ArtifactRef,
    BaseArtifact,
    CodeChanges,
    CodeChangesStatus,
    Finding,
    FindingSeverity,
    FindingSource,
    PlanSpec,
    Provenance,
    ReviewVerdict,
    ReviewVerdictStatus,
    TaskItem,
    TestResults,
)
from oswald.core.artifacts.storage import (
    ArtifactConcurrencyError,
    ArtifactNotFoundError,
    ArtifactStore,
    FileSystemArtifactStore,
    InMemoryArtifactStore,
    compute_artifact_sha256,
)

__all__ = [
    "ArtifactConcurrencyError",
    "ArtifactNotFoundError",
    "ArtifactRef",
    "ArtifactStore",
    "BaseArtifact",
    "CodeChanges",
    "CodeChangesStatus",
    "FileSystemArtifactStore",
    "Finding",
    "FindingSeverity",
    "FindingSource",
    "InMemoryArtifactStore",
    "PlanSpec",
    "Provenance",
    "ReviewVerdict",
    "ReviewVerdictStatus",
    "TaskItem",
    "TestResults",
    "compute_artifact_sha256",
    "generate_task_id",
    "uuid7",
]
