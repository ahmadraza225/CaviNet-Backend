"""M-03 Patient management: FR-03.1 to FR-03.4, doctor role only (section 9.3)."""

import json
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.core import clock
from app.models import AuditAction, Patient
from app.services import patients as patients_service
from app.services import storage
from tests.conftest import audit_entries, scan_zip, upload

PATIENT = {
    "full_name": "Amina Bibi",
    "mr_number": "MR-1001",
    "date_of_birth": "1975-04-12",
    "sex": "female",
    "phone": "+92 300 1234567",
    "notes": "Referred from OPD.",
}


def create(client, headers, **overrides):
    return client.post("/api/patients", json={**PATIENT, **overrides}, headers=headers)


def create_ok(client, headers, **overrides) -> dict:
    response = create(client, headers, **overrides)
    assert response.status_code == 201, response.json()
    return response.json()


def error_messages(response) -> dict[str, str]:
    """FastAPI validation errors as {field: message}."""
    return {
        str(item["loc"][-1]): item["msg"].removeprefix("Value error, ")
        for item in response.json()["detail"]
    }


# --- Create and view (FR-03.1, FR-03.2) ---------------------------------------------------


def test_fr03_1_create_patient_with_all_fields(client, doctor_headers):
    response = create(client, doctor_headers)
    assert response.status_code == 201
    body = response.json()
    assert {k: body[k] for k in PATIENT} == PATIENT
    assert uuid.UUID(body["id"])
    today = clock.utcnow().date()
    born = date(1975, 4, 12)
    assert body["age"] == today.year - born.year - ((today.month, today.day) < (4, 12))
    assert body["created_at"].endswith("Z") or body["created_at"].endswith("+00:00")


def test_fr03_1_optional_fields_may_be_omitted_or_blank(client, doctor_headers):
    first = create_ok(client, doctor_headers, mr_number="MR-1", phone=None, notes=None)
    second = create_ok(client, doctor_headers, mr_number="MR-2", phone="  ", notes="  ")
    for body in (first, second):
        assert body["phone"] is None
        assert body["notes"] is None


def test_fr03_1_values_are_tidied(client, doctor_headers):
    body = create_ok(
        client, doctor_headers, full_name="  Amina   Bibi ", mr_number=" mr-1001/a ", notes=" x "
    )
    assert body["full_name"] == "Amina Bibi"
    assert body["mr_number"] == "MR-1001/A"
    assert body["notes"] == "x"


