"""Coder agent tools package.

All tools granted exclusively to the Coder agent (AGT5).
"""

from oswald.core.llm.types import ToolDefinition
from oswald.core.tools.coder.checks import (
    ALLOWLISTED_COMMANDS,
    StaticCheckResult,
    run_static_check,
)
from oswald.core.tools.coder.edit import EditResult, edit_file, is_protected_path, is_test_path
from oswald.core.tools.coder.imports import check_declared_imports, get_declared_packages
from oswald.core.tools.coder.read import (
    get_file_summary,
    list_files,
    read_file_range,
    read_symbol,
    search_code,
    validate_workspace_path,
)

CODER_TOOL_DEFINITIONS: list[ToolDefinition] = [
    ToolDefinition(
        name="read_file_range",
        description="Read lines from a workspace file (1-indexed). Useful for inspecting code slices.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative path to the file in workspace",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Starting line number (1-indexed, inclusive)",
                    "default": 1,
                },
                "end_line": {
                    "type": "integer",
                    "description": "Ending line number (inclusive). Omit to read up to budget ceiling.",
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
                    "description": "Optional glob pattern to filter files (e.g. '*.py')",
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
        name="edit_file",
        description=(
            "Modify an existing file by replacing old_str with new_str, or create a new file if old_str is empty. "
            "Must be within authorized target_files. Cannot edit test files or sensitive manifests without authorization."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative path of file to edit or create",
                },
                "old_str": {
                    "type": "string",
                    "description": "Exact text chunk to be replaced. Pass empty string to create a new file.",
                },
                "new_str": {
                    "type": "string",
                    "description": "Replacement text content",
                },
            },
            "required": ["file_path", "old_str", "new_str"],
        },
    ),
    ToolDefinition(
        name="run_static_check",
        description=(
            "Run an allowlisted static quality check inside the sandbox. "
            "Allowed check_name options: 'ruff_check', 'ruff_format', 'mypy', 'syntax_check'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "check_name": {
                    "type": "string",
                    "enum": ["ruff_check", "ruff_format", "mypy", "syntax_check"],
                    "description": "Name of the static check to execute",
                },
            },
            "required": ["check_name"],
        },
    ),
    ToolDefinition(
        name="check_declared_imports",
        description="Verify that imports in changed files are declared in dependencies. Read-only.",
        parameters={
            "type": "object",
            "properties": {},
        },
    ),
]

__all__ = [
    "ALLOWLISTED_COMMANDS",
    "CODER_TOOL_DEFINITIONS",
    "EditResult",
    "StaticCheckResult",
    "check_declared_imports",
    "edit_file",
    "get_declared_packages",
    "get_file_summary",
    "is_protected_path",
    "is_test_path",
    "list_files",
    "read_file_range",
    "read_symbol",
    "run_static_check",
    "search_code",
    "validate_workspace_path",
]
