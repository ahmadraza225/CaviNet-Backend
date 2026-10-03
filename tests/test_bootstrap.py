"""FR-01.7: the first admin account is created from environment variables on first start."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import AuditAction, Role, User
from tests.conftest import audit_entries, login, set_env


def admins(db):
    db.expire_all()
    return list(db.scalars(select(User).where(User.role == Role.ADMIN)))


def test_fr01_7_initial_admin_created_on_first_start(monkeypatch, db):
    set_env(monkeypatch, ADMIN_EMAIL="Head.Admin@Hospital.org", ADMIN_PASSWORD="First-admin123")

    with TestClient(create_app()) as client:
        [admin] = admins(db)
        assert admin.email == "head.admin@hospital.org"
        assert admin.must_change_password is True
        response = login(client, "head.admin@hospital.org", "First-admin123")
        assert response.status_code == 200
        assert response.json()["user"]["must_change_password"] is True
    created = audit_entries(db, AuditAction.USER_CREATED)[0]
    assert created.actor_email == "system" and created.details["source"] == "initial admin"


def test_initial_admin_is_created_only_once(monkeypatch, db):
    set_env(monkeypatch, ADMIN_EMAIL="admin@hospital.org", ADMIN_PASSWORD="First-admin123")
    with TestClient(create_app()):
        pass
    set_env(monkeypatch, ADMIN_EMAIL="second@hospital.org")
    with TestClient(create_app()):
        pass
    assert [a.email for a in admins(db)] == ["admin@hospital.org"]


def test_no_admin_without_environment_variables(db, caplog):
    with TestClient(create_app()):
        pass
    assert admins(db) == []
    assert "ADMIN_EMAIL/ADMIN_PASSWORD are not set" in caplog.text


def test_weak_admin_password_is_refused(monkeypatch, db, caplog):
    set_env(monkeypatch, ADMIN_EMAIL="admin@hospital.org", ADMIN_PASSWORD="weak")
    with TestClient(create_app()):
        pass
    assert admins(db) == []
    assert "password policy" in caplog.text


def test_startup_fails_without_secret_key(monkeypatch, db):
    set_env(monkeypatch, SECRET_KEY="")
    with pytest.raises(RuntimeError, match="SECRET_KEY"), TestClient(create_app()):
        pass
