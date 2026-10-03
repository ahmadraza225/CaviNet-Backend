from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes.health import get_database_check, get_redis_check
from app.core import clock
from app.core.config import get_settings
from app.core.database import dispose_engine, get_engine, get_sessionmaker
from app.core.redis import close_redis
from app.main import create_app
from app.models import AuditLog, Base, Role, User
from app.services.users import build_user

TEST_SECRET_KEY = "test-secret-key-0123456789-abcdefghijklmnopqrstuvwxyz"
PASSWORD = "Passw0rd123"


def _clear_caches() -> None:
    dispose_engine()
    close_redis()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def test_env(monkeypatch, tmp_path):
    """Each test gets its own SQLite database and data directory, a test SECRET_KEY and a
    real-time clock."""
    monkeypatch.setenv("SECRET_KEY", TEST_SECRET_KEY)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    for name in ("ADMIN_EMAIL", "ADMIN_PASSWORD", "DEMO_DOCTOR_EMAIL", "DEMO_DOCTOR_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    _clear_caches()
    clock.reset()
    yield
    _clear_caches()
    clock.reset()


@pytest.fixture
def db() -> Session:
    Base.metadata.create_all(get_engine())
    with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def client(db) -> TestClient:
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def make_client():
    """Build a TestClient whose health checks report the given results."""

    def _make(database_ok: bool = True, redis_ok: bool = True) -> TestClient:
        app = create_app()
        app.dependency_overrides[get_database_check] = lambda: lambda: database_ok
        app.dependency_overrides[get_redis_check] = lambda: lambda: redis_ok
        return TestClient(app)

    return _make


@pytest.fixture
def make_user(db) -> Callable[..., User]:
    def _make(
        email: str = "user@example.org",
        role: Role = Role.DOCTOR,
        password: str = PASSWORD,
        *,
        full_name: str = "Test User",
        must_change_password: bool = False,
        is_active: bool = True,
    ) -> User:
        user = build_user(
            email, full_name, role, password, must_change_password=must_change_password
        )
        user.is_active = is_active
        db.add(user)
        db.commit()
        return user

    return _make


@pytest.fixture
def admin(make_user) -> User:
    return make_user("admin@example.org", Role.ADMIN, full_name="Ada Admin")


@pytest.fixture
def doctor(make_user) -> User:
    return make_user("doctor@example.org", Role.DOCTOR, full_name="Dan Doctor")


def login(client: TestClient, email: str, password: str = PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(client: TestClient, email: str, password: str = PASSWORD) -> dict[str, str]:
    response = login(client, email, password)
    assert response.status_code == 200, response.json()
    return bearer(response.json()["access_token"])


@pytest.fixture
def admin_headers(client, admin) -> dict[str, str]:
    return token_for(client, admin.email)


@pytest.fixture
def doctor_headers(client, doctor) -> dict[str, str]:
    return token_for(client, doctor.email)


def audit_entries(db: Session, action: str | None = None) -> list[AuditLog]:
    """Audit rows written by the app (which uses its own sessions)."""
    db.expire_all()
    query = select(AuditLog).order_by(AuditLog.id)
    if action:
        query = query.where(AuditLog.action == action)
    return list(db.scalars(query))


def refresh_cookie(response) -> str | None:
    """Value of the refresh cookie set by a response (None if cleared)."""
    for header in response.headers.get_list("set-cookie"):
        if header.startswith("cavinet_refresh="):
            value = header.split(";", 1)[0].split("=", 1)[1]
            return value.strip('"') or None
    return None


def set_env(monkeypatch, **values: str) -> None:
    """Set environment variables for this test and reload settings."""
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