def test_fr03_2_view_patient_detail_with_empty_scan_history(client, doctor_headers):
    created = create_ok(client, doctor_headers)
    response = client.get(f"/api/patients/{created['id']}", headers=doctor_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["full_name"] == "Amina Bibi"
    assert body["scans"] == []  # FR-03.4: filled from Phase 4


@pytest.mark.parametrize("method", ["GET", "PATCH", "DELETE"])
def test_unknown_patient_is_404(client, doctor_headers, method):
    response = client.request(
        method, f"/api/patients/{uuid.uuid4()}?confirm=X", json={}, headers=doctor_headers
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Patient not found.", "code": "not_found"}


def test_patients_are_shared_by_all_doctors(client, doctor_headers, make_user):
    from tests.conftest import token_for

    created = create_ok(client, doctor_headers)
    other = make_user("other.doctor@example.org")
    response = client.get(f"/api/patients/{created['id']}", headers=token_for(client, other.email))
    assert response.status_code == 200


# --- Edit (FR-03.2) -----------------------------------------------------------------------


def test_fr03_2_edit_changes_only_the_fields_sent(client, doctor_headers):
    created = create_ok(client, doctor_headers)
    response = client.patch(
        f"/api/patients/{created['id']}",
        json={"full_name": "Amina B. Khan", "phone": ""},
        headers=doctor_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["full_name"] == "Amina B. Khan"
    assert body["phone"] is None
    assert body["mr_number"] == "MR-1001"
    assert body["notes"] == "Referred from OPD."
    detail = client.get(f"/api/patients/{created['id']}", headers=doctor_headers).json()
    assert detail["full_name"] == "Amina B. Khan"


def test_fr03_2_edit_every_field(client, doctor_headers):
    created = create_ok(client, doctor_headers)
    changes = {
        "full_name": "Bilal Ahmed",
        "mr_number": "MR-2002",
        "date_of_birth": "1990-01-31",
        "sex": "male",
        "phone": "051-1234567",
        "notes": "Follow-up in 3 months.",
    }
    response = client.patch(f"/api/patients/{created['id']}", json=changes, headers=doctor_headers)
    assert response.status_code == 200
    assert {k: response.json()[k] for k in changes} == changes


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("full_name", "Full name cannot be empty."),
        ("mr_number", "Mr number cannot be empty."),
        ("date_of_birth", "Date of birth cannot be empty."),
        ("sex", "Sex cannot be empty."),
    ],
)
def test_fr03_1_edit_cannot_clear_a_required_field(client, doctor_headers, field, message):
    created = create_ok(client, doctor_headers)
    response = client.patch(
        f"/api/patients/{created['id']}", json={field: None}, headers=doctor_headers
    )
    assert response.status_code == 422
    assert message in response.json()["detail"][0]["msg"]


# --- Unique MR number (FR-03.1) -----------------------------------------------------------


def test_fr03_1_mr_number_must_be_unique(client, doctor_headers):
    create_ok(client, doctor_headers)
    response = create(client, doctor_headers, full_name="Someone Else", mr_number=" mr-1001 ")
    assert response.status_code == 409
    assert response.json() == {
        "detail": "A patient with this MR number already exists.",
        "code": "mr_number_taken",
    }


def test_fr03_1_edit_to_another_patients_mr_number_is_409(client, doctor_headers):
    create_ok(client, doctor_headers)
    other = create_ok(client, doctor_headers, mr_number="MR-2002")
    response = client.patch(
        f"/api/patients/{other['id']}", json={"mr_number": "mr-1001"}, headers=doctor_headers
    )
    assert response.status_code == 409
    assert response.json()["code"] == "mr_number_taken"


def test_edit_keeping_the_same_mr_number_is_allowed(client, doctor_headers):
    created = create_ok(client, doctor_headers)
    response = client.patch(
        f"/api/patients/{created['id']}",
        json={"mr_number": "mr-1001", "notes": "Updated."},
        headers=doctor_headers,
    )
    assert response.status_code == 200


def test_fr03_1_database_enforces_unique_mr_number_on_create(client, doctor_headers, monkeypatch):
    """Two simultaneous requests can both pass the pre-check; the unique index decides."""
    create_ok(client, doctor_headers)
    monkeypatch.setattr(patients_service, "_mr_number_in_use", lambda *a, **k: False)
    response = create(client, doctor_headers, full_name="Someone Else")
    assert response.status_code == 409
    assert response.json()["code"] == "mr_number_taken"


def test_fr03_1_database_enforces_unique_mr_number_on_edit(client, doctor_headers, monkeypatch):
    create_ok(client, doctor_headers)
    other = create_ok(client, doctor_headers, mr_number="MR-2002")
    monkeypatch.setattr(patients_service, "_mr_number_in_use", lambda *a, **k: False)
    response = client.patch(
        f"/api/patients/{other['id']}", json={"mr_number": "MR-1001"}, headers=doctor_headers
    )
    assert response.status_code == 409
    assert response.json()["code"] == "mr_number_taken"


# --- Validation errors (FR-03.1) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "field", "message"),
    [
        ({"full_name": "   "}, "full_name", "Full name is required."),
        ({"full_name": "12345"}, "full_name", "Full name must contain letters."),
        ({"full_name": "A" * 121}, "full_name", "at most 120 characters"),
        ({"mr_number": "  "}, "mr_number", "MR number is required."),
        (
            {"mr_number": "MR 10#"},
            "mr_number",
            "MR number may contain only letters, digits, '-' and '/'.",
        ),
        ({"mr_number": "M" * 33}, "mr_number", "at most 32 characters"),
        ({"date_of_birth": "not-a-date"}, "date_of_birth", "valid date"),
        ({"date_of_birth": "1899-12-31"}, "date_of_birth", "on or after 1900-01-01"),
        ({"sex": "other"}, "sex", "'male' or 'female'"),
        ({"phone": "call me"}, "phone", "Enter a valid phone number"),
        ({"notes": "x" * 2001}, "notes", "at most 2000 characters"),
    ],
)
def test_fr03_1_invalid_values_are_rejected_with_a_message(
    client, doctor_headers, overrides, field, message
):
    response = create(client, doctor_headers, **overrides)
    assert response.status_code == 422
    errors = error_messages(response)
    assert message in errors[field], errors


def test_fr03_1_date_of_birth_cannot_be_in_the_future(client, doctor_headers):
    tomorrow = (clock.utcnow().date() + timedelta(days=1)).isoformat()
    response = create(client, doctor_headers, date_of_birth=tomorrow)
    assert response.status_code == 422
    assert error_messages(response)["date_of_birth"] == "Date of birth cannot be in the future."


