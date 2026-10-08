# Contributing to OSWALD

Thank you for contributing to OSWALD. Please review the guidelines below before opening pull requests or contributing code.

---

## 1. Development Setup

1. **Prerequisites**: Ensure Python 3.12, [`uv`](https://github.com/astral-sh/uv), and Docker are installed.
2. **Environment File**: Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
3. **Install Dependencies**:
   ```bash
   make install
   ```
4. **Pre-commit Hooks**:
   Pre-commit hooks run automatically on commit. You can install and test them manually:
   ```bash
   uv run pre-commit install
   uv run pre-commit run --all-files
   ```

---

## 2. Developer Makefile Targets

All common developer tasks are encapsulated in the root `Makefile`:

| Target | Description |
| --- | --- |
| `make install` | Sync virtual environment dependencies via `uv sync` |
| `make lint` | Run code quality checks via `ruff check` |
| `make format` | Format code using `ruff format` |
| `make typecheck` | Run static type checking via `mypy src tests` |
| `make imports` | Verify architectural dependency contracts via `import-linter` |
| `make test` | Run unit test suite (excludes `@pytest.mark.integration`) |
| `make test-integration` | Run integration test suite against local services |
| `make check` | Run full pre-push verification (lint, format check, typecheck, imports, unit tests) |
| `make up` | Start local Postgres and Redis containers in the background |
| `make down` | Stop local Postgres and Redis containers |

---

## 3. Dependency Policy: Add Only When Used

To avoid dependency bloat and security overhead:
- **Add a dependency only when code in the repository directly imports it.**
- Never add speculative, "might need later" dependencies.
- Runtime dependencies belong in `[project.dependencies]`, while development tooling belongs in `[dependency-groups.dev]`.

---

## 4. Running Integration Tests

Integration tests require local backing services (PostgreSQL and Redis).

1. Start the service containers:
   ```bash
   make up
   ```
2. Verify services are healthy:
   ```bash
   docker compose ps
   ```
3. Execute the integration test suite:
   ```bash
   make test-integration
   ```
4. Stop services when finished:
   ```bash
   make down
   ```

---

## 5. Commit and PR Conventions

- **Commit Messages**: Follow Conventional Commits format (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`). Keep commits small, logical, and focused on a single change.
- **Architectural Integrity**:
  - `src/oswald/core/` must never import from `oswald.api`, `oswald.workers`, or `oswald.db`.
  - `src/oswald/core/artifacts/` must never import `sqlalchemy`.
  - Enforced automatically by `make imports`.
- **Pull Requests**:
  - Fill out all sections in the pull request template (summary, motivation, testing, risks).
  - Ensure `make check` passes cleanly before requesting a review.
