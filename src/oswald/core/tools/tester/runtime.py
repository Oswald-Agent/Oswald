"""Safe test authoring and command-execution tools for the Tester agent."""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import structlog

from oswald.core.llm.types import ToolDefinition
from oswald.core.sandbox.exceptions import SandboxError, SandboxTimeoutError
from oswald.core.sandbox.protocol import SandboxRuntime
from oswald.core.tools.coder.edit import is_protected_path
from oswald.core.tools.coder.read import (
    MAX_TOOL_OUTPUT_BYTES,
    normalize_workspace_rel_path,
    validate_workspace_path,
)

logger = structlog.get_logger(component="tools.tester")

_EXCLUDED_DIRS = {
    ".git",
    ".oswald",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "build",
    "dist",
    "htmlcov",
    ".coverage",
    ".tox",
    ".nox",
    ".hypothesis",
    "target",
    ".turbo",
    ".next",
    ".cache",
}

_EXCLUDED_FILE_PATTERNS = {
    ".coverage*",
    "*.coverage",
    "coverage.xml",
    "junit.xml",
    "test-results.xml",
    "report.xml",
    "report.html",
    ".hypothesis*",
}

_TEST_DIR_NAMES = {
    "test",
    "tests",
    "__tests__",
    "spec",
    "specs",
    "qa",
    "testing",
    "e2e",
    "cypress",
    "testcases",
    "integration",
    "t",
    "autotests",
    "verification",
    "acceptance",
    "benchmarks",
}

_KNOWN_SOURCE_ROOTS = {
    "src",
    "lib",
    "app",
    "pkg",
    "bin",
    "cmd",
    "internal",
    "source",
    "components",
}

_MAX_SNAPSHOT_FILE_BYTES = 10 * 1024 * 1024  # 10 MB per file limit for in-memory byte caching


def is_test_related_path(path: str) -> bool:
    """Recognize test paths across all language ecosystems and naming conventions."""
    clean = path.replace("\\", "/").strip().strip("/")
    candidate = Path(clean)
    name = candidate.name.lower()
    raw_name = candidate.name

    # 1. Known test configuration, runner, or setup manifests across stacks
    if (
        name
        in {
            "conftest.py",
            "tests.py",
            "pytest.ini",
            "runtests.jl",
            "setup_tests.py",
            "setup_test.py",
        }
        or fnmatch.fnmatch(name, "jest.config.*")
        or fnmatch.fnmatch(name, "vitest.config.*")
        or fnmatch.fnmatch(name, "jest.setup.*")
        or fnmatch.fnmatch(name, "vitest.setup.*")
        or fnmatch.fnmatch(name, "setupTests.*")
        or fnmatch.fnmatch(name, "setup_tests.*")
    ):
        return True

    # 2. Universal language-agnostic file naming patterns:
    #    Exact: test.*, tests.* (e.g. test.py, test.sh, tests.js)
    #    Prefix: test_*.*, test-*.*, Test*.* (e.g. test_parser.c, test-main.cpp, TestMath.cs)
    #    Suffix: *_test.*, *-test.*, *_spec.*, *-spec.* (e.g. server_test.go, client_spec.rb)
    #    Class/Type naming: *Test.*, *Tests.*, *TestCase.*, *TestSuite.*, *Spec.*, *Suite.*
    #    Infix/Double-extension: *.test.*, *.spec.* (e.g. App.test.tsx, route.spec.js)
    return (
        fnmatch.fnmatch(name, "test.*")
        or fnmatch.fnmatch(name, "tests.*")
        or fnmatch.fnmatch(name, "test_*.*")
        or fnmatch.fnmatch(name, "test-*.*")
        or fnmatch.fnmatch(name, "*_test.*")
        or fnmatch.fnmatch(name, "*-test.*")
        or fnmatch.fnmatch(name, "*_spec.*")
        or fnmatch.fnmatch(name, "*-spec.*")
        or fnmatch.fnmatch(name, "*.test.*")
        or fnmatch.fnmatch(name, "*.spec.*")
        or fnmatch.fnmatch(raw_name, "*Test.*")
        or fnmatch.fnmatch(raw_name, "*Tests.*")
        or fnmatch.fnmatch(raw_name, "*TestCase.*")
        or fnmatch.fnmatch(raw_name, "*TestSuite.*")
        or fnmatch.fnmatch(raw_name, "*Spec.*")
        or fnmatch.fnmatch(raw_name, "*Suite.*")
        or fnmatch.fnmatch(raw_name, "Test*.*")
    )


