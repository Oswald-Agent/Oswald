"""Core services package.

Responsibility:
    The only entry point into the orchestrator; coordinates authorization,
    validation, tracing, and core invocations.

Forbidden imports:
    Must not import from `oswald.api`, `oswald.workers`, or `oswald.db`.
"""
