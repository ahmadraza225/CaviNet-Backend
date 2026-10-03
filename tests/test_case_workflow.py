"""M-08 case workflow: FR-08.1 statuses and timeline, the Phase 4 stub analyser, the worker
job (FR-04.6), case pages, dashboard counts and deletion."""

import uuid
from datetime import timedelta

import pytest

from app.core import clock
from app.models import Case, CaseEvent, CaseStatus, Notification
from app.services import cases, storage
from app.services.cases import InvalidTransition, set_status
from app.workers import stub_analyser
from app.workers.jobs import analyse_case
from tests.conftest import create_patient, run_queued_jobs, scan_zip, token_for, upload

ORDER = ["uploaded", "validating", "queued", "preprocessing", "analysing", "completed"]


@pytest.fixture
def patient(client, doctor_headers) -> dict:
    return create_patient(client, doctor_headers)


def queued_case(client, headers, patient_id, slices: int = 50) -> dict:
    response = upload(client, headers, patient_id, [("scan.zip", scan_zip(slices))])
    assert response.status_code == 201 and response.json()["status"] == "queued"
    return response.json()


def get_case(client, headers, case_id: str) -> dict:
    response = client.get(f"/api/cases/{case_id}", headers=headers)
    assert response.status_code == 200, response.json()
    return response.json()


# --- FR-08.1 transitions -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "allowed"),
    [
        ("uploaded", {"validating", "failed"}),
        ("validating", {"queued", "failed"}),
        ("queued", {"preprocessing", "failed"}),
        ("preprocessing", {"analysing", "failed"}),
        ("analysing", {"completed", "failed"}),
        ("completed", set()),
        ("failed", set()),
    ],
)
def test_fr08_1_only_the_documented_transitions_are_allowed(db, doctor, current, allowed):
    for target in CaseStatus:
        case = Case(
            id=uuid.uuid4(),
            patient_id=uuid.uuid4(),
            status=current,
            upload_kind="zip",
            upload_files=1,
            upload_bytes=1,
        )
        if target.value in allowed:
            set_status(db, case, target, "note")
            assert case.status == target.value
        else:
            with pytest.raises(InvalidTransition):
                set_status(db, case, target)
            assert case.status == current
        db.expunge_all()


# --- The stub analyser walks the statuses ---------------------------------------------------


def test_fr08_1_worker_takes_the_case_to_completed_with_a_timestamped_timeline(
    client, doctor_headers, patient, analysis_queue
):
    case = queued_case(client, doctor_headers, patient["id"])
    run_queued_jobs(analysis_queue)

    body = get_case(client, doctor_headers, case["id"])
    assert body["status"] == "completed"
    assert [step["status"] for step in body["timeline"]] == ORDER
    times = [step["at"] for step in body["timeline"]]
    assert times == sorted(times) and all(t.endswith("Z") or "+00:00" in t for t in times)
    assert body["completed_at"] is not None


def test_stub_result_is_clearly_flagged(client, doctor_headers, patient, analysis_queue):
    case = queued_case(client, doctor_headers, patient["id"])
    run_queued_jobs(analysis_queue)
    body = get_case(client, doctor_headers, case["id"])
    assert body["result"] == {
        "label": "STUB",
        "is_stub": True,
        "analyser": "stub-analyser (Phase 4 placeholder)",
        "note": "Placeholder from the Phase 4 stub analyser. No AI analysis was performed.",
    }
    assert body["timeline"][-1]["message"] == "STUB result: placeholder, not a diagnosis."


def test_stub_pauses_between_steps_so_progress_can_be_seen(db, client, doctor_headers, patient):
    case = queued_case(client, doctor_headers, patient["id"])
    pauses: list[float] = []
    stub_analyser.run(db, uuid.UUID(case["id"]), sleep=pauses.append)
    assert pauses == [0.0, 0.0]  # STUB_STEP_SECONDS (0 in tests, 2 by default)


