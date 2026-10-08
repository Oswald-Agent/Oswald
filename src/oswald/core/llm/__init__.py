"""Core LLM package.

Responsibility:
    LLMClient Protocol, provider adapters (Anthropic SDK), budget, and circuit breaker.

Forbidden imports:
    Must not import from `oswald.api`, `oswald.workers`, or `oswald.db`.
"""
