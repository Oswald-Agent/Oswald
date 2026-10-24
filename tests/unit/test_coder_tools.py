"""Unit tests for Coder agent tools and security guards."""

import os
from pathlib import Path

import pytest

from oswald.core.sandbox.fake import FakeSandboxRuntime
from oswald.core.sandbox.models import SandboxConfig
from oswald.core.tools.coder import (
    CODER_TOOL_DEFINITIONS,
    check_declared_imports,
    edit_file,
    is_protected_path,
    is_test_path,
    list_files,
    read_file_range,
    run_static_check,
    search_code,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Fixture providing a mock repository workspace."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    # Create standard files
    src_dir = ws / "src" / "pkg"
    src_dir.mkdir(parents=True)
    (src_dir / "app.py").write_text(
        "import os\n\ndef add(a: int, b: int) -> int:\n    return a + b\n",
        encoding="utf-8",
    )
    (src_dir / "utils.py").write_text(
        "def helper() -> str:\n    return 'help'\n",
        encoding="utf-8",
    )

    # Manifest
    (ws / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = ["pydantic>=2.0"]\n',
        encoding="utf-8",
    )

    # Excluded directories
    git_dir = ws / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("git config", encoding="utf-8")

    return ws


def test_tool_definitions_validity() -> None:
    """Verify tool definitions are well-formed and names are unique."""
    names = [t.name for t in CODER_TOOL_DEFINITIONS]
    assert len(names) == len(set(names))
    assert "read_file_range" in names
    assert "edit_file" in names
    assert "run_static_check" in names
    assert "check_declared_imports" in names


def test_read_file_range(workspace: Path) -> None:
    """Verify read_file_range returns numbered lines and honors bounds."""
    res = read_file_range(workspace, "src/pkg/app.py", start_line=1, end_line=3)
    assert "1 | import os" in res
    assert "3 | def add" in res

    # Missing file
    missing = read_file_range(workspace, "missing.py")
    assert "does not exist" in missing

    # Path traversal attempt
    with pytest.raises(PermissionError):
        read_file_range(workspace, "../outside.txt")


def test_list_files_and_exclusions(workspace: Path) -> None:
    """Verify list_files excludes internal dirs (.git, .venv)."""
    output = list_files(workspace, directory=".", recursive=True)
    assert "src/pkg/app.py" in output
    assert "src/pkg/utils.py" in output
    assert "pyproject.toml" in output
    assert ".git" not in output


def test_search_code_literal_and_regex(workspace: Path) -> None:
    """Verify search_code finds matching occurrences across files."""
    lit = search_code(workspace, "return a + b")
    assert "src/pkg/app.py:4" in lit

    reg = search_code(workspace, r"def \w+\(", is_regex=True)
    assert "src/pkg/app.py:3" in reg
    assert "src/pkg/utils.py:1" in reg

    no_match = search_code(workspace, "nonexistent_string_12345")
    assert "No matches found" in no_match


def test_edit_file_whitelist_guard(workspace: Path) -> None:
    """Verify edit_file rejects paths not in target_files whitelist."""
    res = edit_file(
        workspace_root=workspace,
        file_path="src/pkg/app.py",
        old_str="return a + b",
        new_str="return a - b",
        target_files_whitelist=["src/pkg/other.py"],
    )
    assert res.success is False
    assert "PermissionDenied" in res.message
    assert "not in authorized target_files whitelist" in res.message


def test_edit_file_test_path_guard(workspace: Path) -> None:
    """Verify edit_file rejects modifications to test files (D1)."""
    res = edit_file(
        workspace_root=workspace,
        file_path="tests/test_demo.py",
        old_str="",
        new_str="def test_foo(): pass",
        target_files_whitelist=["tests/test_demo.py"],
    )
    assert res.success is False
    assert "PermissionDenied" in res.message
    assert "forbidden from creating or modifying test files" in res.message


def test_edit_file_symlink_guard(workspace: Path, tmp_path: Path) -> None:
    """Verify edit_file detects and blocks symlinks escaping the workspace."""
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    outside_file = outside_dir / "target.py"
    outside_file.write_text("secret = True\n", encoding="utf-8")

    # Create symlink pointing outside workspace
    escape_link = workspace / "escaped_link.py"
    try:
        os.symlink(outside_file, escape_link)
    except OSError:
        pytest.skip("Symlinks not supported on this filesystem")

    res = edit_file(
        workspace_root=workspace,
        file_path="escaped_link.py",
        old_str="secret = True",
        new_str="secret = False",
        target_files_whitelist=["escaped_link.py"],
    )
    assert res.success is False
    assert "SecurityError" in res.message or "Symlink traversal" in res.message


def test_edit_file_creation_and_replacement(workspace: Path) -> None:
    """Verify file creation with empty old_str and replacement of existing content."""
    # 1. Create a new file
    create_res = edit_file(
        workspace_root=workspace,
        file_path="src/pkg/new_module.py",
        old_str="",
        new_str="# New module content\n",
        target_files_whitelist=["src/pkg/new_module.py"],
    )
    assert create_res.success is True
    assert (workspace / "src/pkg/new_module.py").read_text() == "# New module content\n"

    # 2. Modify existing file
    mod_res = edit_file(
        workspace_root=workspace,
        file_path="src/pkg/new_module.py",
        old_str="# New module content\n",
        new_str="# Updated module content\n",
        target_files_whitelist=["src/pkg/new_module.py"],
    )
    assert mod_res.success is True
    assert (workspace / "src/pkg/new_module.py").read_text() == "# Updated module content\n"

    # 3. Mismatched old_str
    err_res = edit_file(
        workspace_root=workspace,
        file_path="src/pkg/new_module.py",
        old_str="non_existent_old_str",
        new_str="won't work",
        target_files_whitelist=["src/pkg/new_module.py"],
    )
    assert err_res.success is False
    assert "old_str was not found" in err_res.message


def test_check_declared_imports(workspace: Path) -> None:
    """Verify check_declared_imports warns about undeclared third-party packages."""
    # Add a file importing declared (pydantic), stdlib (os, sys), and undeclared (requests)
    target_py = workspace / "src" / "pkg" / "network.py"
    target_py.write_text(
        "import sys\nimport os\nfrom pydantic import BaseModel\nimport requests\n",
        encoding="utf-8",
    )

    warnings = check_declared_imports(workspace, ["src/pkg/network.py"])
    assert len(warnings) == 1
    assert "Undeclared dependency 'requests'" in warnings[0]


@pytest.mark.asyncio
async def test_run_static_check_allowlist_and_forbids_tests(workspace: Path) -> None:
    """Verify run_static_check runs allowlisted checks and rejects tests or arbitrary commands."""
    config = SandboxConfig(task_id="task-01", workspace_path=workspace)
    fake_sandbox = FakeSandboxRuntime(config)
    await fake_sandbox.start()

    # Allowlisted check
    fake_sandbox.queue_response(exit_code=0, stdout="All checks passed!\n")
    res = await run_static_check(fake_sandbox, "ruff_check")
    assert res.passed is True
    assert res.exit_code == 0
    assert "All checks passed" in res.output
    assert fake_sandbox.executed_commands[-1] == ["ruff", "check", "."]

    # Forbidden test execution (pytest)
    test_res = await run_static_check(fake_sandbox, "pytest")
    assert test_res.passed is False
    assert "PermissionDenied" in test_res.output

    # Forbidden arbitrary command
    sh_res = await run_static_check(fake_sandbox, "bash")
    assert sh_res.passed is False
    assert "PermissionDenied" in sh_res.output

    await fake_sandbox.stop()


def test_is_protected_and_is_test_path() -> None:
    """Verify helper classification functions for protected and test paths."""
    assert is_protected_path("pyproject.toml") is True
    assert is_protected_path("uv.lock") is True
    assert is_protected_path(".github/workflows/ci.yml") is True
    assert is_protected_path("src/app.py") is False

    assert is_test_path("tests/unit/test_api.py") is True
    assert is_test_path("src/test_utils.py") is True
    assert is_test_path("src/pkg/service.py") is False
