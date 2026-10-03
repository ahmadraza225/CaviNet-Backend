"""M-04 upload endpoint: POST /api/patients/{id}/cases (FR-04.1 to FR-04.6, NFR-3)."""

import json
import uuid

import pydicom
import pytest

from app.models import AuditAction, Case, CaseStatus, Notification
from app.services import storage
from app.synthetic_dicom import IDENTIFYING_VALUES, SyntheticStudy, zip_files
from tests.conftest import audit_entries, create_patient, scan_zip, set_env, upload


@pytest.fixture
def patient(client, doctor_headers) -> dict:
    return create_patient(client, doctor_headers)


def upload_ok(client, headers, patient_id, files) -> dict:
    response = upload(client, headers, patient_id, files)
    assert response.status_code == 201, response.json()
    return response.json()


def staged_leftovers() -> list:
    root = storage.staging_root()
    return list(root.iterdir()) if root.exists() else []


# --- Accepted uploads ---------------------------------------------------------------------


def test_fr04_1_zip_upload_is_validated_deidentified_stored_and_queued(
    client, doctor, doctor_headers, patient, analysis_queue
):
    body = upload_ok(client, doctor_headers, patient["id"], [("scan.zip", scan_zip(60))])

    assert body["status"] == "queued"
    assert body["patient"] == {
        "id": patient["id"],
        "full_name": "Amina Bibi",
        "mr_number": "MR-1001",
    }
    assert body["uploaded_by"] == "Dan Doctor"
    assert body["upload"]["kind"] == "zip" and body["upload"]["files"] == 1
    assert [step["status"] for step in body["timeline"]] == ["uploaded", "validating", "queued"]
    assert body["timeline"][2]["message"] == "One image series found (60 slices)."
    assert body["failure_reason"] is None and body["result"] is None

    stored = list(storage.case_dicom_dir(uuid.UUID(body["id"])).glob("*.dcm"))
    assert len(stored) == 60
    # FR-04.6: the analysis job was queued with the case id.
    jobs = analysis_queue.get_jobs()
    assert [(job.func_name, job.args) for job in jobs] == [
        ("app.workers.jobs.analyse_case", (body["id"],))
    ]
    assert staged_leftovers() == []


def test_fr04_1_several_dcm_files_are_accepted(client, doctor_headers, patient):
    files = SyntheticStudy("dcm").series(52)
    body = upload_ok(
        client, doctor_headers, patient["id"], [(f"IM{i}.dcm", d) for i, d in enumerate(files)]
    )
    assert body["status"] == "queued"
    assert body["upload"] == {"kind": "dcm", "files": 52, "bytes": sum(len(d) for d in files)}
    assert body["scan"]["num_slices"] == 52


def test_fr04_1_dicom_files_without_an_extension_are_accepted(client, doctor_headers, patient):
    files = SyntheticStudy("noext").series(50)
    body = upload_ok(
        client, doctor_headers, patient["id"], [(f"IM{i:04d}", d) for i, d in enumerate(files)]
    )
    assert body["status"] == "queued"


def test_fr04_5_scan_details_are_returned_and_stored(client, doctor_headers, patient, db):
    body = upload_ok(client, doctor_headers, patient["id"], [("scan.zip", scan_zip(64))])
    assert body["scan"] == {
        "study_date": "2026-09-15",
        "num_slices": 64,
        "slice_thickness_mm": 1.25,
        "slice_spacing_mm": 1.25,
        "pixel_spacing_mm": [0.7, 0.7],
        "rows": 64,
        "columns": 64,
        "manufacturer": "SYNTHETIC",
        "model": "CaviNet Synthetic CT",
        "kernel": "STANDARD",
        "series_found": 1,
        "series_number": 2,
    }
    db.expire_all()
    case = db.get(Case, uuid.UUID(body["id"]))
    assert (case.num_slices, case.manufacturer, case.convolution_kernel) == (
        64,
        "SYNTHETIC",
        "STANDARD",
    )


def test_fr04_3_multi_series_upload_uses_the_largest_and_records_the_choice(
    client, doctor_headers, patient
):
    study = SyntheticStudy("multi")
    files = [*study.series(55, series_number=3), *study.series(90, series_number=5)]
    body = upload_ok(client, doctor_headers, patient["id"], [("scan.zip", zip_files(files))])
    assert body["scan"]["num_slices"] == 90
    assert (body["scan"]["series_found"], body["scan"]["series_number"]) == (2, 5)
    assert body["timeline"][-1]["message"] == (
        "2 image series found; used series 5 with the most slices (90; others: 55)."
    )


def test_fr04_4_only_deidentified_files_are_kept(client, doctor_headers, patient):
    body = upload_ok(client, doctor_headers, patient["id"], [("scan.zip", scan_zip(50))])
    case_folder = storage.case_dir(uuid.UUID(body["id"]))
    everything = [p for p in storage.data_root().rglob("*") if p.is_file()]
    assert everything and all(case_folder in p.parents for p in everything)
    for path in everything:
        raw = path.read_bytes()
        for value in IDENTIFYING_VALUES.values():
            assert value.encode() not in raw
        assert pydicom.dcmread(path).PatientIdentityRemoved == "YES"


