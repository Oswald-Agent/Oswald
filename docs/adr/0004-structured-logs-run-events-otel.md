# ADR-0004: Structured Logs, Run Events, and Swappable OTel Exporter (SigNoz Deferred)

- **Status**: Accepted
- **Date**: 2026-10-08
- **Deciders**: OSWALD Architecture Team

## Context and Problem Statement

OSWALD executes multi-step asynchronous runs across HTTP requests, Taskiq job queues, Docker sandbox containers, and external LLM API calls. Diagnosing failures requires end-to-end trace correlation. However, hosting a dedicated tracing platform (like SigNoz or Jaeger) during initial local development adds unnecessary infrastructure overhead.

## Decision Drivers

- Immediate traceability with zero heavyweight infrastructure dependencies for local development.
- End-to-end correlation using a persistent `run_id` across logs, state transitions, and metrics.
- Seamless upgrade path to full APM/tracing backends without refactoring core codebase.
- Dual-use persistence: store token budgets and execution audits in PostgreSQL.

## Considered Options

1. **Deploy SigNoz container locally day one**: Run full OTel collector and SigNoz cluster.
2. **Plain logging only**: Simple print/logging without telemetry schemas.
3. **Structured logs + PostgreSQL `run_events` + instrumented OTel SDK (stdout/file exporter, SigNoz deferred)**.

## Decision Outcome

Chosen option: **Structured logs with `run_id` propagation, PostgreSQL `run_events` table, and OTel SDK with swappable exporter (stdout initially, SigNoz deferred)**. Every log message binds the current `run_id`. Major state transitions and token costs are stored in PostgreSQL's `run_events` table, satisfying both observability and budget enforcement requirements. OpenTelemetry instrumentation is embedded in the application code, but the exporter defaults to stdout/local logs until scale demands a dedicated APM backend.

### Consequences

- **Positive**:
  - Developers run `make up` with only Postgres and Redis, keeping resource usage light.
  - Failures are quickly grepped via `run_id` across log outputs and DB records.
  - Zero code rewrites when enabling SigNoz or OTLP collectors later—only an exporter configuration change.
- **Negative / Risks**:
  - No interactive visual trace dashboard until an external collector is enabled.
- **Mitigations**:
  - The `run_events` table and structured log viewer provide adequate visibility for development and debugging.
