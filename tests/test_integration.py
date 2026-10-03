"""Integration tests against real PostgreSQL and Redis (run in CI service containers).

Set TEST_DATABASE_URL and TEST_REDIS_URL to run them locally.
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.database import get_sessionmaker
from app.main import create_app
from tests.conftest import bearer, login, set_env

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")
BACKEND_DIR = Path(__file__).resolve().parents[1]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (DATABASE_URL and REDIS_URL),
        reason="TEST_DATABASE_URL and TEST_REDIS_URL not set",
    ),
]


def alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


@pytest.fixture
def live_env(monkeypatch):
    set_env(monkeypatch, DATABASE_URL=DATABASE_URL, REDIS_URL=REDIS_URL)


@pytest.fixture
def clean_db(live_env):
    command.upgrade(alembic_config(), "head")
    with get_sessionmaker()() as session:
        for table in ("audit_logs", "refresh_tokens", "users"):
            session.execute(text(f"DELETE FROM {table}"))
        session.commit()


def test_health_ok_against_real_services(live_env):
    response = TestClient(create_app()).get("/api/health")
    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "ok"


def test_migrations_upgrade_downgrade_and_match_the_models(live_env):
    config = alembic_config()
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    # Fails if the models and the migrations disagree (autogenerate would produce changes).
    command.check(config)


def test_account_flow_on_postgres(clean_db, monkeypatch):
    set_env(monkeypatch, ADMIN_EMAIL="admin@hospital.org", ADMIN_PASSWORD="First-admin123")
    with TestClient(create_app()) as client:
        first = login(client, "admin@hospital.org", "First-admin123")
        assert first.status_code == 200 and first.json()["user"]["must_change_password"]
        changed = client.post(
            "/api/auth/change-password",
            json={"current_password": "First-admin123", "new_password": "Admin-own-pass1"},
            headers=bearer(first.json()["access_token"]),
        )
        assert changed.status_code == 200
        admin_headers = bearer(changed.json()["access_token"])

        created = client.post(
            "/api/admin/users",
            json={
                "email": "dr@hospital.org",
                "full_name": "Dr Pg",
                "role": "doctor",
                "temporary_password": "Temp-pass123",
            },
            headers=admin_headers,
        )
        assert created.status_code == 201
        doctor_id = created.json()["id"]

        for _ in range(4):
            assert login(client, "dr@hospital.org", "Wrong-passw0rd").status_code == 401
        assert login(client, "dr@hospital.org", "Wrong-passw0rd").status_code == 423

        reset = client.post(
            f"/api/admin/users/{doctor_id}/reset-password",
            json={"temporary_password": "Reset-pass123"},
            headers=admin_headers,
        )
        assert reset.status_code == 200 and reset.json()["is_locked"] is False
        doctor = login(client, "dr@hospital.org", "Reset-pass123")
        assert doctor.status_code == 200 and doctor.json()["user"]["must_change_password"]
        assert client.post("/api/auth/refresh").status_code == 200
        assert client.post("/api/auth/logout").status_code == 204
        assert client.post("/api/auth/refresh").status_code == 401

        audit = client.get(
            f"/api/admin/audit-logs?user_id={doctor_id}&action=account_locked",
            headers=admin_headers,
        )
        assert audit.status_code == 200 and audit.json()["total"] == 1
        everything = client.get("/api/admin/audit-logs?page_size=100", headers=admin_headers)
        actions = {item["action"] for item in everything.json()["items"]}
        assert {
            "user_created",
            "password_changed",
            "login_failure",
            "password_reset",
            "logout",
        } <= actions
