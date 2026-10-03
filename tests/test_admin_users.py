"""FR-09.1 / FR-01.6: user administration by admins."""

import uuid
from datetime import timedelta

import pytest

from app.core import clock
from app.models import AuditAction, Role
from app.services.errors import Conflict
from app.services.users import _ensure_not_last_admin
from tests.conftest import PASSWORD, audit_entries, bearer, login

NEW_DOCTOR = {
    "email": "Dr.Who@Example.org",
    "full_name": "  Dr   Who ",
    "role": "doctor",
    "temporary_password": "Temp-pass123",
}


def create(client, headers, **overrides):
    return client.post("/api/admin/users", json={**NEW_DOCTOR, **overrides}, headers=headers)


def test_fr09_1_admin_lists_users(client, admin_headers, admin, doctor):
    response = client.get("/api/admin/users", headers=admin_headers)
    assert response.status_code == 200
    emails = {user["email"] for user in response.json()}
    assert emails == {admin.email, doctor.email}
    row = next(u for u in response.json() if u["email"] == doctor.email)
    assert set(row) == {
        "id",
        "email",
        "full_name",
        "role",
        "is_active",
        "must_change_password",
        "is_locked",
        "last_login_at",
        "created_at",
    }
    assert "password_hash" not in response.text and "$argon2" not in response.text


def test_fr09_1_admin_creates_a_doctor_with_a_temporary_password(client, admin_headers, admin, db):
    response = create(client, admin_headers)

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "dr.who@example.org"
    assert body["full_name"] == "Dr Who"
    assert body["role"] == "doctor"
    assert body["must_change_password"] is True
    entry = audit_entries(db, AuditAction.USER_CREATED)[0]
    assert entry.user_id == admin.id
    assert entry.target_type == "user" and entry.target_id == body["id"]
    assert entry.details == {"target_email": "dr.who@example.org", "role": "doctor"}


def test_created_doctor_must_change_the_temporary_password_at_first_login(client, admin_headers):
    create(client, admin_headers)
    first = login(client, "dr.who@example.org", "Temp-pass123")
    assert first.status_code == 200
    assert first.json()["user"]["must_change_password"] is True
    clock.advance(timedelta(seconds=1))
    changed = client.post(
        "/api/auth/change-password",
        json={"current_password": "Temp-pass123", "new_password": "My-own-pass1"},
        headers=bearer(first.json()["access_token"]),
    )
    assert changed.status_code == 200
    assert changed.json()["user"]["must_change_password"] is False


@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({"email": "not-an-email"}, 422),
        ({"full_name": "   "}, 422),
        ({"role": "superuser"}, 422),
        ({"temporary_password": "short1"}, 422),
        ({"temporary_password": "nodigitshere"}, 422),
    ],
)
def test_create_user_validates_input(client, admin_headers, overrides, status):
    assert create(client, admin_headers, **overrides).status_code == status


def test_create_user_rejects_duplicate_email(client, admin_headers, doctor):
    response = create(client, admin_headers, email="DOCTOR@example.org")
    assert response.status_code == 409
    assert response.json()["code"] == "email_taken"


def test_fr09_1_admin_assigns_role_and_renames(client, admin_headers, doctor, db):
    response = client.patch(
        f"/api/admin/users/{doctor.id}",
        json={"role": "admin", "full_name": "Dana Doctor-Admin"},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["role"] == "admin"
    assert response.json()["full_name"] == "Dana Doctor-Admin"
    entry = audit_entries(db, AuditAction.USER_UPDATED)[0]
    assert entry.details["role"] == "doctor -> admin"

    # No change, no audit entry.
    client.patch(f"/api/admin/users/{doctor.id}", json={"role": "admin"}, headers=admin_headers)
    assert len(audit_entries(db, AuditAction.USER_UPDATED)) == 1


def test_admin_cannot_change_own_role_or_deactivate_self(client, admin_headers, admin):
    demote = client.patch(
        f"/api/admin/users/{admin.id}", json={"role": "doctor"}, headers=admin_headers
    )
    assert demote.status_code == 409 and demote.json()["code"] == "self_action"
    deactivate = client.post(f"/api/admin/users/{admin.id}/deactivate", headers=admin_headers)
    assert deactivate.status_code == 409 and deactivate.json()["code"] == "self_action"


def test_last_active_admin_is_protected(db, admin):
    with pytest.raises(Conflict, match="last active administrator"):
        _ensure_not_last_admin(db, admin, "deactivate")


def test_fr09_1_deactivation_ends_access_and_reactivation_restores_it(
    client, admin_headers, doctor, db
):
    doctor_login = login(client, doctor.email)
    doctor_headers = bearer(doctor_login.json()["access_token"])

    response = client.post(f"/api/admin/users/{doctor.id}/deactivate", headers=admin_headers)
    assert response.status_code == 200 and response.json()["is_active"] is False
    assert client.get("/api/auth/me", headers=doctor_headers).status_code == 401
    assert client.post("/api/auth/refresh").status_code == 401  # doctor's cookie, revoked
    assert login(client, doctor.email).status_code == 403

    response = client.post(f"/api/admin/users/{doctor.id}/reactivate", headers=admin_headers)
    assert response.status_code == 200 and response.json()["is_active"] is True
    assert login(client, doctor.email).status_code == 200

    actions = [e.action for e in audit_entries(db) if e.target_id == str(doctor.id)]
    assert actions == [AuditAction.USER_DEACTIVATED, AuditAction.USER_REACTIVATED]


def test_fr01_6_admin_resets_a_password_to_a_temporary_one(client, admin_headers, doctor, db):
    old_session = login(client, doctor.email)
    for _ in range(5):  # lock the account
        login(client, doctor.email, "Wrong-passw0rd")
    clock.advance(timedelta(seconds=1))

    response = client.post(
        f"/api/admin/users/{doctor.id}/reset-password",
        json={"temporary_password": "Reset-pass123"},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["must_change_password"] is True
    assert response.json()["is_locked"] is False

    # Old password and old sessions no longer work; the temporary one forces a change.
    old_headers = bearer(old_session.json()["access_token"])
    assert client.get("/api/auth/me", headers=old_headers).status_code == 401
    assert login(client, doctor.email, PASSWORD).status_code == 401
    fresh = login(client, doctor.email, "Reset-pass123")
    assert fresh.status_code == 200 and fresh.json()["user"]["must_change_password"] is True
    assert len(audit_entries(db, AuditAction.PASSWORD_RESET)) == 1


def test_reset_password_enforces_policy(client, admin_headers, doctor):
    response = client.post(
        f"/api/admin/users/{doctor.id}/reset-password",
        json={"temporary_password": "weak"},
        headers=admin_headers,
    )
    assert response.status_code == 422 and response.json()["code"] == "weak_password"


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("patch", ""), ("post", "/deactivate"), ("post", "/reactivate"), ("post", "/reset-password")],
)
def test_unknown_user_is_404(client, admin_headers, method, suffix):
    body = {"temporary_password": "Reset-pass123"} if suffix == "/reset-password" else {}
    response = getattr(client, method)(
        f"/api/admin/users/{uuid.uuid4()}{suffix}", json=body, headers=admin_headers
    )
    assert response.status_code == 404


def test_role_values(client, admin_headers):
    assert create(client, admin_headers, role=Role.ADMIN.value).json()["role"] == "admin"
