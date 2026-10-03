"""FR-08.2 and FR-08.3: notifications list, unread count, mark read, mark all read."""

import json
from datetime import timedelta

import pytest

from app.core import clock
from app.models import Notification
from tests.conftest import create_patient, run_queued_jobs, scan_zip, token_for, upload


@pytest.fixture
def finished(client, doctor_headers, analysis_queue) -> list[str]:
    """Two notifications for the doctor: a completed case, then a failed one."""
    patient = create_patient(client, doctor_headers)
    done = upload(client, doctor_headers, patient["id"], [("a.zip", scan_zip(50))]).json()
    run_queued_jobs(analysis_queue)
    clock.advance(timedelta(seconds=2))
    failed = upload(client, doctor_headers, patient["id"], [("b.zip", scan_zip(10))]).json()
    return [done["id"], failed["id"]]


def listing(client, headers, **params):
    response = client.get("/api/notifications", params=params, headers=headers)
    assert response.status_code == 200, response.json()
    return response.json()


def test_fr08_2_list_newest_first_with_plain_messages(client, doctor_headers, finished):
    body = listing(client, doctor_headers)
    assert body["total"] == 2 and body["page"] == 1 and body["page_size"] == 20
    first, second = body["items"]
    assert (first["kind"], first["case_id"], first["read"]) == ("case_failed", finished[1], False)
    assert first["message"] == (
        "Scan analysis failed for Amina Bibi (MR-1001). Open the case to see why."
    )
    assert (second["kind"], second["case_id"]) == ("case_completed", finished[0])
    assert second["message"] == "Scan analysis completed for Amina Bibi (MR-1001)."


def test_fr08_2_unread_count(client, doctor_headers, finished):
    response = client.get("/api/notifications/unread-count", headers=doctor_headers)
    assert response.json() == {"count": 2}


def test_fr08_3_mark_one_read(client, doctor_headers, finished):
    first = listing(client, doctor_headers)["items"][0]
    response = client.post(f"/api/notifications/{first['id']}/read", headers=doctor_headers)
    assert response.json() == {"marked": 1}
    # Marking again is harmless.
    client.post(f"/api/notifications/{first['id']}/read", headers=doctor_headers)
    assert client.get("/api/notifications/unread-count", headers=doctor_headers).json() == {
        "count": 1
    }
    items = listing(client, doctor_headers)["items"]
    assert [item["read"] for item in items] == [True, False]
    assert listing(client, doctor_headers, unread_only=True)["total"] == 1


def test_fr08_3_mark_all_read(client, doctor_headers, finished):
    response = client.post("/api/notifications/read-all", headers=doctor_headers)
    assert response.json() == {"marked": 2}
    assert client.get("/api/notifications/unread-count", headers=doctor_headers).json() == {
        "count": 0
    }
    assert client.post("/api/notifications/read-all", headers=doctor_headers).json() == {
        "marked": 0
    }


def test_doctors_cannot_see_or_mark_each_others_notifications(
    client, doctor_headers, make_user, finished
):
    other = make_user("other@example.org")
    other_headers = token_for(client, other.email)
    assert listing(client, other_headers)["total"] == 0
    first = listing(client, doctor_headers)["items"][0]
    response = client.post(f"/api/notifications/{first['id']}/read", headers=other_headers)
    assert response.status_code == 404
    client.post("/api/notifications/read-all", headers=other_headers)
    assert client.get("/api/notifications/unread-count", headers=doctor_headers).json() == {
        "count": 2
    }


def test_unknown_notification_is_404(client, doctor_headers):
    response = client.post("/api/notifications/999/read", headers=doctor_headers)
    assert response.status_code == 404
    assert response.json() == {"detail": "Notification not found.", "code": "not_found"}


def test_notifications_are_doctor_only(client, admin_headers):
    for method, path in [
        ("GET", "/api/notifications"),
        ("GET", "/api/notifications/unread-count"),
        ("POST", "/api/notifications/read-all"),
        ("POST", "/api/notifications/1/read"),
    ]:
        response = client.request(method, path, headers=admin_headers)
        assert response.status_code == 403, path


def test_notifications_store_no_patient_identity(client, doctor_headers, finished, db):
    db.expire_all()
    dump = json.dumps(
        [
            {c.name: str(getattr(n, c.key)) for c in Notification.__table__.columns}
            for n in db.query(Notification).all()
        ]
    )
    for identifier in ("Amina", "Bibi", "MR-1001"):
        assert identifier not in dump


def test_notifications_page_through(client, doctor_headers, db, doctor):
    for _ in range(25):
        db.add(Notification(user_id=doctor.id, kind="case_completed"))
    db.commit()
    assert len(listing(client, doctor_headers)["items"]) == 20
    page_two = listing(client, doctor_headers, page=2)
    assert len(page_two["items"]) == 5
    assert page_two["items"][0]["message"] == "Scan analysis completed for a deleted patient."