# --- Rejected scans become Failed cases with the reason -----------------------------------


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (scan_zip(30), "The scan has 30 slices; at least 50 are needed."),
        (scan_zip(60, modality="MR"), "The selected series is an MRI scan, not a CT scan."),
        (scan_zip(60, spacing_mm=7.5), "The slices are 7.5 mm apart; the maximum is 5 mm."),
        (b"not a zip", "The .zip file is damaged or is not a zip file."),
    ],
)
def test_fr04_2_invalid_scan_fails_with_the_reason_and_stores_nothing(
    client, doctor, doctor_headers, patient, db, analysis_queue, data, reason
):
    body = upload_ok(client, doctor_headers, patient["id"], [("scan.zip", data)])
    assert body["status"] == "failed"
    assert body["failure_reason"].startswith(reason)
    assert [step["status"] for step in body["timeline"]] == ["uploaded", "validating", "failed"]
    assert body["timeline"][-1]["message"] == body["failure_reason"]
    assert body["scan"] is None
    assert not storage.case_dir(uuid.UUID(body["id"])).exists()
    assert staged_leftovers() == []
    assert analysis_queue.get_jobs() == []
    # FR-08.2: the uploading doctor is told it failed.
    db.expire_all()
    notes = db.query(Notification).all()
    assert [(n.user_id, n.kind, str(n.case_id)) for n in notes] == [
        (doctor.id, "case_failed", body["id"])
    ]


def test_fr04_2_non_dicom_files_in_a_dcm_upload_fail_the_case(client, doctor_headers, patient):
    files = [(f"{i}.dcm", d) for i, d in enumerate(SyntheticStudy("x").series(50))]
    files.append(("notes.dcm", b"this is not DICOM"))
    body = upload_ok(client, doctor_headers, patient["id"], files)
    assert body["status"] == "failed"
    assert body["failure_reason"].startswith("1 of the 51 uploaded file is not DICOM")


def test_unexpected_processing_errors_fail_the_case_without_leaking_details(
    client, doctor_headers, patient, monkeypatch, caplog
):
    def explode(upload, dicom_dir):
        dicom_dir.mkdir(parents=True)
        (dicom_dir / "partial.dcm").write_bytes(b"x")
        raise RuntimeError("SYNTHETIC^TESTPATIENT in an error message")

    monkeypatch.setattr("app.services.cases.intake", explode)
    body = upload_ok(client, doctor_headers, patient["id"], [("scan.zip", scan_zip(50))])
    assert body["status"] == "failed"
    assert body["failure_reason"] == (
        "The scan could not be processed because of an unexpected error."
    )
    assert not storage.case_dir(uuid.UUID(body["id"])).exists()
    assert "RuntimeError" in caplog.text
    assert "SYNTHETIC^TESTPATIENT" not in caplog.text


def test_fr04_6_queue_unavailable_fails_the_case_clearly(
    client, doctor_headers, patient, monkeypatch
):
    class DownQueue:
        def enqueue(self, *args, **kwargs):
            raise ConnectionError("redis is down")

    monkeypatch.setattr("app.services.cases.get_analysis_queue", lambda: DownQueue())
    body = upload_ok(client, doctor_headers, patient["id"], [("scan.zip", scan_zip(50))])
    assert body["status"] == "failed"
    assert body["failure_reason"].startswith("The analysis queue is not available")
    assert [s["status"] for s in body["timeline"]] == ["uploaded", "validating", "queued", "failed"]


# --- Request-level problems: no case is created --------------------------------------------


@pytest.mark.parametrize(
    ("files", "status", "code", "detail"),
    [
        ([], 422, "no_files", "Choose a .zip file or the scan's .dcm files to upload."),
        (
            [("a.zip", b"PK"), ("b.dcm", b"x")],
            422,
            "mixed_upload",
            "Upload either one .zip file or the scan's .dcm files, not both.",
        ),
        (
            [("a.zip", b"PK"), ("b.ZIP", b"PK")],
            422,
            "several_zips",
            "Upload one .zip file at a time.",
        ),
    ],
)
def test_fr04_1_upload_shape_is_checked(
    client, doctor_headers, patient, db, files, status, code, detail
):
    if files:
        response = upload(client, doctor_headers, patient["id"], files)
    else:  # a form with no file parts at all
        response = client.post(
            f"/api/patients/{patient['id']}/cases",
            files={"note": (None, "no files here")},
            headers=doctor_headers,
        )
    assert response.status_code == status
    assert response.json() == {"detail": detail, "code": code}
    assert db.query(Case).count() == 0
    assert staged_leftovers() == []


