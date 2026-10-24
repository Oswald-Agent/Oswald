"""Architecture conformance tests enforcing structural rules.

Ensures:
- Subprocesses are spawned only by authorized modules (core.vcs, core.sandbox).
- Direct git executions occur only within core.vcs.
"""

import ast
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "oswald"

SUBPROCESS_ALLOWLIST = {
    "core.vcs.worktree",
    "core.sandbox.docker_runtime",
    "core.sandbox.fake",
}


def test_subprocess_usage_is_strictly_allowlisted() -> None:
    """Verify no core or app modules spawn subprocesses outside authorized adapters."""
    violations: list[str] = []

    for py_file in SRC_DIR.rglob("*.py"):
        rel_mod = py_file.relative_to(SRC_DIR).with_suffix("").as_posix().replace("/", ".")
        if rel_mod in SUBPROCESS_ALLOWLIST:
            continue

        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))

        for node in ast.walk(tree):
            # Detect: import subprocess / from subprocess import ...
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "subprocess" or alias.name.startswith("subprocess."):
                        violations.append(f"{rel_mod}:{node.lineno} imports subprocess")
            elif isinstance(node, ast.ImportFrom):
                if node.module == "subprocess" or (
                    node.module and node.module.startswith("subprocess.")
                ):
                    violations.append(f"{rel_mod}:{node.lineno} imports from subprocess")

            # Detect calls: asyncio.create_subprocess_* or os.system/os.popen
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    if func.attr in ("create_subprocess_exec", "create_subprocess_shell"):
                        violations.append(f"{rel_mod}:{node.lineno} calls asyncio.{func.attr}")
                    elif (
                        func.attr
                        in (
                            "system",
                            "popen",
                            "spawnl",
                            "spawnle",
                            "spawnlp",
                        )
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "os"
                    ):
                        violations.append(f"{rel_mod}:{node.lineno} calls os.{func.attr}")

    assert not violations, "Forbidden subprocess usage detected:\n" + "\n".join(violations)
