.PHONY: help install lint format typecheck imports test test-integration check up down

help:
	@echo "OSWALD development targets:"
	@echo "  install          Sync dependencies using uv"
	@echo "  lint             Run ruff check"
	@echo "  format           Format codebase using ruff"
	@echo "  typecheck        Run mypy type checking"
	@echo "  imports          Verify import boundaries using import-linter"
	@echo "  test             Run unit tests"
	@echo "  test-integration Run integration tests"
	@echo "  check            Run lint, format check, typecheck, imports, and unit tests"
	@echo "  up               Start local PostgreSQL and Redis containers"
	@echo "  down             Stop local PostgreSQL and Redis containers"

install:
	uv sync

lint:
	uv run ruff check

format:
	uv run ruff format

typecheck:
	uv run mypy src tests

imports:
	uv run lint-imports

test:
	uv run pytest -m "not integration"

test-integration:
	uv run pytest -m "integration"

check: lint
	uv run ruff format --check
	$(MAKE) typecheck
	$(MAKE) imports
	$(MAKE) test

up:
	docker compose up -d

down:
	docker compose down