def is_excluded_test_artifact(filename: str) -> bool:
    """Check if filename is a temporary test artifact (e.g. coverage, junit xml)."""
    name = filename.replace("\\", "/").split("/")[-1]
    return any(fnmatch.fnmatch(name, pat) for pat in _EXCLUDED_FILE_PATTERNS)


@dataclass(frozen=True)
class TestEditResult:
    """Outcome of a test file authoring or modification operation."""

    success: bool
    file_path: str
    message: str


def validate_custom_test_root(
    workspace_root: Path,
    test_root: str,
    forbidden_paths: set[str] | None = None,
) -> tuple[str, str | None]:
    """Validate that a custom test root is safe and does not target source directories.

    Returns:
        (clean_root, error_message): error_message is None if valid.
    """
    clean_root = test_root.replace("\\", "/").strip().strip("/")
    while clean_root.startswith("./"):
        clean_root = clean_root.removeprefix("./").strip("/")

    if not clean_root or clean_root == ".":
        return "", "Invalid test_root: cannot declare root directory '.' as test root."

    try:
        norm_root = normalize_workspace_rel_path(workspace_root, clean_root)
    except PermissionError as exc:
        return "", f"SecurityError: {exc}"

    if norm_root in _KNOWN_SOURCE_ROOTS:
        return "", (
            f"PermissionDenied: '{norm_root}' is a recognized application source root. "
            "Tester cannot declare production source trees as custom test directories."
        )

    if is_protected_path(norm_root):
        return "", f"PermissionDenied: '{norm_root}' is a protected project directory."

    if forbidden_paths:
        for forbidden in forbidden_paths:
            f_clean = forbidden.replace("\\", "/").strip("/")
            if f_clean == norm_root or f_clean.startswith(f"{norm_root}/"):
                return "", (
                    f"PermissionDenied: Declared test root '{norm_root}' contains application source "
                    f"file '{f_clean}'. Cannot register a directory containing production code."
                )

    return norm_root, None


