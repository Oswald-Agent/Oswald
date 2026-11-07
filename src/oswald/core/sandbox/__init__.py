"""Core sandbox package.

Responsibility:
    SandboxRuntime Protocol and container execution implementations.

Forbidden imports:
    Must not import from `oswald.api`, `oswald.workers`, or `oswald.db`.
"""

from oswald.core.sandbox.composite import (
    ensure_composite_image,
    generate_composite_dockerfile,
)
from oswald.core.sandbox.docker_runtime import DockerSandboxRuntime
from oswald.core.sandbox.exceptions import (
    ContainerNotRunningError,
    SandboxError,
    SandboxTimeoutError,
)
from oswald.core.sandbox.fake import FakeSandboxRuntime
from oswald.core.sandbox.manifest import (
    EcosystemConfig,
    ManifestRegistry,
    compute_composite_tag,
    default_manifest_registry,
    resolve_execution_image,
)
from oswald.core.sandbox.models import ExecResult, SandboxConfig, SandboxStatus
from oswald.core.sandbox.protocol import SandboxRuntime

__all__ = [
    "ContainerNotRunningError",
    "DockerSandboxRuntime",
    "EcosystemConfig",
    "ExecResult",
    "FakeSandboxRuntime",
    "ManifestRegistry",
    "SandboxConfig",
    "SandboxError",
    "SandboxRuntime",
    "SandboxStatus",
    "SandboxTimeoutError",
    "compute_composite_tag",
    "default_manifest_registry",
    "ensure_composite_image",
    "generate_composite_dockerfile",
    "resolve_execution_image",
]