@pytest.mark.parametrize("field", ["full_name", "mr_number", "date_of_birth", "sex"])
def test_fr03_1_required_fields_are_required(client, doctor_headers, field):
    body = {k: v for k, v in PATIENT.items() if k != field}
    response = client.post("/api/patients", json=body, headers=doctor_headers)
    assert response.status_code == 422
    assert error_messages(response)[field] == "Field required"


def test_invalid_input_creates_nothing(client, doctor_headers, db):
    create(client, doctor_headers, sex="other")
    assert db.scalar(select(Patient.id)) is None
    assert audit_entries(db, AuditAction.PATIENT_CREATED) == []


# --- Search, sorting and pagination (FR-03.3) ---------------------------------------------


def _seed(client, headers, count: int) -> None:
    for n in range(count):
        create_ok(
            client,
            headers,
            full_name=f"Patient {chr(ord('A') + n % 26)}{n:02d}",
            mr_number=f"MR-{n:04d}",
            date_of_birth=date(1950 + n % 50, 1 + n % 12, 1).isoformat(),
        )


def _list(client, headers, **params):
    response = client.get("/api/patients", params=params, headers=headers)
    assert response.status_code == 200, response.json()
    return response.json()


def test_fr03_3_pagination_is_20_per_page(client, doctor_headers):
    _seed(client, doctor_headers, 45)
    first = _list(client, doctor_headers)
    assert (first["total"], first["page"], first["page_size"], first["pages"]) == (45, 1, 20, 3)
    assert len(first["items"]) == 20
    third = _list(client, doctor_headers, page=3)
    assert len(third["items"]) == 5
    beyond = _list(client, doctor_headers, page=4)
    assert beyond["items"] == [] and beyond["total"] == 45
    seen = [
        p["id"] for page in (1, 2, 3) for p in _list(client, doctor_headers, page=page)["items"]
    ]
    assert len(seen) == len(set(seen)) == 45


def test_fr03_3_empty_list_has_one_page(client, doctor_headers):
    body = _list(client, doctor_headers)
    assert body == {"items": [], "total": 0, "page": 1, "page_size": 20, "pages": 1}


def test_fr03_3_newest_first_by_default(client, doctor_headers):
    for n in range(3):
        create_ok(client, doctor_headers, mr_number=f"MR-{n}", full_name=f"Patient {'ABC'[n]}")
        clock.advance(timedelta(seconds=1))
    names = [p["full_name"] for p in _list(client, doctor_headers)["items"]]
    assert names == ["Patient C", "Patient B", "Patient A"]


@pytest.mark.parametrize(
    ("sort", "key"),
    [
        ("full_name", lambda p: p["full_name"].lower()),
        ("mr_number", lambda p: p["mr_number"]),
        ("date_of_birth", lambda p: p["date_of_birth"]),
    ],
)
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_fr03_3_sorting(client, doctor_headers, sort, key, order):
    create_ok(
        client, doctor_headers, full_name="zainab Ali", mr_number="B-2", date_of_birth="1990-01-01"
    )
    create_ok(
        client, doctor_headers, full_name="Ahmed Raza", mr_number="C-3", date_of_birth="1960-06-15"
    )
    create_ok(
        client, doctor_headers, full_name="Maryam Noor", mr_number="A-1", date_of_birth="2001-12-31"
    )
    items = _list(client, doctor_headers, sort=sort, order=order)["items"]
    assert items == sorted(items, key=key, reverse=order == "desc")


def test_fr03_3_sorting_by_last_updated(client, doctor_headers):
    first = create_ok(client, doctor_headers, mr_number="MR-1")
    clock.advance(timedelta(seconds=1))
    create_ok(client, doctor_headers, mr_number="MR-2")
    clock.advance(timedelta(seconds=1))
    client.patch(f"/api/patients/{first['id']}", json={"notes": "Seen."}, headers=doctor_headers)
    items = _list(client, doctor_headers, sort="updated_at", order="desc")["items"]
    assert [p["mr_number"] for p in items] == ["MR-1", "MR-2"]


