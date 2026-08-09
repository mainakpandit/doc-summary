"""Application settings, read once through pydantic-settings from `.env`."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Async SQLAlchemy connection string for the single Postgres instance
    # that holds state, vectors, audit log, and LangGraph checkpoints.
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/pm_analyst"

    # Credentials the docker-compose.yml `db` service uses to initialize the
    # Postgres container. Keep these in sync with DATABASE_URL above.
    POSTGRES_USER: str = "pm_analyst"
    POSTGRES_PASSWORD: str = "pm_analyst"
    POSTGRES_DB: str = "pm_analyst"

    # Anthropic API key used by services/llm.py for every Claude call. Never
    # commit a real key; tests never require this to be set.
    ANTHROPIC_API_KEY: str | None = None

    # Embedding model used by services/embeddings.py to embed chunks and
    # queries. Must stay consistent for a given corpus's vector column.
    EMBEDDING_MODEL: str = "voyage-3"

    # Dimensionality of the embedding vectors produced by EMBEDDING_MODEL;
    # must match the `vector(N)` column size in the chunks table migration.
    EMBEDDING_DIM: int = 1536

    # Filesystem root the ingestion pipeline and inbox watcher read
    # documents from (corpus/demo, corpus/demo2, corpus/inbox all live
    # under this root).
    CORPUS_ROOT: Path = Path("./corpus")

    # How often, in seconds, services/watcher.py polls corpus/inbox for new
    # files to enqueue as update runs.
    INBOX_POLL_SECONDS: int = 2

    # Maximum number of runs the worker will process concurrently via
    # SELECT ... FOR UPDATE SKIP LOCKED.
    MAX_CONCURRENT_RUNS: int = 4

    # Per-run USD ceiling enforced by services/llm.call_claude; a run that
    # would exceed this raises BudgetExceeded instead of silently
    # overspending.
    COST_BUDGET_USD_PER_RUN: float = 5.0

    # Log level for structlog output.
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Deployment environment; gates things like verbose error responses and
    # which .env values are expected to be set.
    ENVIRONMENT: Literal["dev", "test", "prod"] = "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
