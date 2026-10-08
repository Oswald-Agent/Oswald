"""Application configuration settings."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """OSWALD application settings loaded from environment variables or .env."""

    anthropic_api_key: str = Field(
        default="your-anthropic-api-key-here",
        validation_alias="ANTHROPIC_API_KEY",
        description="Anthropic API key for agent LLM calls",
    )
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
