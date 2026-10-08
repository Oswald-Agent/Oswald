# ADR-0003: Artifacts-Only Context Passing Between Agents

- **Status**: Accepted
- **Date**: 2026-10-08
- **Deciders**: OSWALD Architecture Team

## Context and Problem Statement

Multi-agent pipelines often pass unbounded conversation histories or full scratchpads from one agent to the next. In code generation tasks, this accumulates token bloat, causes hallucination compounding, and quickly exhausts context windows or budget limits. We need a disciplined mechanism for transferring context across stages.

## Decision Drivers

- Context efficiency and strict token budgeting per stage.
- Verifiable pipeline stages where each agent's output can be inspected independently.
- Clean boundaries: downstream agents consume only structured, relevant deliverables.
- Human-in-the-loop auditability.

## Considered Options

1. **Shared memory / accumulating conversation history**: All agents append to a single multi-turn dialogue.
2. **Artifacts-only context passing**: Each agent generates typed, self-contained artifacts (`spec.md`, `changes.md`, `test-results.md`, `review.md`) passed downstream via explicit schemas.

## Decision Outcome

Chosen option: **Artifacts-only context passing**. Each stage receives solely the structured artifacts produced by upstream stages necessary for its task. For instance, the Coder receives the Planner's `spec.md`, not the entire planning conversation.

### Consequences

- **Positive**:
  - Context windows are reset between agents, minimizing token consumption and hallucinations.
  - Intermediate deliverables are structured markdown files or typed Pydantic artifacts easily saved, inspected, or displayed in the UI.
  - Reproducibility and replayability: any stage can be rerun using its input artifacts.
- **Negative / Risks**:
  - Requires well-defined artifact schemas and clear specification requirements from earlier agents.
- **Mitigations**:
  - Enforced via Pydantic schemas in `oswald.core.artifacts` and strict validation before stage transitions.
