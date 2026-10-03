"""M-01: sign-in, lockout, sessions, sign-out and password change."""

from datetime import timedelta

from fastapi.testclient import TestClient

from app.core import clock
from app.main import create_app
from app.models import AuditAction, Role
from tests.conftest import PASSWORD, audit_entries, bearer, login, refresh_cookie, token_for

# ---- FR-01.1 / FR-01.2: sign-in ------------------------------------------------------------


def test_fr01_2_login_success_returns_access_token_and_httponly_refresh_cookie(client, doctor, db):
    response = login(client, doctor.email)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 30 * 60
    assert body["user"]["email"] == doctor.email
    assert body["user"]["role"] == "doctor"
    assert response.headers["cache-control"] == "no-store"

    cookie = response.headers["set-cookie"]
    assert cookie.startswith("cavinet_refresh=")
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/api/auth" in cookie
    max_age = int(cookie.split("Max-Age=")[1].split(";")[0])
    assert 8 * 3600 - 5 <= max_age <= 8 * 3600

    me = client.get("/api/auth/me", headers=bearer(body["access_token"]))
    assert me.status_code == 200 and me.json()["email"] == doctor.email
    assert [e.action for e in audit_entries(db)] == [AuditAction.LOGIN_SUCCESS]
    db.refresh(doctor)
    assert doctor.last_login_at is not None


def test_login_email_is_case_and_space_insensitive(client, doctor):
    assert login(client, "  DOCTOR@Example.org ").status_code == 200


def test_fr01_1_wrong_password_and_unknown_email_get_the_same_generic_401(client, doctor, db):
    wrong = login(client, doctor.email, "Wrong-passw0rd")
    unknown = login(client, "nobody@example.org", "Wrong-passw0rd")

    for response in (wrong, unknown):
        assert response.status_code == 401
        assert response.json() == {
            "detail": "Incorrect email or password.",
            "code": "invalid_credentials",
        }
        assert "set-cookie" not in response.headers
    failures = audit_entries(db, AuditAction.LOGIN_FAILURE)
    assert [f.details["reason"] for f in failures] == ["wrong_password", "unknown_email"]
    assert failures[0].user_id == doctor.id
    assert failures[1].user_id is None and failures[1].actor_email == "nobody@example.org"


def test_login_validates_input(client):
    assert client.post("/api/auth/login", json={"email": "a@b.org"}).status_code == 422
    too_long = {"email": "a@b.org", "password": "x" * 129}
    assert client.post("/api/auth/login", json=too_long).status_code == 422


# ---- FR-01.4: lockout --------------------------------------------------------------------


def test_fr01_4_account_locks_for_15_minutes_after_5_failed_logins(client, doctor, db):
    for attempt in range(4):
        assert login(client, doctor.email, "Wrong-passw0rd").status_code == 401, attempt

    fifth = login(client, doctor.email, "Wrong-passw0rd")
    assert fifth.status_code == 423
    assert fifth.json()["code"] == "account_locked"
    assert "15 minute" in fifth.json()["detail"]

    # Even the correct password is refused while locked.
    locked = login(client, doctor.email)
    assert locked.status_code == 423
    assert len(audit_entries(db, AuditAction.ACCOUNT_LOCKED)) == 1

    clock.advance(timedelta(minutes=14, seconds=50))
    assert login(client, doctor.email).status_code == 423
    clock.advance(timedelta(seconds=11))
    assert login(client, doctor.email).status_code == 200


def test_fr01_4_successful_login_resets_the_failure_counter(client, doctor):
    for _ in range(4):
        login(client, doctor.email, "Wrong-passw0rd")
    assert login(client, doctor.email).status_code == 200
    for _ in range(4):
        assert login(client, doctor.email, "Wrong-passw0rd").status_code == 401
    assert login(client, doctor.email).status_code == 200


def test_deactivated_account_cannot_sign_in(client, make_user, db):
    user = make_user("gone@example.org", is_active=False)

    response = login(client, user.email)
    assert response.status_code == 403
    assert response.json()["code"] == "account_inactive"
    # A wrong password does not reveal that the account is deactivated.
    assert login(client, user.email, "Wrong-passw0rd").status_code == 401


# ---- FR-01.2: access token, refresh and sign-out --------------------------------------------


def test_fr01_2_access_token_is_rejected_after_30_minutes(client, doctor):
    headers = token_for(client, doctor.email)
    clock.advance(timedelta(minutes=31))
    response = client.get("/api/auth/me", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_requests_without_valid_token_get_401(client):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers=bearer("garbage")).status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Basic abc"}).status_code == 401


