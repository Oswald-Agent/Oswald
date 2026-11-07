"""Unit tests for ephemeral multi-stage composite Dockerfile generation and image builder."""

from unittest.mock import AsyncMock, patch

from oswald.core.sandbox.composite import (
    compute_composite_tag,
    ensure_composite_image,
    generate_composite_dockerfile,
)
from oswald.core.sandbox.manifest import (
    DEFAULT_ECOSYSTEMS,
    EcosystemConfig,
)


def test_compute_composite_tag_is_deterministic() -> None:
    """Image tag is consistently ordered regardless of input sequence."""
    node = next(e for e in DEFAULT_ECOSYSTEMS if e.name == "node")
    python = next(e for e in DEFAULT_ECOSYSTEMS if e.name == "python")

    tag1 = compute_composite_tag([node, python])
    tag2 = compute_composite_tag([python, node])
    assert tag1 == tag2
    assert tag1 == "oswald-composite:node-python"


def test_generate_composite_dockerfile_node_and_python() -> None:
    """Dockerfile merges Node and Python toolchains into debian:bookworm-slim with build-essential."""
    node = next(e for e in DEFAULT_ECOSYSTEMS if e.name == "node")
    python = next(e for e in DEFAULT_ECOSYSTEMS if e.name == "python")

    dockerfile = generate_composite_dockerfile([node, python])

    assert "FROM debian:bookworm-slim AS base" in dockerfile
    assert "build-essential" in dockerfile
    assert "COPY --from=node:20-bookworm-slim /usr/local/bin/node /usr/local/bin/node" in dockerfile
    assert "COPY --from=node:20-bookworm-slim /usr/local/bin/npm /usr/local/bin/npm" in dockerfile
    assert (
        "COPY --from=python:3.12-slim /usr/local/bin/python3 /usr/local/bin/python3" in dockerfile
    )
    assert (
        "COPY --from=python:3.12-slim /usr/local/lib/libpython3.12.so.1.0 /usr/local/lib/libpython3.12.so.1.0"
        in dockerfile
    )
    assert "RUN ldconfig" in dockerfile
    assert 'ENV PATH="' in dockerfile
    assert "/usr/local/bin" in dockerfile


def test_generate_composite_dockerfile_go_rust_zig() -> None:
    """Dockerfile dynamically sets toolchain environment variables without hardcoding language pairs."""
    go = next(e for e in DEFAULT_ECOSYSTEMS if e.name == "go")
    rust = next(e for e in DEFAULT_ECOSYSTEMS if e.name == "rust")
    zig = next(e for e in DEFAULT_ECOSYSTEMS if e.name == "zig")

    dockerfile = generate_composite_dockerfile([go, rust, zig])

    assert "COPY --from=golang:1.23-bookworm /usr/local/go /usr/local/go" in dockerfile
    assert "COPY --from=rust:1.80-bookworm /usr/local/cargo /usr/local/cargo" in dockerfile
    assert "COPY --from=ziglang/zig:latest /usr/local/bin/zig /usr/local/bin/zig" in dockerfile
    assert 'ENV GOROOT="/usr/local/go"' in dockerfile
    assert 'ENV CARGO_HOME="/usr/local/cargo"' in dockerfile
    assert "/usr/local/go/bin" in dockerfile
    assert "/usr/local/cargo/bin" in dockerfile


def test_generate_composite_dockerfile_with_custom_ecosystem() -> None:
    """Custom language ecosystem layers and paths are generated without modification."""
    custom = EcosystemConfig(
        name="customlang",
        manifest_files=("custom.build",),
        base_image="customlang:1.0-bookworm",
        package_manager="custompm",
        host_cache_path="~/.customcache",
        env_vars={"CUSTOM_HOME": "/opt/custom"},
        path_dirs=("/opt/custom/bin",),
        copy_stages=(("/opt/custom", "/opt/custom"),),
    )

    dockerfile = generate_composite_dockerfile([custom])
    assert "COPY --from=customlang:1.0-bookworm /opt/custom /opt/custom" in dockerfile
    assert 'ENV CUSTOM_HOME="/opt/custom"' in dockerfile
    assert "/opt/custom/bin" in dockerfile


async def test_ensure_composite_image_returns_tag_when_docker_missing() -> None:
    """ensure_composite_image returns deterministic tag gracefully if docker executable is absent."""
    node = next(e for e in DEFAULT_ECOSYSTEMS if e.name == "node")
    with patch("shutil.which", return_value=None):
        tag = await ensure_composite_image([node])
    assert tag == "oswald-composite:node"


async def test_ensure_composite_image_returns_existing_tag() -> None:
    """When docker image inspect succeeds, image build is skipped."""
    node = next(e for e in DEFAULT_ECOSYSTEMS if e.name == "node")
    python = next(e for e in DEFAULT_ECOSYSTEMS if e.name == "python")

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "oswald.core.sandbox.composite.run_docker_command",
            new_callable=AsyncMock,
            return_value=(0, "", ""),
        ) as mock_run_docker,
    ):
        tag = await ensure_composite_image([node, python])
        assert tag == "oswald-composite:node-python"
        # Only inspect was called, build was not called
        assert mock_run_docker.call_count == 1
        assert "inspect" in mock_run_docker.call_args.args[0]
