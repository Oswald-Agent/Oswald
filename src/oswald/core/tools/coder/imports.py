"""Declared dependencies inspector checking changed files against pyproject.toml."""

import ast
import re
import sys
import tomllib
from pathlib import Path

_DEP_NAME_REGEX = re.compile(r"^([a-zA-Z0-9_\-\.]+)")

# Common mappings where Python import module name differs from package distribution name
KNOWN_IMPORT_TO_PKG = {
    "yaml": "pyyaml",
    "cv2": "opencv-python",
    "PIL": "pillow",
    "bs4": "beautifulsoup4",
    "sklearn": "scikit-learn",
    "tree_sitter": "tree-sitter-language-pack",
    "tree_sitter_language_pack": "tree-sitter-language-pack",
}


def _normalize_name(name: str) -> str:
    return name.lower().replace("_", "-")


def get_declared_packages(workspace_root: Path) -> set[str]:
    """Parse declared package names from pyproject.toml in the workspace."""
    pyproject_path = workspace_root / "pyproject.toml"
    if not pyproject_path.is_file():
        return set()

    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return set()

    packages: set[str] = set()

    # Extract [project.dependencies]
    project = data.get("project", {})
    for dep_str in project.get("dependencies", []):
        match = _DEP_NAME_REGEX.match(dep_str.strip())
        if match:
            packages.add(_normalize_name(match.group(1)))

    # Extract [dependency-groups]
    dep_groups = data.get("dependency-groups", {})
    for group_deps in dep_groups.values():
        if isinstance(group_deps, list):
            for dep_str in group_deps:
                if isinstance(dep_str, str):
                    match = _DEP_NAME_REGEX.match(dep_str.strip())
                    if match:
                        packages.add(_normalize_name(match.group(1)))

    return packages


def check_declared_imports(
    workspace_root: Path,
    changed_files: list[str],
) -> list[str]:
    """Inspect Python AST imports in changed files and flag any undeclared external packages."""
    ws = workspace_root.resolve()
    declared = get_declared_packages(ws)
    warnings: list[str] = []

    # Identify local top-level modules inside workspace (e.g., 'oswald', 'src', etc.)
    local_modules: set[str] = set()
    for entry in ws.iterdir():
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            local_modules.add(entry.name)
            if entry.name == "src":
                for sub in entry.iterdir():
                    if sub.is_dir() or sub.suffix == ".py":
                        local_modules.add(sub.stem)
        elif entry.suffix == ".py":
            local_modules.add(entry.stem)

    for rel_path in changed_files:
        if not rel_path.endswith(".py"):
            continue

        full_path = ws / rel_path
        if not full_path.is_file():
            continue

        try:
            tree = ast.parse(full_path.read_text(encoding="utf-8"), filename=rel_path)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        for node in ast.walk(tree):
            imported_names: list[str] = []
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.append(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported_names.append(node.module.split(".")[0])

            for mod in imported_names:
                # 1. Skip standard library
                if mod in sys.stdlib_module_names:
                    continue
                # 2. Skip local workspace packages
                if mod in local_modules:
                    continue

                # 3. Resolve package distribution name
                pkg_candidate = KNOWN_IMPORT_TO_PKG.get(mod, mod)
                norm_pkg = _normalize_name(pkg_candidate)

                if declared and norm_pkg not in declared:
                    lineno = getattr(node, "lineno", 1)
                    msg = (
                        f"Undeclared dependency '{mod}' imported in '{rel_path}' (line {lineno}). "
                        f"Coder agent must not introduce undeclared third-party packages; escalate instead."
                    )
                    if msg not in warnings:
                        warnings.append(msg)

    return warnings
