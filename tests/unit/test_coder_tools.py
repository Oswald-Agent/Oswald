"""Unit tests for Coder agent tools and security guards."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from oswald.core.agents.coder import CoderAgent
from oswald.core.llm.adapters.fake import FakeLLMClient
from oswald.core.llm.types import ToolCall
from oswald.core.sandbox.fake import FakeSandboxRuntime
from oswald.core.sandbox.models import SandboxConfig
from oswald.core.tools.coder import (
    CODER_TOOL_DEFINITIONS,
    check_declared_imports,
    delete_file,
    edit_file,
    is_protected_path,
    is_test_path,
    list_files,
    read_file_range,
    run_static_check,
    search_code,
)
from oswald.core.tools.coder.checks import ALLOWLISTED_COMMANDS, SAFE_CACHE_ENV
from oswald.core.vcs.workspace import FakeWorkspaceDiff


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


def test_edit_file_deletion_whitelisted(workspace: Path) -> None:
    """Verify delete_file and edit_file delete flag remove files within authorized whitelist."""
    # 1. Successful deletion via delete_file
    target = workspace / "src/pkg/obsolete.py"
    target.write_text("# Obsolete file\n", encoding="utf-8")
    assert target.exists() is True

    res = delete_file(
        workspace_root=workspace,
        file_path="src/pkg/obsolete.py",
        target_files_whitelist=["src/pkg/obsolete.py"],
    )
    assert res.success is True
    assert res.is_deleted is True
    assert target.exists() is False
    assert "Successfully deleted" in res.message

    # 2. Successful deletion via edit_file(delete=True)
    target2 = workspace / "src/pkg/deprecated.py"
    target2.write_text("# Deprecated file\n", encoding="utf-8")
    assert target2.exists() is True

    res2 = edit_file(
        workspace_root=workspace,
        file_path="src/pkg/deprecated.py",
        target_files_whitelist=["src/pkg/deprecated.py"],
        delete=True,
    )
    assert res2.success is True
    assert res2.is_deleted is True
    assert target2.exists() is False


def test_edit_file_deletion_guards(workspace: Path) -> None:
    """Verify security guards enforce whitelist, test protection, and existence on deletion."""
    # Guard 1: Whitelist rejection
    target = workspace / "src/pkg/unauthorized.py"
    target.write_text("# Unauthorized\n", encoding="utf-8")

    res = delete_file(
        workspace_root=workspace,
        file_path="src/pkg/unauthorized.py",
        target_files_whitelist=["src/pkg/other.py"],
    )
    assert res.success is False
    assert "PermissionDenied" in res.message
    assert target.exists() is True

    # Guard 2: Test file protection (Coder cannot delete test files)
    test_target = workspace / "tests/test_something.py"
    test_target.parent.mkdir(parents=True, exist_ok=True)
    test_target.write_text("# Test\n", encoding="utf-8")

    res_test = delete_file(
        workspace_root=workspace,
        file_path="tests/test_something.py",
        target_files_whitelist=["tests/test_something.py"],
    )
    assert res_test.success is False
    assert "PermissionDenied" in res_test.message
    assert test_target.exists() is True

    # Guard 3: Deleting nonexistent file fails cleanly
    res_missing = delete_file(
        workspace_root=workspace,
        file_path="src/pkg/nonexistent.py",
        target_files_whitelist=["src/pkg/nonexistent.py"],
    )
    assert res_missing.success is False
    assert "does not exist to be deleted" in res_missing.message


def test_edit_file_crlf_lf_fallback_normalization(workspace: Path) -> None:
    """Verify edit_file normalizes CRLF and LF differences while preserving file line endings."""
    # 1. CRLF file edited with LF old_str
    crlf_file = workspace / "src/pkg/windows.py"
    crlf_file.write_bytes(b"def alpha():\r\n    return 1\r\n\r\ndef beta():\r\n    return 2\r\n")

    res_crlf = edit_file(
        workspace_root=workspace,
        file_path="src/pkg/windows.py",
        old_str="def alpha():\n    return 1\n",
        new_str="def alpha():\n    return 42\n",
        target_files_whitelist=["src/pkg/windows.py"],
    )
    assert res_crlf.success is True
    updated_crlf = crlf_file.read_bytes()
    assert b"return 42\r\n" in updated_crlf
    assert b"\r\n" in updated_crlf
    assert b"def beta():\r\n    return 2\r\n" in updated_crlf

    # 2. LF file edited with CRLF old_str
    unix_file = workspace / "src/pkg/unix.py"
    unix_file.write_bytes(b"def first():\n    return 'a'\n\ndef second():\n    return 'b'\n")

    res_unix = edit_file(
        workspace_root=workspace,
        file_path="src/pkg/unix.py",
        old_str="def second():\r\n    return 'b'\r\n",
        new_str="def second():\r\n    return 'c'\r\n",
        target_files_whitelist=["src/pkg/unix.py"],
    )
    assert res_unix.success is True
    updated_unix = unix_file.read_bytes()
    assert b"return 'c'\n" in updated_unix
    assert b"\r\n" not in updated_unix

    # 3. Direct match single-line edit in CRLF file with LF new_str
    direct_crlf_file = workspace / "src/pkg/direct_crlf.py"
    direct_crlf_file.write_bytes(b"TAG = 1\r\nVALUE = 2\r\n")

    res_direct = edit_file(
        workspace_root=workspace,
        file_path="src/pkg/direct_crlf.py",
        old_str="TAG = 1",
        new_str="TAG = 1\nEXTRA = 99",
        target_files_whitelist=["src/pkg/direct_crlf.py"],
    )
    assert res_direct.success is True
    updated_direct = direct_crlf_file.read_bytes()
    assert updated_direct == b"TAG = 1\r\nEXTRA = 99\r\nVALUE = 2\r\n"


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


@pytest.mark.parametrize("check_name", ["ruff_check", "ruff_format", "mypy"])
async def test_missing_optional_checker_is_skipped(workspace: Path, check_name: str) -> None:
    sandbox = FakeSandboxRuntime(SandboxConfig(task_id="missing-tool", workspace_path=workspace))
    await sandbox.start()
    executable = ALLOWLISTED_COMMANDS[check_name][0]
    sandbox.queue_response(
        exit_code=127,
        stdout=f'OCI runtime exec failed: exec: "{executable}": executable file not found in $PATH',
    )
    result = await run_static_check(sandbox, check_name)
    assert result.skipped
    assert not result.passed
    assert executable in result.output


@pytest.mark.parametrize(
    ("check_name", "output"),
    [
        ("ruff_check", "main.py:1: F401 unused import"),
        ("mypy", "main.py:1: error: Incompatible types in assignment"),
        ("syntax_check", "SyntaxError: invalid syntax"),
        ("ruff_check", "error: Failed to parse configuration file"),
    ],
)
async def test_checker_failures_are_not_skipped(
    workspace: Path, check_name: str, output: str
) -> None:
    sandbox = FakeSandboxRuntime(SandboxConfig(task_id="failed-check", workspace_path=workspace))
    await sandbox.start()
    sandbox.queue_response(exit_code=1, stderr=output)
    result = await run_static_check(sandbox, check_name)
    assert not result.skipped
    assert not result.passed
    assert result.output == output


def test_syntax_check_keeps_bytecode_outside_workspace(workspace: Path) -> None:
    command = [sys.executable, *ALLOWLISTED_COMMANDS["syntax_check"][1:]]
    result = subprocess.run(  # noqa: S603
        command,
        cwd=workspace,
        env={**os.environ, **SAFE_CACHE_ENV},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(workspace.rglob("__pycache__"))
    assert not list(workspace.rglob("*.pyc"))

    (workspace / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    result = subprocess.run(  # noqa: S603
        command,
        cwd=workspace,
        env={**os.environ, **SAFE_CACHE_ENV},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "SyntaxError" in result.stdout + result.stderr


def test_is_protected_and_is_test_path() -> None:
    """Verify helper classification functions for protected and test paths."""
    assert is_protected_path("pyproject.toml") is True
    assert is_protected_path("uv.lock") is True
    assert is_protected_path(".github/workflows/ci.yml") is True
    assert is_protected_path("src/app.py") is False

    assert is_test_path("tests/unit/test_api.py") is True
    assert is_test_path("src/test_utils.py") is True
    assert is_test_path("src/pkg/service.py") is False


async def test_coder_agent_tool_dispatch_for_deletion(workspace: Path) -> None:
    """Verify CoderAgent._execute_tool_call handles both edit_file(delete=True) and delete_file."""
    agent = CoderAgent(
        llm=FakeLLMClient(),
        sandbox=FakeSandboxRuntime(SandboxConfig(task_id="t-del", workspace_path=workspace)),
        workspace_diff=FakeWorkspaceDiff(),
        workspace_root=workspace,
    )

    # 1. Dispatch delete_file tool call
    del_target = workspace / "src/pkg/obsolete_a.py"
    del_target.write_text("class Old: pass\n", encoding="utf-8")
    assert del_target.exists() is True

    call1 = ToolCall(
        id="c1",
        name="delete_file",
        arguments={"file_path": "src/pkg/obsolete_a.py"},
    )
    msg1, success1 = await agent._execute_tool_call(
        call1, target_files=["src/pkg/obsolete_a.py"], task_id="t-del"
    )
    assert success1 is True
    assert "Successfully deleted" in msg1
    assert del_target.exists() is False

    # 2. Dispatch edit_file with delete=True
    del_target2 = workspace / "src/pkg/obsolete_b.py"
    del_target2.write_text("class Old2: pass\n", encoding="utf-8")
    assert del_target2.exists() is True

    call2 = ToolCall(
        id="c2",
        name="edit_file",
        arguments={"file_path": "src/pkg/obsolete_b.py", "delete": True},
    )
    msg2, success2 = await agent._execute_tool_call(
        call2, target_files=["src/pkg/obsolete_b.py"], task_id="t-del"
    )
    assert success2 is True
    assert "Successfully deleted" in msg2
    assert del_target2.exists() is False