def write_test_file(
    workspace_root: Path,
    file_path: str,
    old_str: str,
    new_str: str,
    overwrite: bool = False,
    test_root: str = "",
    forbidden_paths: set[str] | list[str] | None = None,
) -> TestEditResult:
    """Create or make one unambiguous surgical replacement in a test file only.

    Guards enforced:
        1. Protected paths guard: Manifests, lockfiles, and CI are strictly off-limits.
        2. Source file isolation: Files targeted or modified during implementation cannot be touched.
        3. Existing file check: Pre-existing files cannot be edited unless they are recognized test files.
        4. Test root validation: Custom test roots must not encompass application source directories.
        5. Path traversal / Symlink guard: Paths must remain strictly within workspace root.
    """
    try:
        clean = normalize_workspace_rel_path(workspace_root, file_path)
        target = validate_workspace_path(workspace_root, clean)
    except (OSError, PermissionError) as exc:
        logger.warning("test_tool.security_error", file_path=file_path, error=str(exc))
        return TestEditResult(False, file_path, f"SecurityError: {exc}")

    # Convert forbidden paths to normalized set
    forbidden_set = {
        normalize_workspace_rel_path(workspace_root, p)
        for p in (forbidden_paths or [])
        if p and p.strip()
    }

    # Guard 1: Manifests / lockfiles / CI files are never editable by Tester
    if is_protected_path(clean):
        logger.warning("test_tool.protected_path_blocked", file_path=clean)
        return TestEditResult(
            False,
            clean,
            f"PermissionDenied: Tester cannot edit protected project file or manifest '{clean}'.",
        )

    # Guard 2: Source files modified or targeted by implementation are strictly forbidden
    if clean in forbidden_set:
        logger.warning("test_tool.source_file_blocked", file_path=clean)
        return TestEditResult(
            False,
            clean,
            f"PermissionDenied: '{clean}' is an application source file. "
            "Tester is strictly forbidden from modifying application source code.",
        )

    # Validate custom test root if provided
    clean_root = ""
    if test_root.strip():
        clean_root, err = validate_custom_test_root(workspace_root, test_root, forbidden_set)
        if err:
            logger.warning("test_tool.invalid_test_root", test_root=test_root, error=err)
            return TestEditResult(False, clean, err)

    is_under_declared_root = bool(clean_root) and (
        clean == clean_root or clean.startswith(f"{clean_root}/")
    )
    is_test_file = is_test_related_path(clean)

    # Guard 3: If file already exists, it MUST be a test file (cannot edit existing source)
    if target.exists() and not is_test_file and not is_under_declared_root:
        logger.warning("test_tool.existing_non_test_blocked", file_path=clean)
        return TestEditResult(
            False,
            clean,
            f"PermissionDenied: Existing file '{clean}' is not recognized as a test file. "
            "Tester is only permitted to author or edit test-related files.",
        )

    # Guard 4: If creating new file, it must be test-related or reside within a valid test root
    if not is_test_file and not is_under_declared_root:
        logger.warning("test_tool.non_test_creation_blocked", file_path=clean)
        return TestEditResult(
            False,
            clean,
            f"PermissionDenied: '{clean}' is not recognized as a test file. "
            "Tester may only author test files (e.g. tests/, *.test.*, test_*.py) or within a valid test_root.",
        )

    # File creation or overwrite path (old_str == "")
    if old_str == "":
        already_exists = target.exists()
        if already_exists and not overwrite:
            return TestEditResult(
                False,
                clean,
                f"Error: File '{clean}' already exists. Pass overwrite=True to replace or specify old_str for surgical edits.",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_str, encoding="utf-8")
        action = "Overwrote" if already_exists else "Created"
        logger.info("test_tool.file_written", file_path=clean, action=action)
        return TestEditResult(True, clean, f"{action} test file '{clean}'.")

    # Surgical file edit path (old_str != "")
    if not target.is_file():
        return TestEditResult(False, clean, f"Error: File '{clean}' does not exist.")

    try:
        content = target.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return TestEditResult(False, clean, f"Error reading '{clean}': {exc}")

    # Direct match check
    if old_str in content:
        matches = content.count(old_str)
        if matches != 1:
            return TestEditResult(
                False, clean, f"Error: old_str matches {matches} occurrences in '{clean}'."
            )
        updated_content = content.replace(old_str, new_str, 1)
    else:
        # Cross-platform newline normalization fallback (handles CRLF vs LF differences)
        norm_content = content.replace("\r\n", "\n")
        norm_old = old_str.replace("\r\n", "\n")
        norm_new = new_str.replace("\r\n", "\n")
        matches = norm_content.count(norm_old)
        if matches != 1:
            return TestEditResult(
                False, clean, f"Error: old_str matches {matches} occurrences in '{clean}'."
            )
        updated_norm = norm_content.replace(norm_old, norm_new, 1)
        # Restore CRLF if file originally used CRLF line endings
        updated_content = updated_norm.replace("\n", "\r\n") if "\r\n" in content else updated_norm

    target.write_bytes(updated_content.encode("utf-8"))
    logger.info("test_tool.file_updated", file_path=clean)
    return TestEditResult(True, clean, f"Updated test file '{clean}'.")


@dataclass(frozen=True)
class CommandResult:
    """Execution telemetry and output for an executed sandbox test command."""

    command: list[str]
    exit_code: int
    output: str
    duration_seconds: float
    execution_error: str | None = None