def test_fr08_2_completion_notifies_the_uploading_doctor_only(
    client, doctor, doctor_headers, make_user, patient, analysis_queue, db
):
    other = make_user("other@example.org")
    case = queued_case(client, doctor_headers, patient["id"])
    run_queued_jobs(analysis_queue)
    db.expire_all()
    notes = db.query(Notification).all()
    assert [(n.user_id, n.kind, str(n.case_id), n.read_at) for n in notes] == [
        (doctor.id, "case_completed", case["id"], None)
    ]
    other_headers = token_for(client, other.email)
    count = client.get("/api/notifications/unread-count", headers=other_headers).json()
    assert count == {"count": 0}


def test_fr08_2_analysis_failure_fails_the_case_and_notifies(
    client, doctor, doctor_headers, patient, analysis_queue, db
):
    case = queued_case(client, doctor_headers, patient["id"])
    for path in storage.case_dicom_dir(uuid.UUID(case["id"])).glob("0000*.dcm"):
        path.unlink()  # the stored scan went missing
    run_queued_jobs(analysis_queue)

    body = get_case(client, doctor_headers, case["id"])
    assert body["status"] == "failed"
    assert body["failure_reason"] == stub_analyser.MISSING_FILES
    assert [s["status"] for s in body["timeline"]] == ORDER[:4] + ["failed"]
    assert body["result"] is None
    db.expire_all()
    assert [(n.user_id, n.kind) for n in db.query(Notification).all()] == [
        (doctor.id, "case_failed")
    ]


def test_unexpected_worker_errors_fail_the_case(client, doctor_headers, patient, monkeypatch, db):
    case = queued_case(client, doctor_headers, patient["id"])

    def boom(_):
        raise RuntimeError("disk on fire")

    stub_analyser.run(db, uuid.UUID(case["id"]), sleep=boom)
    body = get_case(client, doctor_headers, case["id"])
    assert (body["status"], body["failure_reason"]) == ("failed", stub_analyser.UNEXPECTED)


def test_the_job_skips_cases_that_are_not_queued_or_were_deleted(
    client, doctor_headers, patient, analysis_queue
):
    case = queued_case(client, doctor_headers, patient["id"])
    run_queued_jobs(analysis_queue)
    assert analyse_case(case["id"]) == "skipped (completed)"
    assert analyse_case(str(uuid.uuid4())) == "missing"


def test_a_patient_deleted_during_analysis_does_not_break_the_worker(
    client, doctor_headers, patient, analysis_queue
):
    queued_case(client, doctor_headers, patient["id"])
    client.delete(f"/api/patients/{patient['id']}?confirm=MR-1001", headers=doctor_headers)
    run_queued_jobs(analysis_queue)  # finds no case and finishes quietly
    assert analysis_queue.failed_job_registry.count == 0


# --- Start-up recovery (NFR-4) -------------------------------------------------------------


def test_uploads_interrupted_by_a_restart_are_failed_and_staging_is_cleared(
    db, client, doctor, doctor_headers, patient
):
    case = Case(
        patient_id=uuid.UUID(patient["id"]),
        uploaded_by_id=doctor.id,
        status=CaseStatus.VALIDATING.value,
        upload_kind="zip",
        upload_files=1,
        upload_bytes=10,
    )
    db.add(case)
    db.commit()
    leftover = storage.new_staging_dir()
    (leftover / "00001.zip").write_bytes(b"identifiable original")

    assert cases.recover_interrupted_uploads(db) == 1
    db.refresh(case)
    assert case.status == "failed"
    assert case.failure_reason == cases.INTERRUPTED
    assert list(storage.staging_root().iterdir()) == []


# --- Case page -------------------------------------------------------------------------------


def test_case_page_is_shared_between_doctors(client, doctor_headers, make_user, patient):
    case = queued_case(client, doctor_headers, patient["id"])
    colleague = make_user("colleague@example.org", full_name="Dr Colleague")
    body = get_case(client, token_for(client, colleague.email), case["id"])
    assert body["uploaded_by"] == "Dan Doctor"


def test_unknown_case_is_404(client, doctor_headers):
    response = client.get(f"/api/cases/{uuid.uuid4()}", headers=doctor_headers)
    assert response.status_code == 404
    assert response.json() == {"detail": "Case not found.", "code": "not_found"}


def test_case_page_is_doctor_only(client, doctor_headers, admin_headers, patient):
    case = queued_case(client, doctor_headers, patient["id"])
    response = client.get(f"/api/cases/{case['id']}", headers=admin_headers)
    assert response.status_code == 403


