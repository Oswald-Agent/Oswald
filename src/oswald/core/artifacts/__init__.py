"""Core artifacts package.

Responsibility:
    Pydantic schemas and a storage Protocol for agent artifact passing.

Forbidden imports:
    Must not import from `oswald.api`, `oswald.workers`, `oswald.db`, or `sqlalchemy`.
"""

from oswald.core.artifacts.models import (
    BaseArtifact,
    CodeChanges,
    PlanSpec,
    ReviewVerdict,
    ReviewVerdictStatus,
    TestResults,
)
from oswald.core.artifacts.storage import (
    ArtifactStore,
    FileSystemArtifactStore,
    InMemoryArtifactStore,
)

__all__ = [
    "ArtifactStore",
    "BaseArtifact",
    "CodeChanges",
    "FileSystemArtifactStore",
    "InMemoryArtifactStore",
    "PlanSpec",
    "ReviewVerdict",
    "ReviewVerdictStatus",
    "TestResults",
]