def test_fr04_1_uploads_over_the_size_limit_are_refused(
    client, doctor_headers, patient, monkeypatch, db
):
    set_env(monkeypatch, MAX_UPLOAD_BYTES=str(10 * 1024))
    response = upload(client, doctor_headers, patient["id"], [("scan.zip", scan_zip(60))])
    assert response.status_code == 413
    assert response.json() == {
        "detail": "The upload is larger than the 10 KB limit. Upload only the chest CT "
        "series, or compress it as a .zip.",
        "code": "upload_too_large",
    }
    assert db.query(Case).count() == 0
    assert staged_leftovers() == []


def test_fr04_1_the_limit_is_1_5_gb_and_checked_before_reading(client, doctor_headers, patient):
    response = client.post(
        f"/api/patients/{patient['id']}/cases",
        content=b"--x--\r\n",
        headers={
            **doctor_headers,
            "Content-Type": "multipart/form-data; boundary=x",
            "Content-Length": str(1600 * 1024**2),
        },
    )
    assert response.status_code == 413
    assert response.json()["detail"].startswith("The upload is larger than the 1.5 GB limit.")


def test_upload_must_be_multipart(client, doctor_headers, patient):
    response = client.post(
        f"/api/patients/{patient['id']}/cases",
        content=scan_zip(50),
        headers={**doctor_headers, "Content-Type": "application/zip"},
    )
    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"


def test_malformed_multipart_body_is_refused(client, doctor_headers, patient, db):
    response = client.post(
        f"/api/patients/{patient['id']}/cases",
        content=b"garbage-without-the-boundary",
        headers={**doctor_headers, "Content-Type": "multipart/form-data; boundary=x"},
    )
    assert response.status_code == 422
    assert response.json() == {
        "detail": "The upload was incomplete or damaged. Please try again.",
        "code": "bad_upload",
    }
    assert db.query(Case).count() == 0
    assert staged_leftovers() == []


def test_upload_cut_off_midway_fails_as_a_damaged_zip(client, doctor_headers, patient):
    response = client.post(
        f"/api/patients/{patient['id']}/cases",
        content=b'--x\r\nContent-Disposition: form-data; name="files"; filename="a.zip"\r\n'
        b"\r\nPK\x03\x04 and then the connection drops",
        headers={**doctor_headers, "Content-Type": "multipart/form-data; boundary=x"},
    )
    assert response.status_code == 201
    assert response.json()["failure_reason"] == "The .zip file is damaged or is not a zip file."
    assert staged_leftovers() == []


def test_upload_for_an_unknown_patient_is_404(client, doctor_headers):
    response = upload(client, doctor_headers, str(uuid.uuid4()), [("scan.zip", scan_zip(50))])
    assert response.status_code == 404
    assert response.json()["detail"] == "Patient not found."


def test_upload_is_doctor_only(client, admin_headers, doctor_headers, patient, db):
    response = upload(client, admin_headers, patient["id"], [("scan.zip", scan_zip(50))])
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"
    assert db.query(Case).count() == 0


# --- Audit (FR-09.2) and privacy (NFR-3) --------------------------------------------------


def test_upload_is_audited_without_patient_identity(client, doctor, doctor_headers, patient, db):
    data = scan_zip(50)
    body = upload_ok(client, doctor_headers, patient["id"], [("Amina_Bibi_CT.zip", data)])
    entries = audit_entries(db, AuditAction.SCAN_UPLOADED)
    assert len(entries) == 1
    entry = entries[0]
    assert (entry.user_id, entry.target_type, entry.target_id) == (doctor.id, "case", body["id"])
    assert entry.details == {"kind": "zip", "files": 1, "size_mb": round(len(data) / 1024**2, 1)}
    dump = json.dumps([[e.target_id, e.details] for e in audit_entries(db)])
    for identifier in ("Amina", "Bibi", "MR-1001"):
        assert identifier not in dump


def test_case_rows_hold_no_patient_identity(client, doctor_headers, patient, db):
    """NFR-3: names and MR numbers stay in the patients table, even in failure reasons."""
    upload_ok(client, doctor_headers, patient["id"], [("Amina_Bibi.zip", scan_zip(50))])
    upload_ok(client, doctor_headers, patient["id"], [("Amina_Bibi.zip", b"not a zip")])
    db.expire_all()
    rows = [
        {column.name: str(getattr(case, column.key)) for column in Case.__table__.columns}
        for case in db.query(Case).all()
    ]
    dump = json.dumps(rows) + json.dumps(
        [[e.message] for case in db.query(Case).all() for e in case.events]
    )
    for identifier in ("Amina", "Bibi", "MR-1001", "SYNTHETIC^TESTPATIENT"):
        assert identifier not in dump


def test_case_status_is_one_of_the_documented_statuses(client, doctor_headers, patient, db):
    upload_ok(client, doctor_headers, patient["id"], [("scan.zip", scan_zip(50))])
    db.expire_all()
    assert {case.status for case in db.query(Case).all()} <= {s.value for s in CaseStatus}
