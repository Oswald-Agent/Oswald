# ADR-0002: LLMClient Protocol with Provider-Neutral Types

- **Status**: Accepted
- **Date**: 2026-10-08
- **Deciders**: OSWALD Architecture Team

## Context and Problem Statement

While Claude (via Anthropic) is the primary model family for OSWALD v1, coupling agent logic directly to Anthropic-specific response shapes or SDK types would create vendor lock-in across all agent definitions. We need an interface that permits swapping or augmenting LLM providers without touching agent business logic.

## Decision Drivers

- Decoupling agent implementations from vendor SDK classes.
- Testability: ability to mock or stub LLM responses cleanly in unit tests.
- Extensibility: allow third-party providers or alternative models via plugins.
- Uniform token budgeting, latency tracking, and error handling.

## Considered Options

1. **Direct usage of `anthropic.Anthropic` clients** across all agent modules.
2. **`LLMClient` Protocol** with provider-neutral message, tool, and response types.

## Decision Outcome

Chosen option: **`LLMClient` Protocol with provider-neutral types**. Agent implementations interact strictly with generic request/response structures defined in `oswald.core.llm`. The Anthropic SDK adapter translates between neutral types and Anthropic API payloads.

### Consequences

- **Positive**:
  - Agent roles and prompt runners depend only on the neutral protocol.
  - Unit tests can mock `LLMClient` without network calls or mocking complex SDK internals.
  - Future model adapters (e.g. OpenAI, local LLMs) can be introduced by implementing the protocol.
- **Negative / Risks**:
  - Mapping layer introduces boilerplate and requires keeping neutral types expressive enough for advanced features (e.g., prompt caching, tool use).
- **Mitigations**:
  - Provider adapters encapsulate translation logic; provider-specific metadata is preserved in generic response attributes.
