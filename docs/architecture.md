# OSWALD Architecture Specification

> [!NOTE]
> **TARGET END-STATE SPECIFICATION**: This document describes the intended end-state architecture, folder tree, and pipeline for OSWALD. The current repository contains the initial v1 backend scaffolding; full agent implementations, API routes, workers, and UI layers will be added iteratively as described here.

---

## 1. System Overview

OSWALD is an autonomous agent pipeline for software development running over an HTTP API. The pipeline transitions deterministically across four primary agent roles:
1. **Planner Agent**: Analyzes the problem statement and produces a structured specification (`spec.md`).
2. **Coder Agent**: Implements the specification in an isolated sandbox and generates diffs and changes (`changes.md`).
3. **Tester Agent**: Authors and runs test suites in the sandbox, verifying behavior (`test-results.md`).
4. **Reviewer Agent**: Evaluates the code against specifications, emitting a verdict (`review.md`).

A separate human approval gate is required before branch pushes or PR creation to VCS.

---

## 2. Target End-State Directory Structure

Below is the planned repository layout as components are implemented:

```
oswald/
├── docs/
│   ├── adr/
│   └── architecture.md
├── src/
│   └── oswald/
│       ├── core/
│       │   ├── orchestrator/      # State machine and composition root
│       │   ├── services/          # Shared service entry point into orchestrator
│       │   ├── agents/            # Agent role modules
│       │   │   └── prompts/       # Raw Markdown prompt files
│       │   ├── tools/             # Granular tools (one per role)
│       │   ├── artifacts/         # Pydantic artifact schemas & storage Protocol
│       │   ├── llm/               # LLMClient Protocol, provider adapters, budget
│       │   ├── sandbox/           # SandboxRuntime Protocol & Docker runtime
│       │   └── vcs/               # GitProvider Protocol & GitHub implementation
│       ├── api/
│       │   └── routes/            # FastAPI endpoints and route handlers
│       ├── web/                   # Jinja2 templates & static assets (future)
│       │   ├── templates/
│       │   └── static/
│       ├── cli/                   # Typer CLI commands (future)
│       │   └── commands/
│       ├── workers/               # Taskiq background worker & job definitions
│       ├── db/                    # SQLAlchemy models, repositories, run_events
│       └── observability/         # Structured logging and OTel setup
├── migrations/                    # Alembic schema migrations (future)
├── tests/
│   ├── unit/                      # Unit tests (fakes, no external services)
│   └── integration/               # Integration tests (Postgres, Redis, Docker)
├── docker/                        # App and sandbox base Dockerfiles (future)
└── .github/
    └── workflows/                 # CI pipelines
```

---

## 3. Architecture Diagrams

### System Architecture

```mermaid
flowchart TD
    subgraph Clients
        WebUI["Web App (non-technical requester)"]
        CLIdev["CLI (interactive, local dev)"]
        CLIci["CLI (headless, CI/CD runner)"]
    end

    subgraph Edge["API Layer"]
        API["FastAPI (api/ routes + web/ routes, same process)"]
    end

    subgraph Shared["Shared Service Layer"]
        Svc["core/services — auth check, validation, tracing, core call"]
    end

    subgraph Async["Job Queue"]
        Redis[("Redis")]
        Worker["Taskiq Worker"]
    end

    subgraph Engine["Core Orchestration Engine"]
        SM["State Machine + Gates"]
        Planner["Planner Agent"]
        Coder["Coder Agent"]
        Tester["Tester Agent"]
        Reviewer["Reviewer Agent"]
    end

    Sandbox["Docker Sandbox (per run, no network by default)"]
    Git["GitProvider (clone / branch / push)"]
    PG[("Postgres")]
    Claude["Anthropic API"]
    OTel["OTel SDK (every service)"]
    SigNoz["SigNoz — traces / metrics / logs"]

    WebUI --> API
    CLIdev --> API
    CLIci --> API

    API --> Svc
    Svc --> SM
    Svc --> PG

    API --> Redis
    Redis --> Worker
    Worker --> SM
    SM --> Planner --> Claude
    SM --> Coder --> Claude
    SM --> Tester --> Claude
    SM --> Reviewer --> Claude
    Coder --> Sandbox
    Tester --> Sandbox
    Sandbox --> Git
    SM -- "only after approval gate" --> Git

    API -.-> OTel
    Worker -.-> OTel
    SM -.-> OTel
    OTel --> SigNoz
```

---

### Request Lifecycle & Human Approval Gate

```mermaid
sequenceDiagram
    actor PM as Non-technical requester
    participant Web as Web App
    participant API as FastAPI
    participant Svc as Shared Service Layer
    participant Q as Redis/Taskiq
    participant W as Worker
    participant O as Orchestrator
    participant P as Planner
    participant C as Coder
    participant T as Tester
    participant R as Reviewer
    actor Dev as Technical reviewer
    participant Git as GitProvider

    PM->>Web: Describe feature in plain English
    Web->>API: POST /tasks
    API->>Svc: create_task()
    Svc->>Q: enqueue job
    Svc-->>API: run_id (pending)
    API-->>Web: run_id (pending)
    Q->>W: pick up job
    W->>O: start run

    O->>P: generate spec
    P-->>O: spec.md
    alt open question raised
        O-->>Web: paused — needs input
        PM->>Web: answers question
        Web->>API: POST /tasks/{id}/answer
        API->>Svc: submit_answer()
        Svc->>O: resume planning
    end

    O->>C: implement per spec (in sandbox)
    C-->>O: changes.md + diff
    O->>T: write + run tests (in sandbox)
    T-->>O: test-results.md
    O->>R: review implementation
    R-->>O: verdict + review.md

    O-->>Web: plain-language summary
    Web-->>PM: "Ready to ship" / "Needs a look"
    O-->>Dev: technical review requested
    Dev->>Web: inspects diff, clicks Approve
    Web->>API: POST /tasks/{id}/approve
    API->>Svc: approve_gate()
    Svc->>O: record approval
    Note over O: merge/deploy unblocked only now
    O->>Git: push branch / open PR
    Git-->>O: PR URL
    O-->>Web: PR link surfaced to requester
```

---

## 4. Architectural Boundaries and Rules

1. **`core/` Framework Isolation**:
   - Must contain pure business logic and protocols.
   - Strictly forbidden from importing `oswald.api`, `oswald.workers`, or `oswald.db`.
2. **`core/artifacts/` Pydantic Purity**:
   - Contains immutable schemas representing pipeline handoffs.
   - Strictly forbidden from importing `sqlalchemy` or ORM models.
3. **Single Entry Point**:
   - `core/services` is the exclusive public interface into `core/orchestrator`.
4. **VCS Segregation**:
   - `core/vcs` is the only component authorized to interact with git.
5. **Observability**:
   - Structured logs with `run_id` across all operations.
   - OTel SDK instrumented throughout with stdout export initially, enabling seamless transition to SigNoz or external telemetry collectors when needed.
