"""Application settings, read from environment variables (and a local .env file)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "CaviNet"
    environment: str = "development"

    # Defaults suit local development; docker-compose.yml overrides the hosts.
    database_url: str = "postgresql+psycopg://cavinet:cavinet@localhost:5432/cavinet"
    redis_url: str = "redis://localhost:6379/0"
    data_dir: str = "./data"

    analysis_queue: str = "analysis"
    # FR-04.1: largest accepted upload (1.5 GB). nginx allows a little more for form overhead.
    max_upload_bytes: int = 1536 * 1024 * 1024
    # AI model (section 11.6) and the lungmask weights baked into the image (section 11.1).
    model_path: str = "/models/cavinet_model.pth"
    lungmask_weights: str = "/opt/lungmask/unet_r231-d5d2fc3d.pth"
    # FR-05.5: pause before the one retry after an unexpected (possibly transient) error.
    analysis_retry_delay_seconds: float = 5.0
    health_check_timeout_seconds: float = 2.0

    # Authentication (FR-01.1 to FR-01.7). SECRET_KEY signs access tokens.
    secret_key: str = ""
    access_token_minutes: int = 30
    refresh_token_hours: int = 8
    lockout_threshold: int = 5
    lockout_minutes: int = 15
    # Browsers only send Secure cookies over HTTPS; the laptop demo runs on plain HTTP.
    cookie_secure: bool = False

    # First admin account, created on first start if no admin exists (FR-01.7).
    admin_email: str = ""
    admin_password: str = ""
    admin_full_name: str = "CaviNet Administrator"

    # Demo doctor created by `make seed`.
    demo_doctor_email: str = "doctor@cavinet.local"
    demo_doctor_password: str = ""
    demo_doctor_full_name: str = "Demo Doctor"


@lru_cache
def get_settings() -> Settings:
    return Settings()