def test_fr01_2_refresh_issues_a_new_access_token_and_rotates_the_cookie(client, doctor):
    first = login(client, doctor.email)
    old_cookie = refresh_cookie(first)

    refreshed = client.post("/api/auth/refresh")
    assert refreshed.status_code == 200
    new_cookie = refresh_cookie(refreshed)
    assert new_cookie and new_cookie != old_cookie
    me = client.get("/api/auth/me", headers=bearer(refreshed.json()["access_token"]))
    assert me.status_code == 200


def test_reusing_a_rotated_refresh_token_ends_every_session(doctor, db):
    with TestClient(create_app()) as attacker, TestClient(create_app()) as victim:
        stolen = refresh_cookie(login(victim, doctor.email))
        assert victim.post("/api/auth/refresh").status_code == 200  # rotation

        attacker.cookies.set("cavinet_refresh", stolen, path="/api/auth")
        assert attacker.post("/api/auth/refresh").status_code == 401
        # The victim's current token was revoked too, as a precaution.
        assert victim.post("/api/auth/refresh").status_code == 401


def test_fr01_2_session_ends_8_hours_after_sign_in_even_with_refreshes(client, doctor):
    login(client, doctor.email)
    clock.advance(timedelta(hours=7, minutes=50))
    assert client.post("/api/auth/refresh").status_code == 200
    clock.advance(timedelta(minutes=11))
    assert client.post("/api/auth/refresh").status_code == 401


def test_refresh_without_cookie_is_401(client):
    response = client.post("/api/auth/refresh")
    assert response.status_code == 401
    assert response.json()["code"] == "no_session"


def test_fr01_2_logout_revokes_the_refresh_token(client, doctor, db):
    login(client, doctor.email)
    response = client.post("/api/auth/logout")

    assert response.status_code == 204
    assert refresh_cookie(response) is None  # cookie cleared
    assert client.post("/api/auth/refresh").status_code == 401
    logout_entry = audit_entries(db, AuditAction.LOGOUT)[0]
    assert logout_entry.user_id == doctor.id


def test_logout_without_session_is_harmless(client, db):
    assert client.post("/api/auth/logout").status_code == 204
    assert audit_entries(db, AuditAction.LOGOUT) == []


# ---- FR-01.6: forced password change and own password change --------------------------------


def test_fr01_6_temporary_password_forces_a_change_before_anything_else(client, make_user, db):
    user = make_user("new.admin@example.org", Role.ADMIN, "Temp-12345", must_change_password=True)

    response = login(client, user.email, "Temp-12345")
    assert response.status_code == 200
    assert response.json()["user"]["must_change_password"] is True
    headers = bearer(response.json()["access_token"])

    blocked = client.get("/api/admin/users", headers=headers)
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "password_change_required"
    assert client.get("/api/auth/me", headers=headers).status_code == 200

    clock.advance(timedelta(seconds=1))
    changed = client.post(
        "/api/auth/change-password",
        json={"current_password": "Temp-12345", "new_password": "Brand-new-pass1"},
        headers=headers,
    )
    assert changed.status_code == 200
    assert changed.json()["user"]["must_change_password"] is False
    new_headers = bearer(changed.json()["access_token"])
    assert client.get("/api/admin/users", headers=new_headers).status_code == 200
    # The token issued with the temporary password no longer works.
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    assert login(client, user.email, "Temp-12345").status_code == 401
    assert login(client, user.email, "Brand-new-pass1").status_code == 200
    assert len(audit_entries(db, AuditAction.PASSWORD_CHANGED)) == 1


def test_change_password_rules(client, doctor):
    headers = token_for(client, doctor.email)

    def change(current, new):
        return client.post(
            "/api/auth/change-password",
            json={"current_password": current, "new_password": new},
            headers=headers,
        )

    assert change("Wrong-passw0rd", "Another-pass1").json()["code"] == "wrong_current_password"
    weak = change(PASSWORD, "short1")
    assert weak.status_code == 422 and weak.json()["code"] == "weak_password"
    assert change(PASSWORD, PASSWORD).json()["code"] == "password_reused"
    assert change(PASSWORD, "Another-pass1").status_code == 200


def test_changing_password_ends_other_sessions(doctor):
    with TestClient(create_app()) as laptop, TestClient(create_app()) as phone:
        login(phone, doctor.email)
        headers = token_for(laptop, doctor.email)
        laptop.post(
            "/api/auth/change-password",
            json={"current_password": PASSWORD, "new_password": "Another-pass1"},
            headers=headers,
        )
        assert phone.post("/api/auth/refresh").status_code == 401
        assert laptop.post("/api/auth/refresh").status_code == 200


def test_login_audit_records_client_address(client, doctor, db):
    login(client, doctor.email)
    assert audit_entries(db)[0].ip_address == "testclient"
