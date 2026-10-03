"""Application settings, read from environment variables (and a local .env file)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "CaviNet"
    environment: str = "development"

    # Defaults suit local development; docker-compose.yml overrides the hosts.
    database_url: str = "postgresql+psycopg://cavinet:cavinet@localhost:5432/cavinet"
    redis_url: str = "redis://localhost:6379/0"
    data_dir: str = "./data"

    analysis_queue: str = "analysis"
    health_check_timeout_seconds: float = 2.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
