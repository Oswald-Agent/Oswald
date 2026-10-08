# ADR-0001: Raw Anthropic SDK Instead of an Agent Framework

- **Status**: Accepted
- **Date**: 2026-10-08
- **Deciders**: OSWALD Architecture Team

## Context and Problem Statement

OSWALD coordinates a deterministic pipeline of four discrete agent roles (Planner, Coder, Tester, Reviewer). Many off-the-shelf agent frameworks (LangChain, CrewAI, AutoGen) offer prepackaged abstractions, prompt chaining, and memory loops. We need to decide whether to adopt a third-party agent framework or use the raw Anthropic Python SDK directly.

## Decision Drivers

- Strict separation of agent roles, tools, and system prompts.
- Deterministic state machine control over transitions and human approval gates.
- Fine-grained token budgeting, prompt caching, and circuit-breaking.
- Minimal abstraction overhead and debuggability.

## Considered Options

1. **Third-party agent framework** (e.g., LangChain, CrewAI, AutoGen).
2. **Raw Anthropic SDK** invoked per agent role with explicit schemas.

## Decision Outcome

Chosen option: **Raw Anthropic SDK**, because role separation and explicit control flow are foundational to OSWALD's reliability. Agent frameworks introduce hidden abstractions, leaky execution loops, and opaque state management that obscure failures and token consumption. Direct SDK calls make tool schemas, prompt construction, and response parsing explicit.

### Consequences

- **Positive**:
  - Full transparency over prompt assembly, system prompts, and tool calling definitions.
  - Predictable control flow governed by OSWALD's explicit orchestrator state machine.
  - Elimination of heavy framework dependencies and version churn.
- **Negative / Risks**:
  - Boilerplate for handling retries, tool call parsing, and streaming must be managed internally.
- **Mitigations**:
  - Encapsulated within `core/llm/` behind the `LLMClient` protocol.