@pytest.mark.parametrize(
    "params",
    [{"sort": "phone"}, {"order": "up"}, {"page": 0}, {"q": "x" * 121}],
)
def test_fr03_3_bad_list_parameters_are_422(client, doctor_headers, params):
    response = client.get("/api/patients", params=params, headers=doctor_headers)
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("amina", ["Amina Bibi"]),  # name, any case
        ("BIBI", ["Amina Bibi"]),  # part of the name
        ("mr-20", ["Bilal Ahmed"]),  # MR number, any case
        ("2002", ["Bilal Ahmed"]),  # part of the MR number
        ("  amina   bibi ", ["Amina Bibi"]),  # extra spaces ignored
        ("a", ["Amina Bibi", "Bilal Ahmed", "Sana 100% Khan"]),
        ("100%", ["Sana 100% Khan"]),  # % is matched literally, not as a wildcard
        ("_", []),  # so is _
        ("nobody", []),
    ],
)
def test_fr03_3_search_by_name_or_mr_number(client, doctor_headers, query, expected):
    create_ok(client, doctor_headers)
    create_ok(client, doctor_headers, full_name="Bilal Ahmed", mr_number="MR-2002")
    create_ok(client, doctor_headers, full_name="Sana 100% Khan", mr_number="X-1")
    body = _list(client, doctor_headers, q=query, sort="full_name", order="asc")
    assert [p["full_name"] for p in body["items"]] == expected
    assert body["total"] == len(expected)


def test_fr03_3_search_results_are_paged(client, doctor_headers):
    _seed(client, doctor_headers, 30)
    create_ok(client, doctor_headers, full_name="Other Person", mr_number="ZZ-1")
    body = _list(client, doctor_headers, q="patient", page=2)
    assert (body["total"], body["pages"], len(body["items"])) == (30, 2, 10)


# --- Delete (FR-03.2) ---------------------------------------------------------------------


@pytest.mark.parametrize("confirm", [None, "", "MR-9999"])
def test_fr03_2_delete_requires_typing_the_mr_number(client, doctor_headers, confirm, db):
    created = create_ok(client, doctor_headers)
    params = {} if confirm is None else {"confirm": confirm}
    response = client.delete(
        f"/api/patients/{created['id']}", params=params, headers=doctor_headers
    )
    assert response.status_code == 422
    assert response.json() == {
        "detail": "Type the patient's MR number to confirm the deletion.",
        "code": "confirmation_required",
    }
    assert client.get(f"/api/patients/{created['id']}", headers=doctor_headers).status_code == 200
    assert audit_entries(db, AuditAction.PATIENT_DELETED) == []


def test_fr03_2_delete_is_permanent_and_removes_the_patients_files(client, doctor_headers):
    created = create_ok(client, doctor_headers)
    other = create_ok(client, doctor_headers, mr_number="MR-2")
    mine = upload(client, doctor_headers, created["id"], [("s.zip", scan_zip(50))]).json()
    theirs = upload(client, doctor_headers, other["id"], [("s.zip", scan_zip(50))]).json()
    folder = storage.case_dir(uuid.UUID(mine["id"]))
    other_folder = storage.case_dir(uuid.UUID(theirs["id"]))
    assert any(folder.rglob("*.dcm")) and any(other_folder.rglob("*.dcm"))

    response = client.delete(
        f"/api/patients/{created['id']}", params={"confirm": " mr-1001 "}, headers=doctor_headers
    )
    assert response.status_code == 204
    assert client.get(f"/api/patients/{created['id']}", headers=doctor_headers).status_code == 404
    assert not folder.exists()
    assert other_folder.exists()  # nobody else's files are touched
    assert _list(client, doctor_headers)["total"] == 1


def test_fr03_2_delete_works_when_the_patient_has_no_files(client, doctor_headers):
    created = create_ok(client, doctor_headers)
    response = client.delete(
        f"/api/patients/{created['id']}", params={"confirm": "MR-1001"}, headers=doctor_headers
    )
    assert response.status_code == 204


def test_fr03_2_file_cleanup_failure_does_not_undo_the_delete(
    client, doctor_headers, monkeypatch, caplog
):
    created = create_ok(client, doctor_headers)
    upload(client, doctor_headers, created["id"], [("s.zip", scan_zip(50))])
    monkeypatch.setattr(storage, "remove_paths", lambda paths: list(paths))
    response = client.delete(
        f"/api/patients/{created['id']}", params={"confirm": "MR-1001"}, headers=doctor_headers
    )
    assert response.status_code == 204
    assert client.get(f"/api/patients/{created['id']}", headers=doctor_headers).status_code == 404
    assert "files could not all be removed: 1 left" in caplog.text


def test_storage_refuses_to_delete_outside_the_data_directory(tmp_path, caplog):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    root = storage.data_root()
    root.mkdir(parents=True)
    escape = root / "patients" / ".." / ".." / "outside.txt"
    assert storage.remove_paths([outside, escape, root]) == [outside, outside, root]
    assert outside.exists() and root.exists()
    assert "not inside DATA_DIR" in caplog.text


