"""Application configuration settings."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """OSWALD application settings loaded from environment variables or .env."""

    # LLM provider configuration
    llm_provider_key: str = Field(
        default="your-llm-provider-key-here",
        validation_alias="LLM_PROVIDER_KEY",
        description="API key for the configured LLM provider",
    )
    llm_base_url: str | None = Field(
        default=None,
        validation_alias="LLM_BASE_URL",
        description="Optional custom API base URL for local models or proxy",
    )

    # Agent model configurations (LiteLLM format, e.g., gemini/gemini-2.0-flash, gpt-4o, claude-3-5-sonnet-20241022)
    default_model: str = Field(
        default="gemini/gemini-2.0-flash",
        validation_alias="DEFAULT_MODEL",
        description="Default LLM model identifier across all agents",
    )
    planner_model: str = Field(
        default="gemini/gemini-2.0-flash",
        validation_alias="PLANNER_MODEL",
        description="Model identifier for the Planner agent",
    )
    coder_model: str = Field(
        default="gemini/gemini-2.0-flash",
        validation_alias="CODER_MODEL",
        description="Model identifier for the Coder agent",
    )
    tester_model: str = Field(
        default="gemini/gemini-2.0-flash",
        validation_alias="TESTER_MODEL",
        description="Model identifier for the Tester agent",
    )
    reviewer_model: str = Field(
        default="gemini/gemini-2.0-flash",
        validation_alias="REVIEWER_MODEL",
        description="Model identifier for the Reviewer agent",
    )

    # Backing services
    database_url: str = Field(
        default="postgresql+asyncpg://oswald:oswald@localhost:5432/oswald",
        validation_alias="DATABASE_URL",
        description="PostgreSQL connection URL",
    )
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        validation_alias="REDIS_URL",
        description="Redis connection URL",
    )
    log_level: str = Field(
        default="INFO",
        validation_alias="LOG_LEVEL",
        description="Structured logging log level",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
