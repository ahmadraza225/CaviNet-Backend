"""M-06 results (FR-06.1 to FR-06.4), FR-05.3/05.4/05.6 on stored results, the model
status and the admin model page (FR-09.4)."""

import io
import json
import uuid
import zipfile

import pydicom
import pytest
import SimpleITK as sitk
from PIL import Image

from app.core.config import get_settings
from app.models import AuditAction, Case, CaseStatus
from app.services import model_store, storage
from app.synthetic_dicom import SyntheticStudy, zip_files
from tests.conftest import audit_entries, create_patient, run_queued_jobs, scan_zip, upload

DISCLAIMER = "Decision support only. Not a diagnosis. Confirm with laboratory tests."
BANNER = "DEMO MODEL: NOT FOR CLINICAL USE"


@pytest.fixture
def patient(client, doctor_headers) -> dict:
    return create_patient(client, doctor_headers)


def completed_case(client, headers, patient_id, queue, data: bytes | None = None) -> dict:
    response = upload(client, headers, patient_id, [("scan.zip", data or scan_zip(60))])
    assert response.status_code == 201 and response.json()["status"] == "queued"
    run_queued_jobs(queue)
    body = client.get(f"/api/cases/{response.json()['id']}", headers=headers).json()
    assert body["status"] == "completed", body
    return body


def result_of(client, headers, case_id):
    response = client.get(f"/api/cases/{case_id}/result", headers=headers)
    assert response.status_code == 200, response.json()
    return response.json()


# --- FR-06.1 to FR-06.3 ----------------------------------------------------------------------


def test_fr06_result_has_class_probability_confidence_band_and_explanation(
    client, doctor_headers, patient, analysis_queue
):
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    result = result_of(client, doctor_headers, case["id"])

    assert result["predicted_class"] in {"TB", "NTM"}
    p = result["probability_tb"]
    assert 0 <= p <= 1 and result["probability_tb_pct"] == round(p * 100, 1)
    # Section 11.5: TB if p >= 0.5; confidence p or 1 - p; bands at 80 and 65.
    assert result["predicted_class"] == ("TB" if p >= 0.5 else "NTM")
    expected_confidence = round((p if p >= 0.5 else 1 - p) * 100, 1)
    assert result["confidence_pct"] == expected_confidence
    pct = result["confidence_pct"]
    assert result["band"] == ("High" if pct >= 80 else "Moderate" if pct >= 65 else "Low")
    assert result["inconclusive"] == (result["band"] == "Low")
    if result["band"] == "Low":
        assert result["explanation"].startswith("Inconclusive: the model is not confident")
    else:
        assert result["explanation"] == (
            f"The scan pattern is more consistent with {result['predicted_class']} "
            f"(confidence: {result['band']}, {pct:.1f}%). Confirm with laboratory testing."
        )
    assert result["disclaimer"] == DISCLAIMER
    assert result["patient"]["full_name"] == "Amina Bibi"


def test_fr06_3_validated_performance_comes_from_the_model_card(
    client, doctor_headers, patient, analysis_queue, installed_model
):
    from cavinet_ml.model.bundle import load_bundle

    card = load_bundle(installed_model)["metrics"]
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    performance = result_of(client, doctor_headers, case["id"])["validated_performance"]
    assert performance == {
        "auc": card["locked_test"]["auc"],
        "sensitivity": card["locked_test"]["sensitivity"],
        "specificity": card["locked_test"]["specificity"],
        "cases": card["locked_test"]["n"],
        "dataset": card["dataset"],
    }


def test_fr05_6_demo_results_carry_the_demo_banner_everywhere(
    client, doctor_headers, patient, analysis_queue
):
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    result = result_of(client, doctor_headers, case["id"])
    assert result["is_demo"] is True and result["demo_banner"] == BANNER
    assert result["model"]["is_demo"] is True
    assert case["result"]["is_demo"] is True and case["result"]["is_stub"] is False
    assert case["timeline"][4]["message"].endswith("DEMO MODEL: NOT FOR CLINICAL USE.")
    scans = client.get(f"/api/patients/{patient['id']}", headers=doctor_headers).json()["scans"]
    assert scans[0]["result_is_demo"] is True
    recent = client.get("/api/dashboard/recent-cases", headers=doctor_headers).json()
    assert recent[0]["result_is_demo"] is True