def test_storage_reports_folders_it_could_not_delete(monkeypatch, caplog):
    folder = storage.data_root() / "patients" / "p1"
    folder.mkdir(parents=True)

    def fail(path):
        raise PermissionError(path)

    monkeypatch.setattr(storage.shutil, "rmtree", fail)
    assert storage.remove_paths([folder]) == [folder]
    assert "Could not delete" in caplog.text


def test_storage_removes_files_and_folders_inside_the_data_directory():
    root = storage.data_root()
    (root / "a" / "b").mkdir(parents=True)
    (root / "file.txt").write_text("x")
    assert storage.remove_paths([root / "a", root / "file.txt", root / "missing"]) == []
    assert list(root.iterdir()) == []


# --- Audit (FR-09.2, NFR-3) ---------------------------------------------------------------


def test_create_edit_and_delete_are_audited_without_patient_identity(
    client, doctor, doctor_headers, db
):
    created = create_ok(client, doctor_headers)
    patient_url = f"/api/patients/{created['id']}"
    client.patch(
        patient_url, json={"full_name": "Amina Khan", "notes": "New note."}, headers=doctor_headers
    )
    client.patch(patient_url, json={"notes": "New note."}, headers=doctor_headers)  # no change
    client.delete(patient_url, params={"confirm": "MR-1001"}, headers=doctor_headers)

    entries = [e for e in audit_entries(db) if e.target_type == "patient"]
    assert [e.action for e in entries] == [
        AuditAction.PATIENT_CREATED,
        AuditAction.PATIENT_UPDATED,
        AuditAction.PATIENT_DELETED,
    ]
    for entry in entries:
        assert entry.target_id == created["id"]
        assert entry.user_id == doctor.id
        assert entry.actor_email == doctor.email
    assert entries[1].details == {"fields": ["full_name", "notes"]}

    dump = json.dumps([[e.actor_email, e.target_id, e.details] for e in audit_entries(db)])
    for identifier in ("Amina", "Bibi", "Khan", "MR-1001", "1975-04-12", "300 1234567", "OPD"):
        assert identifier not in dump


def test_patient_identity_is_not_written_to_the_application_log(client, doctor_headers, caplog):
    caplog.set_level("DEBUG")
    created = create_ok(client, doctor_headers)
    client.patch(
        f"/api/patients/{created['id']}", json={"full_name": "Amina Khan"}, headers=doctor_headers
    )
    client.get("/api/patients", params={"q": "amina"}, headers=doctor_headers)
    client.delete(
        f"/api/patients/{created['id']}", params={"confirm": "MR-1001"}, headers=doctor_headers
    )
    # The test client's own request log (httpx2) is not part of the application.
    app_log = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx"))
    for identifier in ("Amina", "Bibi", "MR-1001"):
        assert identifier not in app_log


def test_access_log_lines_drop_the_query_string():
    """uvicorn's access log would otherwise record searched names and MR numbers."""
    import logging

    from app.core.logging import ACCESS_LOGGER, StripQueryString

    record = logging.LogRecord(
        ACCESS_LOGGER,
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:5000", "GET", "/api/patients?q=Amina+Bibi&page=2", "1.1", 200),
        None,
    )
    assert StripQueryString().filter(record)
    assert record.getMessage() == '10.0.0.1:5000 - "GET /api/patients HTTP/1.1" 200'
    assert any(isinstance(f, StripQueryString) for f in logging.getLogger(ACCESS_LOGGER).filters)


# --- Doctor-only access (section 9.3) -----------------------------------------------------


def test_admin_gets_403_from_every_patient_endpoint(client, doctor_headers, admin_headers, db):
    """Admins deliberately have no access to patient data, even to read it."""
    created = create_ok(client, doctor_headers)
    url = f"/api/patients/{created['id']}"
    calls = [
        ("GET", "/api/patients", None),
        ("GET", "/api/patients?q=amina", None),
        ("POST", "/api/patients", {**PATIENT, "mr_number": "MR-ADMIN"}),
        ("GET", url, None),
        ("PATCH", url, {"full_name": "Changed By Admin"}),
        ("DELETE", f"{url}?confirm=MR-1001", None),
    ]
    for method, path, body in calls:
        response = client.request(method, path, json=body, headers=admin_headers)
        assert response.status_code == 403, (method, path)
        assert response.json() == {
            "detail": "You do not have permission to do this.",
            "code": "forbidden",
        }
    detail = client.get(url, headers=doctor_headers).json()
    assert detail["full_name"] == "Amina Bibi"
    assert _list(client, doctor_headers)["total"] == 1
    patient_actions = {e.action for e in audit_entries(db) if e.target_type == "patient"}
    assert patient_actions == {AuditAction.PATIENT_CREATED}
