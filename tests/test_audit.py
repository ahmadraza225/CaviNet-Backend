"""FR-09.2 (what is recorded) and FR-09.3 (viewing and filtering the audit log)."""

import json
from datetime import timedelta

from app.core import clock
from app.models import AuditAction
from tests.conftest import (
    PASSWORD,
    audit_entries,
    bearer,
    login,
    run_queued_jobs,
    scan_zip,
    token_for,
    upload,
)


def test_fr09_2_every_audit_action_is_recorded(client, admin, doctor, db, analysis_queue):
    """Runs every logged action once and checks each produced an audit entry: everything
    FR-09.2 lists, from sign-in to the report download (FR-07.3)."""
    admin_headers = token_for(client, admin.email)
    doctor_headers = token_for(client, doctor.email)
    patient = client.post(
        "/api/patients",
        json={
            "full_name": "Test Patient",
            "mr_number": "MR-1",
            "date_of_birth": "1980-01-01",
            "sex": "female",
        },
        headers=doctor_headers,
    ).json()
    patient_url = f"/api/patients/{patient['id']}"
    client.patch(patient_url, json={"phone": "0300 1234567"}, headers=doctor_headers)
    case = upload(client, doctor_headers, patient["id"], [("scan.zip", scan_zip(50))]).json()
    run_queued_jobs(analysis_queue)
    client.get(f"/api/cases/{case['id']}/result", headers=doctor_headers)  # result_viewed
    client.get(f"/api/cases/{case['id']}/report", headers=doctor_headers)  # report_downloaded
    client.delete(f"{patient_url}?confirm=MR-1", headers=doctor_headers)
    login(client, "nobody@example.org", "Wrong-passw0rd")  # login_failure
    created = client.post(
        "/api/admin/users",
        json={
            "email": "dr@example.org",
            "full_name": "Dr",
            "role": "doctor",
            "temporary_password": "Temp-pass123",
        },
        headers=admin_headers,
    ).json()
    user_url = f"/api/admin/users/{created['id']}"
    client.patch(user_url, json={"full_name": "Dr Renamed"}, headers=admin_headers)
    client.post(
        f"{user_url}/reset-password",
        json={"temporary_password": "Reset-pass123"},
        headers=admin_headers,
    )
    client.post(f"{user_url}/deactivate", headers=admin_headers)
    client.post(f"{user_url}/reactivate", headers=admin_headers)
    for _ in range(5):
        login(client, "dr@example.org", "Wrong-passw0rd")  # account_locked
    clock.advance(timedelta(minutes=16))
    session = login(client, "dr@example.org", "Reset-pass123")
    clock.advance(timedelta(seconds=1))
    client.post(
        "/api/auth/change-password",
        json={"current_password": "Reset-pass123", "new_password": "Own-pass12345"},
        headers=bearer(session.json()["access_token"]),
    )
    client.post("/api/auth/logout")

    recorded = {entry.action for entry in audit_entries(db)}
    assert recorded == {action.value for action in AuditAction}


def test_fr09_2_audit_entries_never_contain_passwords(client, admin, db):
    admin_headers = token_for(client, admin.email)
    login(client, admin.email, "Wrong-passw0rd")
    client.post(
        "/api/admin/users",
        json={
            "email": "dr@example.org",
            "full_name": "Dr",
            "role": "doctor",
            "temporary_password": "Temp-pass123",
        },
        headers=admin_headers,
    )
    dump = json.dumps(
        [[e.actor_email, e.action, e.target_id, e.details] for e in audit_entries(db)]
    )
    for secret in (PASSWORD, "Wrong-passw0rd", "Temp-pass123"):
        assert secret not in dump


def test_fr09_3_audit_log_lists_newest_first_with_pagination(client, admin_headers, doctor):
    for _ in range(3):
        login(client, doctor.email, "Wrong-passw0rd")
    page = client.get("/api/admin/audit-logs?page_size=2", headers=admin_headers).json()
    assert page["total"] == 4  # admin login + 3 failures
    assert page["page"] == 1 and page["page_size"] == 2 and len(page["items"]) == 2
    assert page["items"][0]["action"] == "login_failure"
    times = [item["created_at"] for item in page["items"]]
    assert times == sorted(times, reverse=True)
    assert times[0].endswith("Z") or "+00:00" in times[0]
    last = client.get("/api/admin/audit-logs?page_size=2&page=2", headers=admin_headers).json()
    assert [i["action"] for i in last["items"]][-1] == "login_success"


def test_fr09_3_filter_by_user_and_action(client, admin, admin_headers, doctor):
    login(client, doctor.email)
    login(client, doctor.email, "Wrong-passw0rd")

    by_user = client.get(f"/api/admin/audit-logs?user_id={doctor.id}", headers=admin_headers)
    assert {i["user_id"] for i in by_user.json()["items"]} == {str(doctor.id)}  # no admin acts
    assert by_user.json()["total"] == 2

    by_action = client.get("/api/admin/audit-logs?action=login_success", headers=admin_headers)
    assert {i["action"] for i in by_action.json()["items"]} == {"login_success"}
    assert by_action.json()["total"] == 2  # admin + doctor

    both = client.get(
        f"/api/admin/audit-logs?user_id={doctor.id}&action=login_failure", headers=admin_headers
    )
    assert both.json()["total"] == 1


def test_fr09_3_filter_by_date_range(client, admin, doctor):
    day1 = clock.utcnow().date()
    login(client, doctor.email)  # day 1
    clock.advance(timedelta(days=2))
    headers = token_for(client, admin.email)  # day 3
    day3 = clock.utcnow().date()

    def total(query):
        return client.get(f"/api/admin/audit-logs?{query}", headers=headers).json()["total"]

    assert total(f"date_from={day1}&date_to={day1}") == 1
    assert total(f"date_from={day3}") == 1
    assert total(f"date_to={day3}") == 2
    assert total(f"date_from={day1 + timedelta(days=1)}&date_to={day1 + timedelta(days=1)}") == 0


def test_audit_filters_are_validated(client, admin_headers):
    bad_range = client.get(
        "/api/admin/audit-logs?date_from=2026-10-05&date_to=2026-10-01", headers=admin_headers
    )
    assert bad_range.status_code == 422 and bad_range.json()["code"] == "bad_range"
    assert client.get("/api/admin/audit-logs?action=nope", headers=admin_headers).status_code == 422
    assert client.get("/api/admin/audit-logs?page=0", headers=admin_headers).status_code == 422
    assert (
        client.get("/api/admin/audit-logs?page_size=500", headers=admin_headers).status_code == 422
    )
    assert client.get("/api/admin/audit-logs?user_id=x", headers=admin_headers).status_code == 422


def test_audit_action_list_for_the_filter(client, admin_headers):
    response = client.get("/api/admin/audit-logs/actions", headers=admin_headers)
    assert response.json() == [action.value for action in AuditAction]


def test_fr09_3_user_filter_includes_actions_on_that_users_account(client, admin, doctor):
    headers = token_for(client, admin.email)
    client.post(f"/api/admin/users/{doctor.id}/deactivate", headers=headers)
    client.post(f"/api/admin/users/{doctor.id}/reactivate", headers=headers)
    login(client, doctor.email)

    page = client.get(f"/api/admin/audit-logs?user_id={doctor.id}", headers=headers).json()
    assert [item["action"] for item in page["items"]] == [
        "login_success",
        "user_reactivated",
        "user_deactivated",
    ]
    assert page["items"][1]["actor_email"] == admin.email