async def execute_test_command(
    sandbox: SandboxRuntime,
    command: list[str],
    timeout_seconds: float = 120.0,
) -> CommandResult:
    """Run an argument-vector command in the isolated sandbox without shell wrapping."""
    clean = [str(part) for part in command if str(part).strip()]
    if not clean:
        logger.warning("test_tool.empty_command")
        return CommandResult(
            [], 1, "PermissionDenied: command must contain at least one argument.", 0.0
        )

    try:
        result = await sandbox.exec_command(
            clean,
            timeout_seconds=timeout_seconds,
            env={
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": "/tmp/oswald-test-pycache",  # noqa: S108
            },
        )
    except SandboxTimeoutError as exc:
        logger.error("test_tool.command_timeout", command=clean, timeout=timeout_seconds)
        return CommandResult(
            clean, 124, f"Command timed out: {exc}", timeout_seconds, f"Command timed out: {exc}"
        )
    except SandboxError as exc:
        logger.error("test_tool.sandbox_error", command=clean, error=str(exc))
        return CommandResult(
            clean, 1, f"Sandbox execution error: {exc}", 0.0, f"Sandbox execution error: {exc}"
        )

    combined_output = f"{result.stdout}\n{result.stderr}".strip()
    output = combined_output or "Command completed with no output."
    encoded = output.encode("utf-8")
    if len(encoded) > MAX_TOOL_OUTPUT_BYTES:
        marker = "\n[... middle of command output omitted ...]\n"
        budget = MAX_TOOL_OUTPUT_BYTES - len(marker.encode("utf-8"))
        head_bytes = budget // 2
        output = (
            encoded[:head_bytes].decode("utf-8", errors="ignore")
            + marker
            + encoded[-(budget - head_bytes) :].decode("utf-8", errors="ignore")
        )
    logger.info(
        "test_tool.command_executed",
        command=clean,
        exit_code=result.exit_code,
        duration_seconds=result.duration_seconds,
    )
    return CommandResult(clean, result.exit_code, output, result.duration_seconds)


@dataclass(frozen=True)
class FileSnapshotInfo:
    """Stored metadata and content for a workspace file."""

    content: bytes | None
    mode: int
    sha256: str
    size: int


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """Pre-test workspace contents used to detect and revert unauthorized command mutations."""

    files: dict[str, FileSnapshotInfo]

    @classmethod
    def capture(cls, workspace_root: Path) -> WorkspaceSnapshot:
        """Capture the current state and contents of all files in workspace."""
        root = workspace_root.resolve()
        captured: dict[str, FileSnapshotInfo] = {}

        for current, dirs, names in os.walk(root):
            dirs[:] = [name for name in dirs if name not in _EXCLUDED_DIRS]
            for name in names:
                if is_excluded_test_artifact(name):
                    continue
                path = Path(current) / name
                if path.is_symlink():
                    continue

                try:
                    rel_posix = path.relative_to(root).as_posix()
                    st = path.stat()
                    file_size = st.st_size

                    # Hash file content
                    hasher = hashlib.sha256()
                    if file_size <= _MAX_SNAPSHOT_FILE_BYTES:
                        data = path.read_bytes()
                        hasher.update(data)
                        content_cache: bytes | None = data
                    else:
                        # Stream large files to avoid memory pressure
                        with path.open("rb") as f:
                            while chunk := f.read(65536):
                                hasher.update(chunk)
                        content_cache = None

                    captured[rel_posix] = FileSnapshotInfo(
                        content=content_cache,
                        mode=stat.S_IMODE(st.st_mode),
                        sha256=hasher.hexdigest(),
                        size=file_size,
                    )
                except OSError:
                    continue

        return cls(captured)

    def restore_non_test_changes(
        self,
        workspace_root: Path,
        test_roots: set[str] | None = None,
    ) -> list[str]:
        """Restore any non-test file changed or created since capture.

        Returns list of affected relative file paths that required restoration.
        """
        root = workspace_root.resolve()
        current_snapshot = WorkspaceSnapshot.capture(root)
        current = current_snapshot.files
        roots = test_roots or set()

        affected: list[str] = []
        all_paths = sorted(set(self.files) | set(current))

        for path in all_paths:
            # Skip test files and custom test root directories
            if is_test_related_path(path):
                continue
            if any(path == r or path.startswith(f"{r}/") for r in roots):
                continue
            if is_excluded_test_artifact(path):
                continue

            orig_info = self.files.get(path)
            curr_info = current.get(path)

            # Check if file was modified, created, or deleted
            if orig_info is None or curr_info is None or orig_info.sha256 != curr_info.sha256:
                affected.append(path)

        for path in affected:
            target = root / Path(path)
            orig_info = self.files.get(path)

            if orig_info is None:
                # File was newly created by an unauthorized test command -> delete it
                if target.is_file() or target.is_symlink():
                    try:
                        target.unlink()
                        logger.info("test_snapshot.unauthorized_file_removed", path=path)
                    except OSError as exc:
                        logger.error("test_snapshot.unlink_failed", path=path, error=str(exc))
            else:
                # File was modified or deleted -> restore original content and permissions
                if orig_info.content is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(orig_info.content)
                    if hasattr(os, "chmod"):
                        with contextlib.suppress(OSError):
                            target.chmod(orig_info.mode)
                    logger.info("test_snapshot.non_test_file_restored", path=path)

        return affected