def test_fr05_4_model_version_timing_and_warnings_are_recorded(
    client, doctor_headers, patient, analysis_queue, db
):
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    result = result_of(client, doctor_headers, case["id"])
    assert result["model"]["name"] == "CaviNet demo model (synthetic data)"
    assert result["model"]["version"].startswith("demo-")
    assert result["model"]["folds"] == 5 and result["model"]["trained_at"]
    assert result["processing_seconds"] > 0
    assert set(result["step_seconds"]) >= {
        "load",
        "lung_mask",
        "resample",
        "previews",
        "inference",
        "total",
    }
    # No lungmask weights in the test environment: the documented fallback is recorded.
    assert result["warnings"] == [
        "Fallback crop: the lung segmentation model is not available, so the scan was cropped "
        "to the body outline."
    ]
    db.expire_all()
    stored = db.get(Case, uuid.UUID(case["id"]))
    assert stored.model_version == result["model"]["version"]
    assert stored.processing_seconds == result["processing_seconds"]


def test_fr09_2_viewing_a_result_is_audited(
    client, doctor, doctor_headers, patient, analysis_queue, db
):
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    result_of(client, doctor_headers, case["id"])
    entries = audit_entries(db, AuditAction.RESULT_VIEWED)
    assert [(e.user_id, e.target_type, e.target_id, e.details) for e in entries] == [
        (doctor.id, "case", case["id"], None)
    ]
    assert "Amina" not in json.dumps([e.details for e in audit_entries(db)])


def test_no_result_until_the_case_completes(client, doctor_headers, patient):
    response = upload(client, doctor_headers, patient["id"], [("scan.zip", scan_zip(50))])
    case_id = response.json()["id"]
    result = client.get(f"/api/cases/{case_id}/result", headers=doctor_headers)
    assert result.status_code == 404
    assert result.json() == {"detail": "This case has no AI result yet.", "code": "no_result"}
    assert client.get(f"/api/cases/{case_id}/previews/0", headers=doctor_headers).status_code == 404


def test_phase4_stub_results_have_no_ai_result(client, doctor_headers, patient, db):
    case_id = upload(client, doctor_headers, patient["id"], [("s.zip", scan_zip(50))]).json()["id"]
    case = db.get(Case, uuid.UUID(case_id))
    for status in (CaseStatus.PREPROCESSING, CaseStatus.ANALYSING, CaseStatus.COMPLETED):
        case.status = status.value
    case.result_label, case.result_is_stub = "STUB", True
    db.commit()
    assert client.get(f"/api/cases/{case_id}/result", headers=doctor_headers).status_code == 404
    summary = client.get(f"/api/cases/{case_id}", headers=doctor_headers).json()["result"]
    assert summary["is_stub"] is True and summary["is_demo"] is False


def test_results_are_doctor_only(client, doctor_headers, admin_headers, patient, analysis_queue):
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    for path in (f"/api/cases/{case['id']}/result", f"/api/cases/{case['id']}/previews/0"):
        assert client.get(path, headers=admin_headers).status_code == 403


# --- FR-05.3 previews and FR-06.4 slice viewer --------------------------------------------------


