"""Unit tests for configuration and settings."""

import pytest
from pydantic_settings import SettingsConfigDict

from oswald.config import Settings


def test_default_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify default application settings without external env files."""
    monkeypatch.setattr(Settings, "model_config", SettingsConfigDict(env_file=None, extra="ignore"))
    settings = Settings()
    assert settings.llm_provider_key == "your-llm-provider-key-here"
    assert settings.llm_base_url is None
    assert settings.default_model == "gemini/gemini-2.0-flash"
    assert settings.planner_model == "gemini/gemini-2.0-flash"
    assert settings.coder_model == "gemini/gemini-2.0-flash"
    assert settings.tester_model == "gemini/gemini-2.0-flash"
    assert settings.reviewer_model == "gemini/gemini-2.0-flash"
    assert "postgresql" in settings.database_url
    assert "redis" in settings.redis_url
    assert settings.log_level == "INFO"


def test_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify settings loaded from environment variable overrides."""
    monkeypatch.setattr(Settings, "model_config", SettingsConfigDict(env_file=None, extra="ignore"))
    monkeypatch.setenv("LLM_PROVIDER_KEY", "test-llm-key-12345")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("DEFAULT_MODEL", "openai/gpt-4o")
    monkeypatch.setenv("PLANNER_MODEL", "claude-3-5-sonnet-20241022")
    monkeypatch.setenv("CODER_MODEL", "deepseek/deepseek-coder")
    monkeypatch.setenv("TESTER_MODEL", "openai/gpt-4o-mini")
    monkeypatch.setenv("REVIEWER_MODEL", "gemini/gemini-1.5-pro")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@db:5432/testdb")
    monkeypatch.setenv("REDIS_URL", "redis://queue:6379/1")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = Settings()
    assert settings.llm_provider_key == "test-llm-key-12345"
    assert settings.llm_base_url == "http://localhost:11434/v1"
    assert settings.default_model == "openai/gpt-4o"
    assert settings.planner_model == "claude-3-5-sonnet-20241022"
    assert settings.coder_model == "deepseek/deepseek-coder"
    assert settings.tester_model == "openai/gpt-4o-mini"
    assert settings.reviewer_model == "gemini/gemini-1.5-pro"
    assert settings.database_url == "postgresql+asyncpg://user:pass@db:5432/testdb"
    assert settings.redis_url == "redis://queue:6379/1"
    assert settings.log_level == "DEBUG"