TESTER_TOOL_DEFINITIONS: list[ToolDefinition] = [
    ToolDefinition(
        name="read_file_range",
        description="Read lines from a workspace file (1-indexed). Inspect existing code or tests.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative path to the workspace file",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Starting line number (1-indexed, inclusive)",
                    "default": 1,
                },
                "end_line": {
                    "type": "integer",
                    "description": "Ending line number (inclusive). Omit to read up to limit.",
                },
            },
            "required": ["file_path"],
        },
    ),
    ToolDefinition(
        name="search_code",
        description="Search for a literal string or regex pattern across workspace files.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "String or regex pattern to search for",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Optional glob pattern to filter files (e.g. '*.py', '*.ts')",
                },
                "is_regex": {
                    "type": "boolean",
                    "description": "Whether query is a regular expression",
                    "default": False,
                },
            },
            "required": ["query"],
        },
    ),
    ToolDefinition(
        name="list_files",
        description="List files inside a workspace directory.",
        parameters={
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Relative directory path (defaults to root '.')",
                    "default": ".",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Whether to search recursively into subdirectories",
                    "default": True,
                },
            },
        },
    ),
    ToolDefinition(
        name="read_symbol",
        description="Look up function, class, or method signature and definition across the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "symbol_name": {
                    "type": "string",
                    "description": "Name of the symbol (function, method, class) to look up",
                },
            },
            "required": ["symbol_name"],
        },
    ),
    ToolDefinition(
        name="write_test_file",
        description=(
            "Create a new test file or surgically edit an existing test file. "
            "Tester may only edit test files, never application source files. "
            "To create a new file, pass old_str=''. To edit, provide unique old_str to replace with new_str."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative path to the test file (e.g. 'tests/test_feature.py')",
                },
                "old_str": {
                    "type": "string",
                    "description": "Exact text to replace. Pass '' when creating a new file.",
                },
                "new_str": {
                    "type": "string",
                    "description": "New content to insert or replace with.",
                },
                "test_root": {
                    "type": "string",
                    "description": "Optional custom test directory (e.g. 'qa', 'e2e') if non-standard.",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Whether to overwrite the entire file if it already exists (when old_str='').",
                    "default": False,
                },
            },
            "required": ["file_path", "old_str", "new_str"],
        },
    ),
    ToolDefinition(
        name="run_diagnostic_command",
        description="Inspect runtime versions or installed tools. Diagnostic results never count as test results. Git inspection uses the supplied diff, not sandbox commands.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 600, "default": 30},
            },
            "required": ["command"],
        },
    ),
    ToolDefinition(
        name="run_test_command",
        description="Execute a test runner command as argument vector argv in the isolated sandbox.",
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Argv list of command and arguments (e.g. ['pytest', 'tests/test_app.py'])",
                },
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 600,
                    "default": 120.0,
                    "description": "Maximum seconds before execution aborts",
                },
            },
            "required": ["command"],
        },
    ),
]
