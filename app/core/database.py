"""Database engine, sessions and the health check."""

import logging
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    connect_args = {}
    if settings.database_url.startswith("postgresql"):
        connect_args["connect_timeout"] = max(1, int(settings.health_check_timeout_seconds))
    engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)
    if engine.dialect.name == "sqlite":
        # SQLite (used by the unit tests) only enforces foreign keys, and therefore
        # ON DELETE CASCADE, when asked to on every connection.
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def _enable_sqlite_foreign_keys(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a database session."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def check_database() -> bool:
    """Return True if the database answers a trivial query."""
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - any failure means "not healthy"
        logger.warning("Database health check failed", exc_info=True)
        return False


def dispose_engine() -> None:
    """Close pooled connections and forget the engine (app shutdown, tests)."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_sessionmaker.cache_clear()
    get_engine.cache_clear()
