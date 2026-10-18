from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (and ../.env locally)."""

    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    app_env: Literal["local", "ci", "prod"] = "local"
    log_level: str = "INFO"
    # Comma-separated in the environment, e.g. CORS_ORIGINS=http://localhost:3000,https://x.y
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    openai_api_key: SecretStr | None = None
    # "openai" for real embeddings; "hash" is a deterministic, offline stand-in (tests, demos).
    embedding_provider: Literal["openai", "hash"] = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    # Chat models per tier (docs/PLAN.md §5), chosen by per-task evals. Blank = not configured.
    model_easy: str | None = None  # query rewrite
    model_hard: str | None = None  # answer synthesis
    model_judge: str | None = None  # eval judge
    # reasoning_effort for reasoning models (e.g. "low"); blank = the model's default.
    model_easy_reasoning: str | None = None
    model_hard_reasoning: str | None = None
    model_judge_reasoning: str | None = None
    # Add model-suggested search terms to every question (docs/research/graphrag-retrieval-
    # study.md, option B). False: only multi-turn messages get a model call before search.
    query_expansion: bool = True
    # Global LLM spend per UTC day; chat answers stop with a friendly message once reached.
    daily_spend_cap_usd: float = 2.0

    database_url: str = "postgresql+asyncpg://support:support@localhost:5432/support"
    redis_url: str = "redis://localhost:6379/0"
    kafka_bootstrap_servers: str = "localhost:19092"
    opensearch_url: str = "http://localhost:9200"
    opensearch_user: str | None = None
    opensearch_password: SecretStr | None = None
    opensearch_index: str = "kb_chunks"  # alias; the physical index is versioned behind it
    neo4j_uri: str = "bolt://localhost:7687"

    # Shared secret for POST /api/ingest/collect (header X-Ingest-Token). Unset = endpoint off.
    ingest_webhook_token: SecretStr | None = None

    @field_validator(
        "ingest_webhook_token",
        "openai_api_key",
        "opensearch_password",
        "model_easy",
        "model_hard",
        "model_judge",
        "model_easy_reasoning",
        "model_hard_reasoning",
        "model_judge_reasoning",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # `KEY=` in .env or compose's ${KEY:-} yields "", which must not enable anything
        # (an empty ingest token would match an empty header).
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