# --- Patient scan history (FR-03.4) and dashboard (FR-02.1, FR-02.2) ------------------------


def test_fr03_4_patient_page_lists_scans_with_date_status_and_result(
    client, doctor_headers, patient, analysis_queue
):
    first = queued_case(client, doctor_headers, patient["id"])
    run_queued_jobs(analysis_queue)
    clock.advance(timedelta(seconds=5))
    failed = upload(client, doctor_headers, patient["id"], [("scan.zip", scan_zip(10))]).json()

    scans = client.get(f"/api/patients/{patient['id']}", headers=doctor_headers).json()["scans"]
    assert [(s["id"], s["status"], s["result"]) for s in scans] == [
        (failed["id"], "failed", None),
        (first["id"], "completed", "STUB"),
    ]
    assert all(s["uploaded_at"] for s in scans)


def test_fr02_1_dashboard_counts_follow_the_cases(
    client, doctor, doctor_headers, patient, analysis_queue
):
    def stats():
        # A fresh token each time, because the clock jumps ahead below.
        return client.get("/api/dashboard/stats", headers=token_for(client, doctor.email)).json()

    queued_case(client, doctor_headers, patient["id"])
    assert stats() == {
        "total_patients": 1,
        "scans_last_7_days": 1,
        "cases_in_progress": 1,
        "completed_cases": 0,
        "failed_cases": 0,
    }
    run_queued_jobs(analysis_queue)
    upload(client, doctor_headers, patient["id"], [("scan.zip", scan_zip(10))])
    assert stats() == {
        "total_patients": 1,
        "scans_last_7_days": 2,
        "cases_in_progress": 0,
        "completed_cases": 1,
        "failed_cases": 1,
    }
    clock.advance(timedelta(days=8))
    assert stats()["scans_last_7_days"] == 0
    assert stats()["completed_cases"] == 1


def test_fr02_2_recent_cases_newest_first_at_most_10(
    client, doctor_headers, patient, analysis_queue
):
    other = create_patient(client, doctor_headers, full_name="Bilal Ahmed", mr_number="MR-2")
    ids = []
    for n in range(11):
        owner = patient if n % 2 == 0 else other
        ids.append(upload(client, doctor_headers, owner["id"], [("s.zip", scan_zip(10))]).json())
        clock.advance(timedelta(seconds=1))
    recent = client.get("/api/dashboard/recent-cases", headers=doctor_headers).json()
    assert len(recent) == 10
    assert [r["case_id"] for r in recent] == [c["id"] for c in reversed(ids)][:10]
    assert recent[0] == {
        "case_id": ids[-1]["id"],
        "patient_id": patient["id"],
        "patient_name": "Amina Bibi",
        "uploaded_at": recent[0]["uploaded_at"],
        "status": "failed",
        "result": None,
    }


# --- Deleting a patient removes their cases and files (FR-03.2, NFR-3) ----------------------


def test_fr03_2_deleting_a_patient_removes_cases_timelines_notifications_and_files(
    client, doctor_headers, patient, analysis_queue, db
):
    other = create_patient(client, doctor_headers, full_name="Bilal Ahmed", mr_number="MR-2")
    mine = queued_case(client, doctor_headers, patient["id"])
    theirs = queued_case(client, doctor_headers, other["id"])
    run_queued_jobs(analysis_queue)
    my_folder = storage.case_dir(uuid.UUID(mine["id"]))
    their_folder = storage.case_dir(uuid.UUID(theirs["id"]))
    assert my_folder.exists() and their_folder.exists()

    response = client.delete(
        f"/api/patients/{patient['id']}?confirm=MR-1001", headers=doctor_headers
    )
    assert response.status_code == 204
    assert not my_folder.exists()
    assert their_folder.exists()
    db.expire_all()
    assert [str(c.id) for c in db.query(Case).all()] == [theirs["id"]]
    assert {str(e.case_id) for e in db.query(CaseEvent).all()} == {theirs["id"]}
    assert {str(n.case_id) for n in db.query(Notification).all()} == {theirs["id"]}
    assert client.get(f"/api/cases/{mine['id']}", headers=doctor_headers).status_code == 404
