"""Unit tests for configuration and settings."""

import pytest

from oswald.config import Settings


def test_default_settings() -> None:
    """Verify default application settings."""
    settings = Settings()
    assert settings.anthropic_api_key == "your-anthropic-api-key-here"
    assert "postgresql" in settings.database_url
    assert "redis" in settings.redis_url
    assert settings.log_level == "INFO"


def test_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify settings loaded from environment variable overrides."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-12345")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@db:5432/testdb")
    monkeypatch.setenv("REDIS_URL", "redis://queue:6379/1")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = Settings()
    assert settings.anthropic_api_key == "sk-ant-test-key-12345"
    assert settings.database_url == "postgresql+asyncpg://user:pass@db:5432/testdb"
    assert settings.redis_url == "redis://queue:6379/1"
    assert settings.log_level == "DEBUG"
