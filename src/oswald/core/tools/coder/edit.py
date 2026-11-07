"""Surgical file edit tool for Coder agent with strict whitelist, test-path, and protected-path guards."""

import fnmatch
from dataclasses import dataclass
from pathlib import Path

from oswald.core.tools.coder.read import (
    normalize_workspace_rel_path,
    validate_workspace_path,
)

PROTECTED_PATTERNS = [
    "pyproject.toml",
    "requirements*.txt",
    "package.json",
    "*.lock",
    "uv.lock",
    "package-lock.json",
    "poetry.lock",
    "Dockerfile*",
    ".github/*",
    ".github/**/*",
    ".env*",
    ".gitignore",
]


@dataclass(frozen=True)
class EditResult:
    """Result of an edit_file execution."""

    success: bool
    file_path: str
    message: str
    is_protected: bool = False
    is_new_file: bool = False
    is_deleted: bool = False


def is_test_path(rel_path: str) -> bool:
    """Return True if path is a test file or located within a tests directory."""
    clean = rel_path.strip()
    while clean.startswith("./") or clean.startswith("/"):
        if clean.startswith("./"):
            clean = clean.removeprefix("./")
        if clean.startswith("/"):
            clean = clean.removeprefix("/")
    p = Path(clean)
    parts = p.parts
    if "tests" in parts or "test" in parts:
        return True
    if p.name in ("conftest.py", "tests.py"):
        return True
    return fnmatch.fnmatch(p.name, "test_*.py") or fnmatch.fnmatch(p.name, "*_test.py")


def is_protected_path(rel_path: str) -> bool:
    """Return True if path matches any sensitive dependency, lockfile, or CI configuration."""
    clean_path = rel_path.strip()
    while clean_path.startswith("./"):
        clean_path = clean_path.removeprefix("./")
    p = Path(clean_path)
    parts = p.parts

    if ".github" in parts or any(part.startswith(".env") for part in parts):
        return True

    filename = p.name
    for pattern in PROTECTED_PATTERNS:
        if fnmatch.fnmatch(clean_path, pattern) or fnmatch.fnmatch(filename, pattern):
            return True
    return False


