"""M-12 symptom-assisted prediction in the application (FR-12.1 to FR-12.3)."""

import json
import shutil
import uuid

import pytest

from app.models import Case
from app.services import model_store
from tests.conftest import audit_entries, create_patient, run_queued_jobs, scan_zip
from tests.test_reports import download, pdf_text
from tests.test_results import result_of

COMBINED_LABEL = "CT + symptoms"


@pytest.fixture
def patient(client, doctor_headers) -> dict:
    # Born 1975-04-12, female (tests.conftest.PATIENT).
    return create_patient(client, doctor_headers)


@pytest.fixture
def with_fusion(monkeypatch, tmp_path, installed_model):
    """The demo model with its demo fusion parameters beside it (as `fetch-model` does)."""
    from cavinet_ml.fetch import install_fusion

    path = tmp_path / "models" / "cavinet_model.pth"
    path.parent.mkdir()
    shutil.copy(installed_model, path)
    install_fusion(path, log=lambda m: None)
    monkeypatch.setenv("MODEL_PATH", str(path))
    model_store.clear_caches()
    from app.core.config import get_settings

    get_settings.cache_clear()
    return path


def upload_with(client, headers, patient_id, symptoms=None, raw: str | None = None):
    data = {}
    if raw is not None:
        data["symptoms"] = raw
    elif symptoms is not None:
        data["symptoms"] = json.dumps(symptoms)
    return client.post(
        f"/api/patients/{patient_id}/cases",
        data=data,
        files=[("files", ("scan.zip", scan_zip(60), "application/zip"))],
        headers=headers,
    )


def completed(client, headers, patient_id, queue, symptoms=None) -> dict:
    response = upload_with(client, headers, patient_id, symptoms)
    assert response.status_code == 201, response.json()
    run_queued_jobs(queue)
    case = client.get(f"/api/cases/{response.json()['id']}", headers=headers).json()
    assert case["status"] == "completed", case
    return case


# --- FR-12.1: symptoms at upload ---------------------------------------------------------------


def test_fr12_1_symptoms_are_stored_in_the_dataset_order(
    client, doctor_headers, patient, db, analysis_queue
):
    response = upload_with(client, doctor_headers, patient["id"], ["night_sweats", "cough"])
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["upload"]["symptoms"] == ["Cough", "Night sweats"]
    assert db.get(Case, uuid.UUID(body["id"])).symptoms == ["cough", "night_sweats"]
    (entry,) = audit_entries(db, "scan_uploaded")
    assert entry.details["symptoms_entered"] is True


def test_no_symptoms_field_means_not_entered_and_an_empty_list_means_none_present(
    client, doctor_headers, patient
):
    without = upload_with(client, doctor_headers, patient["id"]).json()
    assert without["upload"]["symptoms"] is None
    none_present = upload_with(client, doctor_headers, patient["id"], []).json()
    assert none_present["upload"]["symptoms"] == []


@pytest.mark.parametrize(
    "raw",
    ['["sneezing"]', "cough", '{"cough": true}', "[1, 2]", '["cough"' + ", " * 3000 + "]"],
)
def test_fr12_1_invalid_symptoms_are_rejected_with_a_plain_message(
    client, doctor_headers, patient, raw
):
    response = upload_with(client, doctor_headers, patient["id"], raw=raw)
    assert response.status_code == 422
    assert response.json()["code"] == "bad_symptoms"


# --- FR-12.2 / FR-12.3: the combined result ----------------------------------------------------


def test_model_status_says_whether_symptom_assisted_prediction_is_available(
    client, doctor_headers, with_fusion
):
    status = client.get("/api/model/status", headers=doctor_headers).json()
    assert status["symptom_assisted"] is True


