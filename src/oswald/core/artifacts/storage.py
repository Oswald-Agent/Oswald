"""Storage protocols and implementations for OSWALD pipeline artifacts.

Architecture Rules:
    - Zero imports of sqlalchemy or ORM models.
    - Pure Python stdlib and Pydantic.
    - Versioned, content-addressed append-only lineage with optimistic concurrency.
"""

import asyncio
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from oswald.core.artifacts.models import ArtifactRef, Provenance


class ArtifactNotFoundError(FileNotFoundError):
    """Raised when an artifact reference or version is not found in store."""


class ArtifactConcurrencyError(ValueError):
    """Raised when expected_parent does not match latest artifact version."""


def compute_artifact_sha256(artifact: BaseModel) -> str:
    """Compute deterministic SHA-256 hash of canonical artifact JSON excluding created_at."""
    data = artifact.model_dump(mode="json")
    data.pop("created_at", None)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@runtime_checkable
class ArtifactStore(Protocol):
    """Protocol defining persistence and lineage operations for pipeline stage artifacts."""

    async def save(
        self,
        task_id: str,
        name: str,
        artifact: BaseModel,
        *,
        produced_by: Provenance | None = None,
        inputs: Sequence[ArtifactRef] = (),
        expected_parent: str | None = None,
    ) -> ArtifactRef:
        """Persist an artifact version with lineage and return its content-addressed reference."""
        ...

    async def load[T: BaseModel](self, ref: ArtifactRef, model_cls: type[T]) -> T:
        """Load and validate an artifact by content-addressed reference."""
        ...

    async def load_latest[T: BaseModel](self, task_id: str, name: str, model_cls: type[T]) -> T:
        """Load and validate the latest version of an artifact by task_id and name."""
        ...

    async def latest(self, task_id: str, name: str) -> ArtifactRef | None:
        """Retrieve the latest ArtifactRef for a task and artifact name."""
        ...

    async def history(self, task_id: str, name: str) -> list[ArtifactRef]:
        """Retrieve chronological history of ArtifactRefs for a task and artifact name."""
        ...

    async def exists(self, task_id: str, name: str) -> bool:
        """Check whether at least one version of an artifact exists."""
        ...


class _VersionRecord:
    def __init__(
        self,
        ref: ArtifactRef,
        payload: str,
        produced_by: Provenance,
        inputs: list[ArtifactRef],
        parent_sha256: str | None,
    ) -> None:
        self.ref = ref
        self.payload = payload
        self.produced_by = produced_by
        self.inputs = inputs
        self.parent_sha256 = parent_sha256


class InMemoryArtifactStore:
    """In-memory append-only artifact store for testing and fast local execution."""

    def __init__(self) -> None:
        self._history: dict[tuple[str, str], list[_VersionRecord]] = {}
        self._by_ref: dict[tuple[str, str, int, str], str] = {}

    async def save(
        self,
        task_id: str,
        name: str,
        artifact: BaseModel,
        *,
        produced_by: Provenance | None = None,
        inputs: Sequence[ArtifactRef] = (),
        expected_parent: str | None = None,
    ) -> ArtifactRef:
        key = (task_id, name)
        history = self._history.setdefault(key, [])
        sha = compute_artifact_sha256(artifact)
        payload = artifact.model_dump_json(indent=2)
        provenance = produced_by or Provenance(agent="unknown")

        # Deduplication check: identical output is an idempotent no-op
        if history and history[-1].ref.sha256 == sha:
            return history[-1].ref

        # Optimistic concurrency check
        if expected_parent is not None:
            if not history:
                raise ArtifactConcurrencyError(
                    f"Expected parent '{expected_parent}' but no prior versions exist for ({task_id}, {name})"
                )
            if history[-1].ref.sha256 != expected_parent:
                raise ArtifactConcurrencyError(
                    f"Parent mismatch for ({task_id}, {name}): expected '{expected_parent}', current is '{history[-1].ref.sha256}'"
                )

        version = len(history) + 1
        ref = ArtifactRef(task_id=task_id, name=name, version=version, sha256=sha)
        parent_sha = history[-1].ref.sha256 if history else None

        record = _VersionRecord(
            ref=ref,
            payload=payload,
            produced_by=provenance,
            inputs=list(inputs),
            parent_sha256=parent_sha,
        )
        history.append(record)
        self._by_ref[(task_id, name, version, sha)] = payload
        return ref

    async def load[T: BaseModel](self, ref: ArtifactRef, model_cls: type[T]) -> T:
        ref_key = (ref.task_id, ref.name, ref.version, ref.sha256)
        if ref_key not in self._by_ref:
            raise ArtifactNotFoundError(
                f"Artifact version {ref.version} ({ref.sha256[:8]}) not found for ({ref.task_id}, {ref.name})"
            )
        return model_cls.model_validate_json(self._by_ref[ref_key])

    async def load_latest[T: BaseModel](self, task_id: str, name: str, model_cls: type[T]) -> T:
        lat = await self.latest(task_id, name)
        if lat is None:
            raise ArtifactNotFoundError(f"No artifact found for ({task_id}, {name})")
        return await self.load(lat, model_cls)

    async def latest(self, task_id: str, name: str) -> ArtifactRef | None:
        records = self._history.get((task_id, name), [])
        return records[-1].ref if records else None

    async def history(self, task_id: str, name: str) -> list[ArtifactRef]:
        return [r.ref for r in self._history.get((task_id, name), [])]

    async def exists(self, task_id: str, name: str) -> bool:
        return bool(self._history.get((task_id, name)))


