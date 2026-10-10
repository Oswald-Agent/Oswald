"""Storage protocols and implementations for OSWALD pipeline artifacts.

Architecture Rules:
    - Zero imports of sqlalchemy or ORM models.
    - Pure Python stdlib and Pydantic.
"""

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


class ArtifactStore(Protocol):
    """Protocol defining persistence operations for pipeline stage artifacts."""

    def save(self, task_id: str, name: str, artifact: BaseModel) -> None:
        """Persist an artifact under a given task identifier and artifact name."""
        ...

    def load[T: BaseModel](self, task_id: str, name: str, model_cls: type[T]) -> T:
        """Load and validate an artifact by task identifier and name."""
        ...

    def exists(self, task_id: str, name: str) -> bool:
        """Check whether an artifact exists."""
        ...


class FileSystemArtifactStore:
    """Stores artifacts as formatted JSON files on the local filesystem.

    Artifacts are partitioned by task identifier under `base_dir / <task_id> / <name>.json`.
    """

    def __init__(self, base_dir: Path | str = "runs") -> None:
        self.base_dir = Path(base_dir)

    def _resolve_path(self, task_id: str, name: str) -> Path:
        filename = f"{name}.json" if not name.endswith(".json") else name
        return self.base_dir / task_id / filename

    def save(self, task_id: str, name: str, artifact: BaseModel) -> None:
        target = self._resolve_path(task_id, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")

    def load[T: BaseModel](self, task_id: str, name: str, model_cls: type[T]) -> T:
        target = self._resolve_path(task_id, name)
        if not target.is_file():
            raise FileNotFoundError(f"Artifact '{name}' for task '{task_id}' not found at {target}")
        return model_cls.model_validate_json(target.read_text(encoding="utf-8"))

    def exists(self, task_id: str, name: str) -> bool:
        return self._resolve_path(task_id, name).is_file()


class InMemoryArtifactStore:
    """Ephemeral in-memory artifact storage primarily used for fast unit testing."""

    def __init__(self) -> None:
        self._storage: dict[tuple[str, str], str] = {}

    def save(self, task_id: str, name: str, artifact: BaseModel) -> None:
        key = (task_id, name)
        self._storage[key] = artifact.model_dump_json()

    def load[T: BaseModel](self, task_id: str, name: str, model_cls: type[T]) -> T:
        key = (task_id, name)
        if key not in self._storage:
            raise FileNotFoundError(f"Artifact '{name}' for task '{task_id}' not found in memory")
        return model_cls.model_validate_json(self._storage[key])

    def exists(self, task_id: str, name: str) -> bool:
        return (task_id, name) in self._storage
