"""Extensible, language-agnostic manifest registry and toolchain environment resolver.

Maps detected repository root manifests to official Debian-bookworm container images,
package managers, and toolchain environment paths (PATH, GOROOT, CARGO_HOME).
Defaults to python:3.12-slim when no root manifests are detected.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class EcosystemConfig:
    """Configuration contract for a detected programming language ecosystem."""

    name: str
    manifest_files: tuple[str, ...]
    base_image: str
    package_manager: str
    host_cache_path: str
    env_vars: dict[str, str] = field(default_factory=dict)
    path_dirs: tuple[str, ...] = field(default_factory=tuple)
    copy_stages: tuple[tuple[str, str], ...] = field(default_factory=tuple)


DEFAULT_ECOSYSTEMS: tuple[EcosystemConfig, ...] = (
    EcosystemConfig(
        name="node",
        manifest_files=("package.json",),
        base_image="node:20-bookworm-slim",
        package_manager="npm",
        host_cache_path="~/.npm",
        env_vars={
            "NODE_ENV": "development",
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        },
        path_dirs=("/usr/local/bin",),
        copy_stages=(
            ("/usr/local/bin/node", "/usr/local/bin/node"),
            ("/usr/local/bin/npm", "/usr/local/bin/npm"),
            ("/usr/local/bin/npx", "/usr/local/bin/npx"),
            ("/usr/local/lib/node_modules", "/usr/local/lib/node_modules"),
            ("/usr/local/include/node", "/usr/local/include/node"),
        ),
    ),
    EcosystemConfig(
        name="python",
        manifest_files=("pyproject.toml", "requirements.txt"),
        base_image="python:3.12-slim",
        package_manager="pip",
        host_cache_path="~/.cache/pip",
        env_vars={
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        path_dirs=("/usr/local/bin",),
        copy_stages=(
            ("/usr/local/bin/python3", "/usr/local/bin/python3"),
            ("/usr/local/bin/python", "/usr/local/bin/python"),
            ("/usr/local/bin/pip", "/usr/local/bin/pip"),
            ("/usr/local/bin/pip3", "/usr/local/bin/pip3"),
            ("/usr/local/lib/python3.12", "/usr/local/lib/python3.12"),
            ("/usr/local/include/python3.12", "/usr/local/include/python3.12"),
        ),
    ),
    EcosystemConfig(
        name="go",
        manifest_files=("go.mod",),
        base_image="golang:1.23-bookworm",
        package_manager="go",
        host_cache_path="~/go/pkg/mod",
        env_vars={
            "GOROOT": "/usr/local/go",
            "GOPATH": "/go",
        },
        path_dirs=("/usr/local/go/bin", "/go/bin"),
        copy_stages=(("/usr/local/go", "/usr/local/go"),),
    ),
    EcosystemConfig(
        name="rust",
        manifest_files=("Cargo.toml",),
        base_image="rust:1.80-bookworm",
        package_manager="cargo",
        host_cache_path="~/.cargo/registry",
        env_vars={
            "CARGO_HOME": "/usr/local/cargo",
            "RUSTUP_HOME": "/usr/local/rustup",
        },
        path_dirs=("/usr/local/cargo/bin",),
        copy_stages=(
            ("/usr/local/cargo", "/usr/local/cargo"),
            ("/usr/local/rustup", "/usr/local/rustup"),
        ),
    ),
    EcosystemConfig(
        name="zig",
        manifest_files=("build.zig",),
        base_image="ziglang/zig:latest",
        package_manager="zig",
        host_cache_path="~/.cache/zig",
        env_vars={},
        path_dirs=("/usr/local/bin",),
        copy_stages=(("/usr/local/bin/zig", "/usr/local/bin/zig"),),
    ),
)


class ManifestRegistry:
    """Registry mapping detected repository root manifests to toolchains and base images."""

    DEFAULT_IMAGE: str = "python:3.12-slim"

    def __init__(self, ecosystems: Sequence[EcosystemConfig] | None = None) -> None:
        self._ecosystems: list[EcosystemConfig] = (
            list(ecosystems) if ecosystems is not None else list(DEFAULT_ECOSYSTEMS)
        )

    def register(self, ecosystem: EcosystemConfig) -> None:
        """Register a new or custom language ecosystem configuration."""
        self._ecosystems = [e for e in self._ecosystems if e.name != ecosystem.name]
        self._ecosystems.append(ecosystem)

    def get_ecosystems(self) -> list[EcosystemConfig]:
        """Return all registered ecosystem configurations."""
        return list(self._ecosystems)

    def detect_ecosystems(self, root: Path) -> list[EcosystemConfig]:
        """Detect which language ecosystems are present at the workspace root."""
        matched: list[EcosystemConfig] = []
        for ecosystem in self._ecosystems:
            for manifest_name in ecosystem.manifest_files:
                candidate = root / manifest_name
                if candidate.is_file():
                    matched.append(ecosystem)
                    break
        return matched

    def resolve_environment_variables(
        self,
        ecosystems: Sequence[EcosystemConfig],
        existing_path: str = "/usr/local/bin:/usr/bin:/bin",
    ) -> dict[str, str]:
        """Compute merged environment variables and PATH for all specified ecosystems."""
        env: dict[str, str] = {}
        paths: list[str] = []
        for ecosystem in ecosystems:
            env.update(ecosystem.env_vars)
            for p in ecosystem.path_dirs:
                if p not in paths:
                    paths.append(p)
        for base_p in existing_path.split(":"):
            if base_p and base_p not in paths:
                paths.append(base_p)
        env["PATH"] = ":".join(paths)
        return env

    def resolve_single_image(self, root: Path) -> str:
        """Resolve container image for single-stack workspace or default to python:3.12-slim."""
        detected = self.detect_ecosystems(root)
        if len(detected) == 1:
            return detected[0].base_image
        return self.DEFAULT_IMAGE


default_manifest_registry = ManifestRegistry()
