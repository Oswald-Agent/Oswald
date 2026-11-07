"""Unit tests for the language-agnostic root manifest registry and toolchain environment resolver."""

from pathlib import Path

from oswald.core.sandbox.manifest import (
    EcosystemConfig,
    ManifestRegistry,
    default_manifest_registry,
)


def test_empty_workspace_defaults_to_python_slim(tmp_path: Path) -> None:
    """When no root manifests are detected, resolve to python:3.12-slim."""
    detected = default_manifest_registry.detect_ecosystems(tmp_path)
    assert detected == []
    image = default_manifest_registry.resolve_single_image(tmp_path)
    assert image == "python:3.12-slim"


def test_detect_node_manifest(tmp_path: Path) -> None:
    """package.json maps to Node ecosystem, node:20-bookworm-slim image, and npm manager."""
    (tmp_path / "package.json").write_text("{}")
    detected = default_manifest_registry.detect_ecosystems(tmp_path)
    assert len(detected) == 1
    assert detected[0].name == "node"
    assert detected[0].base_image == "node:20-bookworm-slim"
    assert detected[0].package_manager == "npm"
    assert default_manifest_registry.resolve_single_image(tmp_path) == "node:20-bookworm-slim"


def test_detect_python_manifests(tmp_path: Path) -> None:
    """pyproject.toml and requirements.txt map to Python ecosystem and python:3.12-slim image."""
    pyproject_dir = tmp_path / "pyproject_repo"
    pyproject_dir.mkdir()
    (pyproject_dir / "pyproject.toml").write_text("[project]")

    detected = default_manifest_registry.detect_ecosystems(pyproject_dir)
    assert len(detected) == 1
    assert detected[0].name == "python"
    assert detected[0].base_image == "python:3.12-slim"
    assert detected[0].package_manager == "pip"

    reqs_dir = tmp_path / "reqs_repo"
    reqs_dir.mkdir()
    (reqs_dir / "requirements.txt").write_text("pytest\n")

    detected_reqs = default_manifest_registry.detect_ecosystems(reqs_dir)
    assert len(detected_reqs) == 1
    assert detected_reqs[0].name == "python"


def test_detect_go_manifest(tmp_path: Path) -> None:
    """go.mod maps to Go ecosystem, golang:1.23-bookworm image, and GOROOT environment."""
    (tmp_path / "go.mod").write_text("module example.com/app\n\ngo 1.23\n")
    detected = default_manifest_registry.detect_ecosystems(tmp_path)
    assert len(detected) == 1
    assert detected[0].name == "go"
    assert detected[0].base_image == "golang:1.23-bookworm"
    assert detected[0].package_manager == "go"
    assert detected[0].env_vars.get("GOROOT") == "/usr/local/go"

    env = default_manifest_registry.resolve_environment_variables(detected)
    assert env.get("GOROOT") == "/usr/local/go"
    assert "/usr/local/go/bin" in env.get("PATH", "")


def test_detect_rust_manifest(tmp_path: Path) -> None:
    """Cargo.toml maps to Rust ecosystem, rust:1.80-bookworm image, and CARGO_HOME environment."""
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "app"\n')
    detected = default_manifest_registry.detect_ecosystems(tmp_path)
    assert len(detected) == 1
    assert detected[0].name == "rust"
    assert detected[0].base_image == "rust:1.80-bookworm"
    assert detected[0].package_manager == "cargo"
    assert detected[0].env_vars.get("CARGO_HOME") == "/usr/local/cargo"

    env = default_manifest_registry.resolve_environment_variables(detected)
    assert env.get("CARGO_HOME") == "/usr/local/cargo"
    assert "/usr/local/cargo/bin" in env.get("PATH", "")


def test_detect_zig_manifest(tmp_path: Path) -> None:
    """build.zig maps to Zig ecosystem and ziglang/zig:latest image."""
    (tmp_path / "build.zig").write_text('const std = @import("std");\n')
    detected = default_manifest_registry.detect_ecosystems(tmp_path)
    assert len(detected) == 1
    assert detected[0].name == "zig"
    assert detected[0].base_image == "ziglang/zig:latest"
    assert detected[0].package_manager == "zig"
    assert default_manifest_registry.resolve_single_image(tmp_path) == "ziglang/zig:latest"


def test_resolve_environment_variables_combines_paths(tmp_path: Path) -> None:
    """resolve_environment_variables combines multiple toolchain PATHs and variables cleanly."""
    (tmp_path / "go.mod").write_text("module example\n")
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    detected = default_manifest_registry.detect_ecosystems(tmp_path)
    assert len(detected) == 2

    env = default_manifest_registry.resolve_environment_variables(detected)
    assert env["GOROOT"] == "/usr/local/go"
    assert env["CARGO_HOME"] == "/usr/local/cargo"
    path = env["PATH"]
    assert "/usr/local/go/bin" in path
    assert "/usr/local/cargo/bin" in path


def test_manifest_registry_extensibility(tmp_path: Path) -> None:
    """Custom language ecosystems can be registered without modifying core code."""
    registry = ManifestRegistry()
    elixir = EcosystemConfig(
        name="elixir",
        manifest_files=("mix.exs",),
        base_image="elixir:1.17-slim",
        package_manager="mix",
        host_cache_path="~/.hex",
        env_vars={"MIX_ENV": "dev"},
        path_dirs=("/usr/local/bin",),
    )
    registry.register(elixir)

    (tmp_path / "mix.exs").write_text("defmodule App.MixProject do\nend\n")
    detected = registry.detect_ecosystems(tmp_path)
    assert len(detected) == 1
    assert detected[0].name == "elixir"
    assert detected[0].base_image == "elixir:1.17-slim"
    assert registry.resolve_single_image(tmp_path) == "elixir:1.17-slim"