class FileSystemArtifactStore:
    """Stores versioned artifacts and lineage metadata on the local filesystem."""

    def __init__(self, base_dir: Path | str = "runs") -> None:
        self.base_dir = Path(base_dir).resolve()

    def _dir_for(self, task_id: str, name: str) -> Path:
        return self.base_dir / task_id / name

    def _lineage_path(self, task_id: str, name: str) -> Path:
        return self._dir_for(task_id, name) / "_lineage.json"

    def _file_path(self, ref: ArtifactRef) -> Path:
        return self._dir_for(ref.task_id, ref.name) / f"v{ref.version}_{ref.sha256[:12]}.json"

    def _read_lineage_sync(self, task_id: str, name: str) -> list[dict[str, object]]:
        lineage_file = self._lineage_path(task_id, name)
        if not lineage_file.is_file():
            return []
        try:
            return json.loads(lineage_file.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
        except (json.JSONDecodeError, OSError):
            return []

    def _write_lineage_sync(
        self, task_id: str, name: str, entries: list[dict[str, object]]
    ) -> None:
        lineage_file = self._lineage_path(task_id, name)
        lineage_file.parent.mkdir(parents=True, exist_ok=True)
        lineage_file.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    async def save(
        self,
        task_id: str,
        name: str,
        artifact: BaseModel,
        *,
        produced_by: Provenance | None = None,
        inputs: Sequence[ArtifactRef] = (),
        expected_parent: str | None = None,
    ) -> ArtifactRef:
        def _save_sync() -> ArtifactRef:
            entries = self._read_lineage_sync(task_id, name)
            sha = compute_artifact_sha256(artifact)
            payload = artifact.model_dump_json(indent=2)
            provenance = produced_by or Provenance(agent="unknown")

            # Deduplication
            if entries:
                last_ref_raw = entries[-1].get("ref")
                if isinstance(last_ref_raw, dict) and last_ref_raw.get("sha256") == sha:
                    return ArtifactRef.model_validate(last_ref_raw)

            # Optimistic concurrency check
            if expected_parent is not None:
                if not entries:
                    raise ArtifactConcurrencyError(
                        f"Expected parent '{expected_parent}' but no prior versions exist for ({task_id}, {name})"
                    )
                last_ref_raw = entries[-1].get("ref")
                last_sha = last_ref_raw.get("sha256") if isinstance(last_ref_raw, dict) else None
                if last_sha != expected_parent:
                    raise ArtifactConcurrencyError(
                        f"Parent mismatch for ({task_id}, {name}): expected '{expected_parent}', current is '{last_sha}'"
                    )

            version = len(entries) + 1
            ref = ArtifactRef(task_id=task_id, name=name, version=version, sha256=sha)
            last_ref_raw = entries[-1].get("ref") if entries else None
            parent_sha = (
                str(last_ref_raw["sha256"])
                if isinstance(last_ref_raw, dict) and "sha256" in last_ref_raw
                else None
            )

            target_file = self._file_path(ref)
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(payload, encoding="utf-8")

            entries.append(
                {
                    "ref": ref.model_dump(),
                    "produced_by": provenance.model_dump(),
                    "inputs": [i.model_dump() for i in inputs],
                    "parent_sha256": parent_sha,
                }
            )
            self._write_lineage_sync(task_id, name, entries)
            return ref

        return await asyncio.to_thread(_save_sync)

    async def load[T: BaseModel](self, ref: ArtifactRef, model_cls: type[T]) -> T:
        def _load_sync() -> T:
            target_file = self._file_path(ref)
            if not target_file.is_file():
                raise ArtifactNotFoundError(
                    f"Artifact file not found for version {ref.version} ({ref.sha256[:8]}) at {target_file}"
                )
            return model_cls.model_validate_json(target_file.read_text(encoding="utf-8"))

        return await asyncio.to_thread(_load_sync)

    async def load_latest[T: BaseModel](self, task_id: str, name: str, model_cls: type[T]) -> T:
        lat = await self.latest(task_id, name)
        if lat is None:
            raise ArtifactNotFoundError(f"No artifact found for ({task_id}, {name})")
        return await self.load(lat, model_cls)

    async def latest(self, task_id: str, name: str) -> ArtifactRef | None:
        def _latest_sync() -> ArtifactRef | None:
            entries = self._read_lineage_sync(task_id, name)
            return ArtifactRef.model_validate(entries[-1]["ref"]) if entries else None

        return await asyncio.to_thread(_latest_sync)

    async def history(self, task_id: str, name: str) -> list[ArtifactRef]:
        def _history_sync() -> list[ArtifactRef]:
            entries = self._read_lineage_sync(task_id, name)
            return [ArtifactRef.model_validate(e["ref"]) for e in entries]

        return await asyncio.to_thread(_history_sync)

    async def exists(self, task_id: str, name: str) -> bool:
        def _exists_sync() -> bool:
            return bool(self._read_lineage_sync(task_id, name))

        return await asyncio.to_thread(_exists_sync)
