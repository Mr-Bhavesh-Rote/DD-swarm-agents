"""Typed application settings (§3.1).

Every secret and environment-specific value is read from a single `.env` file via a
typed pydantic-settings object. Required keys are validated on startup and the app
fails fast with a clear message if any are missing. Secret values are never logged
and never sent to the client.
"""
from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- LLM (Anthropic) ---
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    default_model: str = Field(default="claude-opus-4-8", alias="DEFAULT_MODEL")
    research_model: str = Field(default="claude-sonnet-4-6", alias="RESEARCH_MODEL")
    verifier_model: str = Field(default="claude-opus-4-8", alias="VERIFIER_MODEL")

    # --- Observability ---
    langfuse_public_key: str = Field(default="", alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field(default="", alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field(default="https://cloud.langfuse.com", alias="LANGFUSE_HOST")

    # --- Search / tools ---
    tavily_api_key: str = Field(default="", alias="TAVILY_API_KEY")
    scraper_user_agent: str = Field(default="deep-dd-bot/1.0", alias="SCRAPER_USER_AGENT")
    request_timeout_seconds: int = Field(default=30, alias="REQUEST_TIMEOUT_SECONDS")  # HTTP scraping
    llm_timeout_seconds: int = Field(default=120, alias="LLM_TIMEOUT_SECONDS")          # Anthropic calls
    llm_max_retries: int = Field(default=2, alias="LLM_MAX_RETRIES")

    # --- Content depth (raise for richer/longer reports; lower to cut cost) ---
    search_max_results: int = Field(default=8, alias="SEARCH_MAX_RESULTS")              # results per web_search
    search_depth: str = Field(default="advanced", alias="SEARCH_DEPTH")                    # "basic" (fast) | "advanced" (slower, deeper)
    search_include_raw_content: bool = Field(default=True, alias="SEARCH_INCLUDE_RAW_CONTENT")  # store full page text (for verifier)
    scrape_max_chars: int = Field(default=50000, alias="SCRAPE_MAX_CHARS")              # per-page extracted text cap
    research_max_tokens: int = Field(default=8000, alias="RESEARCH_MAX_TOKENS")         # per research-agent output
    aggregator_max_tokens: int = Field(default=4000, alias="AGGREGATOR_MAX_TOKENS")
    synthesizer_max_tokens: int = Field(default=16000, alias="SYNTHESIZER_MAX_TOKENS")  # FINAL report draft
    verifier_max_tokens: int = Field(default=4000, alias="VERIFIER_MAX_TOKENS")         # per verify batch
    verifier_source_chars: int = Field(default=6000, alias="VERIFIER_SOURCE_CHARS")     # source text per claim

    # --- Database & jobs ---
    database_url: str = Field(
        default="postgresql+asyncpg://user:pass@localhost:5432/deepdd",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # --- Workflow tuning ---
    max_revisions: int = Field(default=2, alias="MAX_REVISIONS")
    # Only run the (expensive) verifier→writer revise loop when faithfulness is below this.
    # A report at/above this score is accepted as-is (flagged claims recorded), avoiding a
    # full re-synthesis for a handful of weak citations.
    revision_min_faithfulness: float = Field(default=0.7, alias="REVISION_MIN_FAITHFULNESS")
    max_subagents: int = Field(default=8, alias="MAX_SUBAGENTS")
    recursion_limit: int = Field(default=50, alias="RECURSION_LIMIT")
    run_budget_usd: float = Field(default=10.0, alias="RUN_BUDGET_USD")

    # --- Auth & security ---
    jwt_secret: str = Field(default="", alias="JWT_SECRET")
    jwt_expiry_minutes: int = Field(default=120, alias="JWT_EXPIRY_MINUTES")
    cors_allowed_origins: str = Field(
        default="http://localhost:5173", alias="CORS_ALLOWED_ORIGINS"
    )

    # --- Storage (exports) ---
    export_storage_uri: str = Field(
        default="file:///var/deepdd/exports", alias="EXPORT_STORAGE_URI"
    )

    @property
    def cors_origins(self) -> List[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @field_validator("request_timeout_seconds", "max_revisions", "max_subagents")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be non-negative")
        return v


# Keys that MUST be present for the full server to run. The headless CLI (Milestone 1)
# only needs ANTHROPIC_API_KEY + a search key; the FastAPI server additionally needs a
# DB URL and JWT secret. We validate the union and fail fast.
REQUIRED_FOR_SERVER = ["anthropic_api_key", "database_url", "jwt_secret"]


def validate_required(settings: "Settings", required: List[str]) -> None:
    """Fail fast with a clear message listing every missing required key."""
    missing = [k.upper() for k in required if not getattr(settings, k, None)]
    if missing:
        raise RuntimeError(
            "Missing required configuration: "
            + ", ".join(missing)
            + ". Set them in your .env file (see .env.example)."
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