def test_fr12_2_combined_result_with_age_sex_and_symptoms(
    client, doctor_headers, patient, analysis_queue, with_fusion
):
    from cavinet_ml.fusion import load_fusion

    case = completed(client, doctor_headers, patient["id"], analysis_queue, ["fever", "cough"])
    result = result_of(client, doctor_headers, case["id"])
    combined = result["combined"]
    assert result["symptoms"] == ["Cough", "Fever"] and result["combined_note"] is None
    assert combined["symptoms"] == ["Cough", "Fever"]
    assert combined["sex"] == "female" and combined["is_demo"] is True
    assert combined["fusion_version"] == "demo"
    # Age on the scan's study date (or the upload date when the scan has none).
    assert 40 <= combined["age"] <= 60
    # The combined probability is the fusion of this case's CT logit, age, sex and symptoms.
    fusion = load_fusion(with_fusion.with_name("cavinet_fusion.json"))
    from app.core.database import get_sessionmaker

    with get_sessionmaker()() as session:
        stored = session.get(Case, uuid.UUID(case["id"]))
        expected = fusion.predict(
            ct_logit=stored.result_details["logit"],
            age=combined["age"],
            sex="female",
            symptoms=["cough", "fever"],
        )
    assert combined["probability_tb_pct"] == round(expected * 100, 1)
    assert combined["predicted_class"] == ("TB" if expected >= 0.5 else "NTM")
    assert 50.0 <= combined["confidence_pct"] <= 100.0
    assert combined["band"] in ("High", "Moderate", "Low")
    assert "Confirm with laboratory testing" in combined["explanation"]


def test_fr12_3_without_symptoms_only_the_ct_result_is_shown(
    client, doctor_headers, patient, analysis_queue, with_fusion
):
    case = completed(client, doctor_headers, patient["id"], analysis_queue)
    result = result_of(client, doctor_headers, case["id"])
    assert result["symptoms"] is None
    assert result["combined"] is None and result["combined_note"] is None
    text = pdf_text(download(client, doctor_headers, case["id"], tz="UTC").content)
    assert "CT only" not in text and COMBINED_LABEL not in text


def test_none_of_the_symptoms_present_still_gives_a_combined_result(
    client, doctor_headers, patient, analysis_queue, with_fusion
):
    case = completed(client, doctor_headers, patient["id"], analysis_queue, [])
    result = result_of(client, doctor_headers, case["id"])
    assert result["symptoms"] == [] and result["combined"]["symptoms"] == []


def test_fr12_3_the_report_shows_both_results_on_one_page(
    client, doctor_headers, patient, analysis_queue, with_fusion
):
    import io

    import pypdf

    case = completed(client, doctor_headers, patient["id"], analysis_queue, ["night_sweats"])
    data = download(client, doctor_headers, case["id"], tz="UTC").content
    text = pdf_text(data)
    assert "CT only" in text and COMBINED_LABEL in text
    assert "Night sweats" in text and "Symptom-assisted model demo" in text
    assert len(pypdf.PdfReader(io.BytesIO(data)).pages) == 1


def test_symptoms_without_installed_fusion_parameters_show_ct_only_with_a_note(
    client, doctor_headers, patient, analysis_queue
):
    status = client.get("/api/model/status", headers=doctor_headers).json()
    assert status["symptom_assisted"] is False
    case = completed(client, doctor_headers, patient["id"], analysis_queue, ["cough"])
    result = result_of(client, doctor_headers, case["id"])
    assert result["combined"] is None and result["symptoms"] == ["Cough"]
    assert "only the CT result is shown" in result["combined_note"]
    text = pdf_text(download(client, doctor_headers, case["id"], tz="UTC").content)
    assert "only the CT result is shown" in text and COMBINED_LABEL not in text


def test_fusion_parameters_for_another_model_are_ignored(
    client, doctor_headers, patient, analysis_queue, with_fusion
):
    path = with_fusion.with_name("cavinet_fusion.json")
    params = json.loads(path.read_text(encoding="utf-8"))
    params["bundle_weights_sha256"] = "0" * 64
    path.write_text(json.dumps(params), encoding="utf-8")
    model_store.clear_caches()
    assert model_store.get_fusion() is None
    case = completed(client, doctor_headers, patient["id"], analysis_queue, ["cough"])
    assert result_of(client, doctor_headers, case["id"])["combined"] is None


def test_a_broken_fusion_file_never_fails_the_analysis(
    client, doctor_headers, patient, analysis_queue, with_fusion, monkeypatch
):
    from cavinet_ml.fusion import FusionModel

    def broken(*args, **kwargs):
        raise RuntimeError("no fusion")

    monkeypatch.setattr(FusionModel, "predict", broken)
    case = completed(client, doctor_headers, patient["id"], analysis_queue, ["cough"])
    result = result_of(client, doctor_headers, case["id"])
    assert result["combined"] is None and result["predicted_class"] in ("TB", "NTM")

    with_fusion.with_name("cavinet_fusion.json").write_text("not json", encoding="utf-8")
    model_store.clear_caches()
    assert model_store.get_fusion() is None
