from app.core.config import get_settings


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://a:b@db:5432/x")
    monkeypatch.setenv("REDIS_URL", "redis://cache:6379/1")
    monkeypatch.setenv("ANALYSIS_QUEUE", "custom")
    settings = get_settings()
    assert settings.database_url == "postgresql+psycopg://a:b@db:5432/x"
    assert settings.redis_url == "redis://cache:6379/1"
    assert settings.analysis_queue == "custom"


def test_settings_defaults(monkeypatch):
    for name in ("DATABASE_URL", "REDIS_URL", "ANALYSIS_QUEUE", "DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    settings = get_settings()
    assert settings.analysis_queue == "analysis"
    assert settings.redis_url.startswith("redis://")
