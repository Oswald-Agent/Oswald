"""Allowlisted static checks execution tool running inside the container sandbox."""

from dataclasses import dataclass

from oswald.core.sandbox.exceptions import (
    ContainerNotRunningError,
    SandboxError,
    SandboxTimeoutError,
)
from oswald.core.sandbox.protocol import SandboxRuntime
from oswald.core.tools.coder.read import truncate_output

ALLOWLISTED_COMMANDS: dict[str, list[str]] = {
    "ruff_check": ["ruff", "check", "."],
    "ruff_format": ["ruff", "format", "--check", "."],
    "mypy": ["mypy", "."],
    "syntax_check": [
        "python",
        "-X",
        "pycache_prefix=/tmp/oswald-pycache",
        "-m",
        "compileall",
        "-q",
        ".",
    ],
}

FORBIDDEN_KEYWORDS = {
    "pytest",
    "unittest",
    "nosetests",
    "tox",
    "pip",
    "uv",
    "apt",
    "curl",
    "wget",
}

SAFE_CACHE_ENV = {
    "MYPY_CACHE_DIR": "/tmp/.mypy_cache",  # noqa: S108
    "RUFF_CACHE_DIR": "/tmp/.ruff_cache",  # noqa: S108
    "PYTHONDONTWRITEBYTECODE": "1",
}


@dataclass(frozen=True)
class StaticCheckResult:
    """Outcome of a static check execution inside the sandbox."""

    check_name: str
    exit_code: int
    passed: bool
    output: str
    duration_seconds: float
    skipped: bool = False


async def run_static_check(
    sandbox: SandboxRuntime,
    check_name: str,
    timeout_seconds: float = 120.0,
) -> StaticCheckResult:
    """Execute a single allowlisted static check inside the sandbox.

    Guards enforced:
        - Strict command allowlist (ruff check, ruff format --check, mypy, compileall).
        - Explicitly forbids running tests (pytest, unittest) or package installers (D1).
        - Caches redirected to /tmp to prevent workspace pollution.
        - Output truncated to 8 KB ceiling.
    """
    clean_name = check_name.strip().lower()
    if clean_name not in ALLOWLISTED_COMMANDS:
        return StaticCheckResult(
            check_name=check_name,
            exit_code=1,
            passed=False,
            output=(
                f"PermissionDenied: Command '{check_name}' is not in the allowlist of static checks. "
                f"Allowed checks: {sorted(ALLOWLISTED_COMMANDS.keys())}. "
                f"Coder is forbidden from running tests or unapproved shell commands."
            ),
            duration_seconds=0.0,
        )

    cmd = ALLOWLISTED_COMMANDS[clean_name]

    # Redundant guard against forbidden commands
    for token in cmd:
        if token.lower() in FORBIDDEN_KEYWORDS:
            return StaticCheckResult(
                check_name=check_name,
                exit_code=1,
                passed=False,
                output=f"SecurityError: Command component '{token}' is strictly forbidden for Coder agent.",
                duration_seconds=0.0,
            )

    try:
        exec_res = await sandbox.exec_command(
            cmd=cmd,
            timeout_seconds=timeout_seconds,
            env=SAFE_CACHE_ENV,
        )
    except SandboxTimeoutError as exc:
        return StaticCheckResult(
            check_name=check_name,
            exit_code=124,
            passed=False,
            output=f"Check timed out after {timeout_seconds}s: {exc}",
            duration_seconds=timeout_seconds,
        )
    except ContainerNotRunningError as exc:
        return StaticCheckResult(
            check_name=check_name,
            exit_code=1,
            passed=False,
            output=f"Sandbox container is not running: {exc}",
            duration_seconds=0.0,
        )
    except SandboxError as exc:
        return StaticCheckResult(
            check_name=check_name,
            exit_code=1,
            passed=False,
            output=f"Sandbox execution error: {exc}",
            duration_seconds=0.0,
        )

    raw_output = f"{exec_res.stdout}\n{exec_res.stderr}".strip()
    if (
        exec_res.exit_code != 0
        and cmd[0] in {"ruff", "mypy"}
        and f'exec: "{cmd[0]}": executable file not found in $PATH' in raw_output
    ):
        return StaticCheckResult(
            check_name=clean_name,
            exit_code=exec_res.exit_code,
            passed=False,
            skipped=True,
            output=f"Optional static checker '{cmd[0]}' is not installed in the sandbox; "
            "checks using it were skipped.",
            duration_seconds=exec_res.duration_seconds,
        )
    return StaticCheckResult(
        check_name=check_name,
        exit_code=exec_res.exit_code,
        passed=(exec_res.exit_code == 0),
        output=truncate_output(raw_output or "Check completed with no output."),
        duration_seconds=exec_res.duration_seconds,
    )