def test_fr05_3_previews_are_generated_and_served(client, doctor_headers, patient, analysis_queue):
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    result = result_of(client, doctor_headers, case["id"])
    assert result["preview_count"] == 48
    folder = storage.case_dir(uuid.UUID(case["id"])) / "previews"
    names = sorted(p.name for p in folder.iterdir())
    assert len([n for n in names if n.startswith("preview-")]) == 48
    assert [n for n in names if n.startswith("representative-")] == [
        "representative-1.png",
        "representative-2.png",
        "representative-3.png",
    ]

    response = client.get(f"/api/cases/{case['id']}/previews/0", headers=doctor_headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert "private" in response.headers["cache-control"]
    image = Image.open(io.BytesIO(response.content))
    assert image.mode == "L" and max(image.size) == 512
    last = client.get(f"/api/cases/{case['id']}/previews/47", headers=doctor_headers)
    assert last.status_code == 200
    assert (
        client.get(f"/api/cases/{case['id']}/previews/48", headers=doctor_headers).status_code
        == 404
    )


def test_previews_contain_no_patient_identity(client, doctor_headers, patient, analysis_queue):
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    folder = storage.case_dir(uuid.UUID(case["id"]))
    for path in folder.rglob("*"):
        if path.is_file():
            assert b"SYNTHETIC^TESTPATIENT" not in path.read_bytes()
            assert b"Amina" not in path.read_bytes()


# --- JPEG-Lossless scans are decoded end to end -----------------------------------------------


def jpeg_lossless_zip(tmp_path, slices: int = 55) -> bytes:
    """The synthetic scan re-encoded as JPEG Lossless (1.2.840.10008.1.2.4.70) by GDCM."""
    files = []
    for index, data in enumerate(SyntheticStudy("jpeg").series(slices)):
        source = tmp_path / f"in-{index}.dcm"
        target = tmp_path / f"out-{index}.dcm"
        source.write_bytes(data)
        writer = sitk.ImageFileWriter()
        writer.KeepOriginalImageUIDOn()
        writer.SetImageIO("GDCMImageIO")
        writer.SetUseCompression(True)
        writer.SetCompressor("JPEG")
        writer.SetFileName(str(target))
        writer.Execute(sitk.ReadImage(str(source), sitk.sitkInt16))
        files.append(target.read_bytes())
    syntax = pydicom.dcmread(io.BytesIO(files[0]), stop_before_pixels=True).file_meta
    assert syntax.TransferSyntaxUID == "1.2.840.10008.1.2.4.70"
    return zip_files(files)


def test_jpeg_lossless_upload_is_stored_decoded_and_analysed(
    client, doctor_headers, patient, analysis_queue, tmp_path
):
    case = completed_case(
        client, doctor_headers, patient["id"], analysis_queue, jpeg_lossless_zip(tmp_path)
    )
    stored = next(storage.case_dicom_dir(uuid.UUID(case["id"])).glob("*.dcm"))
    assert pydicom.dcmread(stored).file_meta.TransferSyntaxUID == "1.2.840.10008.1.2.4.70"
    assert result_of(client, doctor_headers, case["id"])["preview_count"] == 48


# --- Model status (FR-05.6) and the admin model page (FR-09.4) ---------------------------------


def test_model_status_is_visible_to_every_signed_in_user(client, doctor_headers, admin_headers):
    for headers in (doctor_headers, admin_headers):
        status = client.get("/api/model/status", headers=headers).json()
        assert status["installed"] is True and status["is_demo"] is True
        assert status["demo_banner"] == BANNER
        assert status["model_name"] == "CaviNet demo model (synthetic data)"
    assert client.get("/api/model/status").status_code == 401


def test_model_status_without_a_model(client, doctor_headers, monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "missing.pth"))
    get_settings.cache_clear()
    status = client.get("/api/model/status", headers=doctor_headers).json()
    assert status == {
        "installed": False,
        "is_demo": False,
        "model_name": None,
        "model_version": None,
        "created_at": None,
        "demo_banner": None,
    }


def test_fr09_4_admin_sees_version_training_date_demo_flag_and_metrics(
    client, admin_headers, installed_model
):
    from cavinet_ml.model.bundle import file_sha256, load_bundle

    bundle = load_bundle(installed_model)
    info = client.get("/api/admin/model", headers=admin_headers).json()
    assert info["model_name"] == bundle["model_name"]
    assert info["model_version"] == bundle["model_version"]
    assert info["created_at"] == bundle["created_at"]  # training date
    assert info["is_demo"] is True and info["demo_banner"] == BANNER
    assert info["metrics"] == bundle["metrics"]
    assert info["folds"] == 5 and info["parameters_per_fold"] > 0
    assert info["temperature"] == bundle["temperature"]
    assert info["decision_threshold"] == 0.5
    assert info["confidence_bands"] == {"high": 0.8, "moderate": 0.65}
    assert info["label_map"] == {"1": "TB", "0": "NTM"}
    assert info["preprocessing"]["target_spacing_mm"] == 1.5
    assert info["file_sha256"] == file_sha256(installed_model)
    assert info["file_size_bytes"] == installed_model.stat().st_size
    assert "fold_state_dicts" not in info


def test_admin_model_page_without_a_model_and_for_doctors(
    client, admin_headers, doctor_headers, monkeypatch, tmp_path
):
    assert client.get("/api/admin/model", headers=doctor_headers).status_code == 403
    broken = tmp_path / "cavinet_model.pth"
    broken.write_bytes(b"broken")
    monkeypatch.setenv("MODEL_PATH", str(broken))
    get_settings.cache_clear()
    model_store.clear_caches()
    response = client.get("/api/admin/model", headers=admin_headers)
    assert response.status_code == 404 and response.json()["code"] == "no_model"


def test_the_synthetic_zip_helper_is_a_zip():
    assert zipfile.is_zipfile(io.BytesIO(scan_zip(50)))
