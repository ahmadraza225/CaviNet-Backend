"""The real checks must report failure (not raise) when services are unreachable."""

from app.core.database import check_database
from app.core.redis import check_redis

# Port 1 on localhost is never a PostgreSQL or Redis server.
UNREACHABLE_DB = "postgresql+psycopg://user:pass@127.0.0.1:1/none"
UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"


def test_check_database_false_when_unreachable(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DB)
    assert check_database() is False


def test_check_redis_false_when_unreachable(monkeypatch):
    monkeypatch.setenv("REDIS_URL", UNREACHABLE_REDIS)
    assert check_redis() is False


def test_check_database_true_with_sqlite(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'health.db'}")
    assert check_database() is True


def test_dispose_and_close_are_safe_to_repeat(monkeypatch, tmp_path):
    from app.core.database import dispose_engine
    from app.core.redis import close_redis

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'x.db'}")
    assert check_database() is True
    dispose_engine()
    dispose_engine()
    close_redis()
    close_redis()
