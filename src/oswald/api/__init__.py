"""API package.

Responsibility:
    Thin FastAPI layer exposing endpoints and calling `oswald.core.services`.

Forbidden imports:
    Must not import directly from `oswald.core.orchestrator` (must call via `oswald.core.services`).
"""
