import pytest
from fastapi.testclient import TestClient

from app.api.routes.health import get_database_check, get_redis_check
from app.core.config import get_settings
from app.core.database import dispose_engine
from app.core.redis import close_redis
from app.main import create_app


def _clear_caches() -> None:
    dispose_engine()
    close_redis()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def fresh_settings():
    """Each test sees settings built from its own environment."""
    _clear_caches()
    yield
    _clear_caches()


@pytest.fixture
def make_client():
    """Build a TestClient whose health checks report the given results."""

    def _make(database_ok: bool = True, redis_ok: bool = True) -> TestClient:
        app = create_app()
        app.dependency_overrides[get_database_check] = lambda: lambda: database_ok
        app.dependency_overrides[get_redis_check] = lambda: lambda: redis_ok
        return TestClient(app)

    return _make
