"""Application configuration and settings helpers."""

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralised configuration loaded from environment variables or .env."""

    app_secret: Optional[str] = Field(
        None,
        description="Shared secret required in incoming requests.",
    )
    github_token: Optional[str] = Field(
        None,
        description="GitHub personal access token used for repo automation.",
    )
    github_owner: Optional[str] = Field(
        None,
        description="GitHub username or organisation that will own generated repos.",
    )
    github_default_branch: str = Field(
        "main",
        description="Branch used for the generated repositories.",
    )
    redis_url: str = Field(
        "redis://localhost:6379/0",
        description="Redis connection URL for Celery broker and result backend.",
    )
    github_base_url: str = Field(
        "https://api.github.com",
        description="Base URL for the GitHub REST API.",
    )
    github_pages_host: str = Field(
        "github.io",
        description="Host suffix used when constructing GitHub Pages URLs.",
    )
    pages_timeout_seconds: int = Field(
        420,
        description="Maximum seconds to wait for GitHub Pages to become available.",
    )
    pages_poll_interval: int = Field(
        15,
        description="Seconds between GitHub Pages status checks.",
    )
    callback_timeout_seconds: int = Field(
        10,
        description="HTTP timeout when notifying the evaluation URL.",
    )
    dry_run: bool = Field(
        False,
        description=(
            "Enable to skip real GitHub calls and use local filesystem output for testing."
        ),
    )
    log_level: str = Field(
        "INFO",
        description="Application log level.",
    )
    openai_api_key: Optional[str] = Field(
        None,
        description="OpenAI-compatible API key for LLM powered generation.",
    )
    ai_pipe_token: Optional[str] = Field(
        None,
        description="API token for AI Pipe proxy (used if OPENAI_API_KEY is unset).",
    )
    openai_model: str = Field(
        "gpt-4o-mini",
        description="Model identifier used when invoking the OpenAI API.",
    )
    openai_base_url: Optional[str] = Field(
        None,
        description="Override the OpenAI API base URL (e.g., for compatible providers).",
    )
    openai_temperature: float = Field(
        0.2,
        ge=0.0,
        le=1.0,
        description="Sampling temperature for the LLM.",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )


@lru_cache()
def get_settings() -> Settings:
    """Return a cached Settings instance."""

    return Settings()
