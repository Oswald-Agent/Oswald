# ADR-0005: Taskiq for Asynchronous Background Jobs

- **Status**: Accepted
- **Date**: 2026-10-08
- **Deciders**: OSWALD Architecture Team

## Context and Problem Statement

OSWALD is an autonomous multi-step software development pipeline running on an asynchronous HTTP API. Agent pipeline runs (planning, coding in sandbox, testing, reviewing) are long-running I/O and compute-bound tasks that must not block the FastAPI web server. We need an asynchronous distributed task queue to handle worker coordination, job dispatching, and retries.

Initially, `arq` was proposed in early scaffolding. However, we evaluated whether `arq`, `celery`, `bullmq`, or `taskiq` best fits OSWALD's pure Python 3.12 architecture.

## Decision Drivers

- Pure Python 3.12+ and native `asyncio` compatibility.
- Seamless integration with Pydantic v2 schemas for artifact and job payload serialization.
- Strict type checking (`mypy --strict`) on task dispatch and argument handling.
- Fast unit testing capability without mandatory live backing services (`InMemoryBroker`).
- Active open-source maintenance and ecosystem alignment with FastAPI.

## Considered Options

1. **Celery**: The historical standard for Python distributed tasks.
   - *Downside*: Heavyweight, complex configuration, awkward with native async/await, and legacy sync execution roots.
2. **Arq**: Lightweight Redis-based async job queue.
   - *Downside*: Low maintenance activity and infrequent release cycles; lacks modular broker backends.
3. **BullMQ (Python)**: Python port of the popular Node.js queue library.
   - *Downside*: Primarily designed for Node.js interoperability; lacks Pythonic idioms, type annotations, and Pydantic integration. Payloads are untyped dictionaries.
4. **Taskiq**: Modern async distributed task queue built specifically for Python 3.10+ and FastAPI.
   - *Upside*: Native `asyncio`, strict type safety (`.kiq()` is typed), modular brokers (Redis in production, InMemory for unit tests), dependency injection, and active maintenance.

## Decision Outcome

Chosen option: **Taskiq with Redis broker (`taskiq-redis`)**.

Taskiq provides idiomatic Python 3.12 async architecture with strict typing and Pydantic integration, while allowing unit tests to execute in memory without external dependencies.

### Consequences

- **Positive**:
  - Full typing support preserves strict `mypy` compliance across worker definitions and invocations.
  - Pydantic models are serialized and validated cleanly across worker boundaries.
  - `InMemoryBroker` enables blazing-fast unit tests in CI without spinning up Redis containers.
  - Production deployments use the Redis broker matching OSWALD's `docker-compose.yml`.
  - Active maintenance and native FastAPI dependency injection support.
- **Negative / Risks**:
  - Requires adding `taskiq` and `taskiq-redis` to dependencies.
- **Mitigations**:
  - Dependencies are managed cleanly via `uv` and pinned in `uv.lock`.
