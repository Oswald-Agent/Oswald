"""Host-side read tools for Coder agent with path traversal and symlink guards."""

import fnmatch
import os
import re
from pathlib import Path

from oswald.core.codeindex.indexer import CodeIndexer

MAX_TOOL_OUTPUT_BYTES = 8192


def truncate_output(text: str, max_bytes: int = MAX_TOOL_OUTPUT_BYTES) -> str:
    """Truncate tool output if it exceeds maximum byte budget."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return f"{truncated}\n\n[... Output truncated: exceeded {max_bytes} bytes ceiling ...]"


def normalize_workspace_rel_path(workspace_root: Path, path_str: str) -> str:
    """Normalize a path to be strictly relative to workspace_root.

    Handles absolute paths inside the workspace, leading slashes, and ./ prefixes.
    Raises PermissionError on traversal outside workspace.
    """
    clean = path_str.strip()
    if not clean:
        return ""
    ws = workspace_root.resolve()
    p = Path(clean)
    if p.is_absolute():
        try:
            if p.resolve().is_relative_to(ws):
                return p.resolve().relative_to(ws).as_posix()
        except (ValueError, RuntimeError):
            pass

        if clean == "/workspace" or clean.startswith("/workspace/"):
            cand = clean.removeprefix("/workspace").lstrip("/")
            return cand

        # Known system root directories on host Linux/Unix systems
        system_roots = {
            "etc",
            "var",
            "usr",
            "home",
            "root",
            "tmp",
            "opt",
            "bin",
            "sbin",
            "lib",
            "dev",
            "proc",
            "sys",
            "private",
        }
        if p.exists() or (len(p.parts) > 1 and p.parts[1] in system_roots):
            raise PermissionError(
                f"Path traversal detected: '{path_str}' resolves outside workspace"
            )

        rel_cand = clean.lstrip("/")
        resolved = (ws / rel_cand).resolve()
        if not resolved.is_relative_to(ws):
            raise PermissionError(
                f"Path traversal detected: '{path_str}' resolves outside workspace"
            )
        return rel_cand

    while clean.startswith("./") or clean.startswith("/"):
        if clean.startswith("./"):
            clean = clean.removeprefix("./")
        if clean.startswith("/"):
            clean = clean.removeprefix("/")
    return clean


def validate_workspace_path(workspace_root: Path, rel_path: str) -> Path:
    """Ensure path resolves strictly within the workspace worktree and rejects symlinks."""
    clean_rel = normalize_workspace_rel_path(workspace_root, rel_path)
    if not clean_rel:
        raise FileNotFoundError("Empty file path provided")
    ws = workspace_root.resolve()
    target = (ws / clean_rel).resolve()

    if not target.is_relative_to(ws):
        raise PermissionError(f"Path traversal detected: '{rel_path}' resolves outside workspace")

    # Check that no existing path component in workspace is a symlink pointing outside
    current = ws
    for part in Path(clean_rel).parts:
        current = current / part
        if current.is_symlink():
            resolved_link = current.resolve()
            if not resolved_link.is_relative_to(ws):
                raise PermissionError(
                    f"Symlink traversal detected: '{current}' points outside workspace to '{resolved_link}'"
                )

    return target


def read_file_range(
    workspace_root: Path,
    file_path: str,
    start_line: int = 1,
    end_line: int | None = None,
) -> str:
    """Read a specific slice of lines from a workspace file (1-indexed, inclusive)."""
    try:
        target = validate_workspace_path(workspace_root, file_path)
    except FileNotFoundError:
        return f"Error: File '{file_path}' does not exist"

    if not target.is_file():
        return f"Error: File '{file_path}' does not exist"

    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"Error: File '{file_path}' is not valid UTF-8 text"
    except OSError as exc:
        return f"Error reading file '{file_path}': {exc}"

    lines = content.splitlines()
    total_lines = len(lines)
    if start_line < 1:
        start_line = 1
    if end_line is not None and start_line > end_line:
        return f"Error: start_line ({start_line}) cannot be greater than end_line ({end_line})"
    if end_line is None or end_line > total_lines:
        end_line = total_lines

    if start_line > total_lines:
        return f"Error: start_line {start_line} exceeds file length ({total_lines} lines)"

    selected = lines[start_line - 1 : end_line]
    numbered_lines = [f"{start_line + i:4d} | {line}" for i, line in enumerate(selected)]
    output = "\n".join(numbered_lines)
    return truncate_output(output)


def list_files(
    workspace_root: Path,
    directory: str = ".",
    recursive: bool = True,
    max_files: int = 200,
) -> str:
    """List relative file paths inside a workspace directory, filtering exclusions."""
    target_dir = validate_workspace_path(workspace_root, directory)
    if not target_dir.is_dir():
        return f"Error: Directory '{directory}' does not exist"

    excluded_dirs = {
        ".git",
        ".oswald",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "node_modules",
        "build",
        "dist",
    }
    matched: list[str] = []

    ws = workspace_root.resolve()
    if recursive:
        for root, dirs, files in os.walk(target_dir):
            dirs[:] = [d for d in dirs if d not in excluded_dirs]
            for file in files:
                full = Path(root) / file
                rel = full.relative_to(ws).as_posix()
                matched.append(rel)
                if len(matched) >= max_files:
                    break
            if len(matched) >= max_files:
                break
    else:
        for entry in target_dir.iterdir():
            if entry.name in excluded_dirs:
                continue
            if entry.is_file():
                matched.append(entry.relative_to(ws).as_posix())
            if len(matched) >= max_files:
                break

    if not matched:
        return "No files found."

    output = "\n".join(sorted(matched))
    return truncate_output(output)


def search_code(
    workspace_root: Path,
    query: str,
    file_pattern: str | None = None,
    is_regex: bool = False,
    max_matches: int = 50,
) -> str:
    """Search code across workspace files with regex or literal matching."""
    if not query or not query.strip():
        return "Error: Search query cannot be empty"

    ws = workspace_root.resolve()
    excluded_dirs = {
        ".git",
        ".oswald",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "node_modules",
        "build",
        "dist",
    }

    if is_regex:
        try:
            pattern = re.compile(query)
        except re.error as exc:
            return f"Error: Invalid regex '{query}': {exc}"
    else:
        pattern = None

    matches: list[str] = []
    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if d not in excluded_dirs]
        for file in files:
            if file_pattern and not fnmatch.fnmatch(file, file_pattern):
                continue
            full_path = Path(root) / file
            rel_path = full_path.relative_to(ws).as_posix()

            try:
                content = full_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue

            for idx, line in enumerate(content.splitlines(), start=1):
                is_hit = pattern.search(line) if pattern else (query in line)
                if is_hit:
                    matches.append(f"{rel_path}:{idx}: {line.strip()}")
                    if len(matches) >= max_matches:
                        break
            if len(matches) >= max_matches:
                break
        if len(matches) >= max_matches:
            break

    if not matches:
        return f"No matches found for '{query}'."

    output = "\n".join(matches)
    return truncate_output(output)


def read_symbol(
    workspace_root: Path,
    symbol_name: str,
    indexer: CodeIndexer | None = None,
) -> str:
    """Find and retrieve symbol definition or signature across workspace."""
    if indexer is not None:
        symbols = indexer.find_symbol(symbol_name)
        if symbols:
            entries: list[str] = []
            for s in symbols:
                entries.append(
                    f"Symbol: {s.name} ({s.kind})\nFile: {s.file_path}:{s.start_line}-{s.end_line}\nSignature: {s.signature or 'N/A'}\nDoc: {s.docstring_first_line or 'None'}"
                )
            return truncate_output("\n\n".join(entries))

    # Fallback to search_code if no indexer provided
    return search_code(workspace_root, f"def {symbol_name}", is_regex=False)


def get_file_summary(
    workspace_root: Path,
    file_path: str,
    indexer: CodeIndexer | None = None,
) -> str:
    """Retrieve indexed symbol summary or skeleton for a file."""
    if indexer is not None:
        skeleton = indexer.get_skeleton([file_path])
        if skeleton.strip():
            return truncate_output(skeleton)

    # Fallback to reading first 50 lines
    return read_file_range(workspace_root, file_path, start_line=1, end_line=50)