def edit_file(
    workspace_root: Path,
    file_path: str,
    old_str: str = "",
    new_str: str = "",
    target_files_whitelist: list[str] | None = None,
    *,
    delete: bool = False,
) -> EditResult:
    """Surgically modify, create, or delete a file inside the task worktree.

    Guards enforced:
        1. Whitelist guard: file_path must be in target_files_whitelist.
        2. Test guard: Coder agent is strictly forbidden from editing tests (D1).
        3. Protected paths guard: Manifests/lockfiles/CI require explicit whitelist inclusion.
        4. Symlink/Traversal guard: Target and parents must remain within workspace.
    """
    try:
        clean_path = normalize_workspace_rel_path(workspace_root, file_path)
    except PermissionError as exc:
        return EditResult(
            success=False,
            file_path=file_path,
            message=f"SecurityError: {exc}",
        )

    clean_whitelist = [
        normalize_workspace_rel_path(workspace_root, f)
        for f in (target_files_whitelist if target_files_whitelist is not None else [])
    ]

    # Guard 1: Whitelist check
    if clean_path not in clean_whitelist:
        return EditResult(
            success=False,
            file_path=clean_path,
            message=(
                f"PermissionDenied: File '{clean_path}' is not in authorized target_files whitelist: "
                f"{clean_whitelist}. Coder cannot touch files outside the planned scope."
            ),
        )

    # Guard 2: Test path check
    if is_test_path(clean_path):
        return EditResult(
            success=False,
            file_path=clean_path,
            message=(
                f"PermissionDenied: Coder agent is forbidden from creating or modifying test files ('{clean_path}'). "
                f"Only the Tester agent authors and edits tests."
            ),
        )

    # Guard 3: Protected path check
    is_protected = is_protected_path(clean_path)

    # Guard 4: Workspace path containment & symlink check
    try:
        target = validate_workspace_path(workspace_root, clean_path)
    except (PermissionError, OSError) as exc:
        return EditResult(
            success=False,
            file_path=clean_path,
            message=f"SecurityError: {exc}",
        )

    # Explicit file deletion
    if delete:
        if not target.exists():
            return EditResult(
                success=False,
                file_path=clean_path,
                message=f"Error: File '{clean_path}' does not exist to be deleted.",
                is_protected=is_protected,
            )
        if not target.is_file():
            return EditResult(
                success=False,
                file_path=clean_path,
                message=f"Error: '{clean_path}' is not a file to be deleted.",
                is_protected=is_protected,
            )
        try:
            target.unlink()
        except OSError as exc:
            return EditResult(
                success=False,
                file_path=clean_path,
                message=f"FileSystemError deleting file '{clean_path}': {exc}",
                is_protected=is_protected,
            )
        return EditResult(
            success=True,
            file_path=clean_path,
            message=f"Successfully deleted file '{clean_path}'",
            is_protected=is_protected,
            is_deleted=True,
        )

    # Surgical replacement or creation
    if old_str == "":
        # File creation mode
        if target.exists():
            return EditResult(
                success=False,
                file_path=clean_path,
                message=f"Error: File '{clean_path}' already exists. Specify old_str to replace existing content.",
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new_str, encoding="utf-8")
        except OSError as exc:
            return EditResult(
                success=False,
                file_path=clean_path,
                message=f"FileSystemError creating file '{clean_path}': {exc}",
            )
        return EditResult(
            success=True,
            file_path=clean_path,
            message=f"Successfully created file '{clean_path}'",
            is_protected=is_protected,
            is_new_file=True,
        )

    # Edit existing file mode
    if not target.is_file():
        return EditResult(
            success=False,
            file_path=clean_path,
            message=f"Error: File '{clean_path}' does not exist to be edited.",
        )

    try:
        content = target.read_bytes().decode("utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return EditResult(
            success=False,
            file_path=clean_path,
            message=f"Error reading file '{clean_path}': {exc}",
        )

    # Direct match check
    if old_str in content:
        matches = content.count(old_str)
        if matches != 1:
            return EditResult(
                success=False,
                file_path=clean_path,
                message=(
                    f"Error: old_str matches {matches} occurrences in '{clean_path}'. "
                    f"Please include more surrounding context lines in old_str to ensure a unique match."
                ),
            )
        if "\r\n" in content and "\r\n" not in new_str:
            target_new = new_str.replace("\n", "\r\n")
        elif "\r\n" not in content and "\r\n" in new_str:
            target_new = new_str.replace("\r\n", "\n")
        else:
            target_new = new_str
        updated_content = content.replace(old_str, target_new, 1)
    else:
        # Cross-platform newline normalization fallback (handles CRLF vs LF differences)
        norm_content = content.replace("\r\n", "\n")
        norm_old = old_str.replace("\r\n", "\n")
        norm_new = new_str.replace("\r\n", "\n")
        matches = norm_content.count(norm_old)
        if matches == 0:
            return EditResult(
                success=False,
                file_path=clean_path,
                message=f"Error: old_str was not found in '{clean_path}'. Verify indentation and surrounding lines.",
            )
        if matches > 1:
            return EditResult(
                success=False,
                file_path=clean_path,
                message=(
                    f"Error: old_str matches {matches} occurrences in '{clean_path}'. "
                    f"Please include more surrounding context lines in old_str to ensure a unique match."
                ),
            )
        updated_norm = norm_content.replace(norm_old, norm_new, 1)
        # Restore CRLF if file originally used CRLF line endings
        updated_content = updated_norm.replace("\n", "\r\n") if "\r\n" in content else updated_norm

    try:
        target.write_bytes(updated_content.encode("utf-8"))
    except OSError as exc:
        return EditResult(
            success=False,
            file_path=clean_path,
            message=f"FileSystemError writing to '{clean_path}': {exc}",
        )

    return EditResult(
        success=True,
        file_path=clean_path,
        message=f"Successfully updated '{clean_path}'",
        is_protected=is_protected,
        is_new_file=False,
    )


def delete_file(
    workspace_root: Path,
    file_path: str,
    target_files_whitelist: list[str],
) -> EditResult:
    """Explicitly delete a file inside the task worktree within the target files whitelist."""
    return edit_file(
        workspace_root=workspace_root,
        file_path=file_path,
        old_str="",
        new_str="",
        target_files_whitelist=target_files_whitelist,
        delete=True,
    )
