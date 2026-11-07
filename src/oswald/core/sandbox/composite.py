"""Ephemeral multi-stage composite Dockerfile generation and image builder for polyglot workspaces.

Merges language toolchains from official Debian-bookworm images into debian:bookworm-slim
with build-essential, setting appropriate toolchain environment variables without hardcoding
language pairs.
"""

import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

import structlog

from oswald.core.sandbox.docker_runtime import run_docker_command
from oswald.core.sandbox.manifest import EcosystemConfig, default_manifest_registry


def compute_composite_tag(ecosystems: Sequence[EcosystemConfig]) -> str:
    """Compute deterministic image tag for a combination of ecosystems."""
    names = sorted(e.name for e in ecosystems)
    return f"oswald-composite:{'-'.join(names)}"


def generate_composite_dockerfile(
    ecosystems: Sequence[EcosystemConfig],
    base_image: str = "debian:bookworm-slim",
) -> str:
    """Generate ephemeral multi-stage Dockerfile merging toolchains from official images into base_image."""
    lines: list[str] = [
        f"FROM {base_image} AS base",
        "ENV DEBIAN_FRONTEND=noninteractive",
        "RUN apt-get update && apt-get install -y --no-install-recommends \\",
        "    build-essential \\",
        "    ca-certificates \\",
        "    git \\",
        "    curl \\",
        "    && rm -rf /var/lib/apt/lists/*",
        "",
    ]

    for eco in ecosystems:
        if eco.copy_stages:
            lines.append(f"# Toolchain layers for {eco.name}")
            for src, dst in eco.copy_stages:
                lines.append(f"COPY --from={eco.base_image} {src} {dst}")
            lines.append("")

    env = default_manifest_registry.resolve_environment_variables(ecosystems)
    if env:
        lines.append("# Toolchain environment variables and paths")
        for k, v in sorted(env.items()):
            lines.append(f'ENV {k}="{v}"')
        lines.append("")

    return "\n".join(lines)


async def ensure_composite_image(
    ecosystems: Sequence[EcosystemConfig],
    tag: str | None = None,
    docker_bin: str = "docker",
) -> str:
    """Ensure composite container image exists locally, building it ephemerally if missing."""
    image_tag = tag or compute_composite_tag(ecosystems)
    if not shutil.which(docker_bin):
        return image_tag

    inspect_code, _, _ = await run_docker_command(
        ["image", "inspect", image_tag], docker_bin=docker_bin
    )
    if inspect_code == 0:
        return image_tag

    dockerfile_content = generate_composite_dockerfile(ecosystems)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            df_path = Path(tmpdir) / "Dockerfile"
            df_path.write_text(dockerfile_content, encoding="utf-8")
            build_code, _, err = await run_docker_command(
                ["build", "-t", image_tag, "-f", str(df_path), tmpdir],
                docker_bin=docker_bin,
            )
            if build_code != 0:
                logger = structlog.get_logger()
                logger.warning(
                    "composite_image.build_failed",
                    tag=image_tag,
                    error=err.strip(),
                )
    except Exception as exc:
        logger = structlog.get_logger()
        logger.warning("composite_image.build_exception", tag=image_tag, error=str(exc))

    return image_tag
