"""M-07 diagnostic PDF report (FR-07.1 to FR-07.3)."""

import io
import re
import uuid
from datetime import date

import pypdf
import pytest

from app.core import clock
from app.models import AuditAction, Case
from app.services import report, storage
from tests.conftest import audit_entries, create_patient, scan_zip, upload
from tests.test_results import DISCLAIMER, completed_case, result_of

BANNER = "DEMO MODEL: NOT FOR CLINICAL USE"


@pytest.fixture
def patient(client, doctor_headers) -> dict:
    return create_patient(client, doctor_headers)


def download(client, headers, case_id, **params):
    return client.get(f"/api/cases/{case_id}/report", headers=headers, params=params)


def pdf_text(data: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(data))
    text = "\n".join(page.extract_text() for page in reader.pages)
    return " ".join(text.split())  # one space between words, whatever the layout


def pdf_image_placements(data: bytes) -> int:
    """How many times an image is drawn (identical images are embedded only once)."""
    reader = pypdf.PdfReader(io.BytesIO(data))
    count = 0
    for page in reader.pages:
        xobjects = page["/Resources"].get("/XObject", {})
        images = {
            name for name, ref in xobjects.items() if ref.get_object()["/Subtype"] == "/Image"
        }
        content = page.get_contents().get_data()
        count += sum(name.decode() in images for name in re.findall(rb"(/[^\s/]+)\s+Do", content))
    return count


# --- FR-07.1 ---------------------------------------------------------------------------------


def test_fr07_1_completed_case_downloads_as_a_pdf(client, doctor_headers, patient, analysis_queue):
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    response = download(client, doctor_headers, case["id"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
    today = clock.utcnow().date().isoformat()
    assert response.headers["content-disposition"] == (
        f'attachment; filename="CaviNet-report-MR-1001-{today}.pdf"'
    )
    # The report names the patient: never cached by the browser or a proxy.
    assert response.headers["cache-control"] == "no-store"


def test_fr07_1_no_report_before_the_analysis_completes(
    client, doctor_headers, patient, analysis_queue
):
    queued = upload(client, doctor_headers, patient["id"], [("scan.zip", scan_zip(60))]).json()
    assert queued["status"] == "queued"  # the job has not run
    response = download(client, doctor_headers, queued["id"])
    assert response.status_code == 404 and response.json()["code"] == "no_result"

    failed = upload(client, doctor_headers, patient["id"], [("short.zip", scan_zip(30))]).json()
    assert failed["status"] == "failed"
    assert download(client, doctor_headers, failed["id"]).status_code == 404


def test_fr07_1_unknown_case_is_404(client, doctor_headers):
    response = download(client, doctor_headers, "00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


# --- FR-07.2 ---------------------------------------------------------------------------------


def test_fr07_2_report_contains_every_required_item(
    client, doctor_headers, patient, analysis_queue, installed_model
):
    from cavinet_ml.model.bundle import load_bundle

    card = load_bundle(installed_model)["metrics"]["locked_test"]
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    result = result_of(client, doctor_headers, case["id"])
    data = download(client, doctor_headers, case["id"], tz="UTC").content
    text = pdf_text(data)

    required = {
        "header": "CaviNet Decision-support report: pulmonary TB vs NTM",
        "report date": f"Report date {clock.utcnow():%d %b %Y}",
        "patient name": "Amina Bibi",
        "MR number": "MR number MR-1001",
        # Born 12 Apr 1975, scanned 15 Sep 2026 (the synthetic study date).
        "age": "Age 51 years",
        "sex": "Sex Female",
        "scan details": "Study date 15 Sep 2026",
        "slices": "Slices 60",
        "slice thickness": "Slice thickness 1.25 mm",
        "scanner": "SYNTHETIC CaviNet Synthetic CT",
        "kernel": "Kernel STANDARD",
        "probability": f"Probability of TB {result['probability_tb_pct']:.1f}%",
        "confidence band": f"Confidence {result['confidence_pct']:.1f}% ({result['band']})",
        "band explanation": f"Confidence band: {result['band']}. {result['explanation']}",
        "band legend": "Confidence bands: High 80% or more; Moderate 65% to 79.9%; Low below 65%",
        "slices section": "Representative slices",
        "model version": f"Version {result['model']['version']}",
        "test AUC": f"Test AUC {card['auc']:.2f}",
        "sensitivity": f"Sensitivity (TB) {card['sensitivity'] * 100:.1f}%",
        "specificity": f"Specificity (NTM) {card['specificity'] * 100:.1f}%",
        "disclaimer": DISCLAIMER,
        "signature line": "Reviewing doctor (name) Signature Date",
        "demo banner": BANNER,
    }
    missing = {name: value for name, value in required.items() if value not in text}
    assert not missing, f"missing from the PDF: {missing}\n\n{text}"
    label = (
        f"Inconclusive (leans towards {result['predicted_class']})"
        if result["inconclusive"]
        else f"Result {result['predicted_class']}"
    )
    assert label in text
    assert pdf_image_placements(data) == 3  # the 3 representative slices (FR-05.3)
    assert len(pypdf.PdfReader(io.BytesIO(data)).pages) == 1  # one printed A4 page
    assert "Slice thickness" in text and "Pixel spacing" in text


def test_fr07_2_report_without_validated_metrics_says_so(
    client, doctor_headers, patient, analysis_queue, db
):
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    stored = db.get(Case, uuid.UUID(case["id"]))
    details = dict(stored.result_details)
    details["model"] = {**details["model"], "metrics": {"locked_test": None}}
    stored.result_details = details
    db.commit()
    text = pdf_text(download(client, doctor_headers, case["id"]).content)
    assert "Test AUC Not measured" in text
    assert "Not yet measured on real patients. This is the demo model." in text


def test_fr07_2_missing_slice_images_do_not_break_the_report(
    client, doctor_headers, patient, analysis_queue
):
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    for path in (storage.case_dir(case["id"]) / "previews").glob("representative-*.png"):
        path.unlink()
    response = download(client, doctor_headers, case["id"])
    assert response.status_code == 200
    assert pdf_text(response.content).count("Image not available") == 3


def test_fr07_2_names_with_accents_and_markup_characters_print_as_typed(
    client, doctor_headers, analysis_queue
):
    patient = create_patient(
        client, doctor_headers, full_name="Zoë Núñez <Bibi> & Co", mr_number="MR-77/A"
    )
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    response = download(client, doctor_headers, case["id"])
    assert "Zoë Núñez <Bibi> & Co" in pdf_text(response.content)
    # The file name keeps only safe characters.
    assert re.search(
        r'filename="CaviNet-report-MR-77-A-\d{4}-\d\d-\d\d\.pdf"', str(response.headers)
    )


def test_fr07_2_report_date_uses_the_doctors_time_zone(
    client, doctor_headers, patient, analysis_queue
):
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    text = pdf_text(download(client, doctor_headers, case["id"], tz="Asia/Karachi").content)
    assert "Time zone: Asia/Karachi" in text
    text = pdf_text(download(client, doctor_headers, case["id"], tz="Not/AZone").content)
    assert "Time zone: UTC" in text


def test_fr07_2_report_is_generated_on_demand_and_never_stored(
    client, doctor_headers, patient, analysis_queue
):
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    assert download(client, doctor_headers, case["id"]).status_code == 200
    data_dir = storage.case_dir(case["id"]).parents[1]
    assert list(data_dir.rglob("*.pdf")) == []  # NFR-3: identity only in the patients table


@pytest.mark.parametrize(
    ("born", "on", "age"),
    [
        (date(1975, 4, 12), date(2026, 4, 11), 50),
        (date(1975, 4, 12), date(2026, 4, 12), 51),
        (date(2000, 2, 29), date(2026, 2, 28), 25),
        (date(2000, 2, 29), date(2026, 3, 1), 26),
    ],
)
def test_age_is_in_whole_years(born, on, age):
    assert report.age_on(born, on) == age


@pytest.mark.parametrize("name", [None, "", "../../etc/passwd", "Not/AZone", "x" * 80])
def test_unusable_time_zones_fall_back_to_utc(name):
    assert str(report.timezone_or_utc(name)) == "UTC"


# --- FR-07.3 ---------------------------------------------------------------------------------


def test_fr07_3_each_download_is_audited_without_patient_identity(
    client, doctor_headers, doctor, patient, analysis_queue, db
):
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    download(client, doctor_headers, case["id"])
    download(client, doctor_headers, case["id"])
    entries = audit_entries(db, AuditAction.REPORT_DOWNLOADED)
    assert len(entries) == 2
    entry = entries[0]
    assert entry.user_id == doctor.id and entry.actor_email == doctor.email
    assert entry.target_type == "case" and entry.target_id == case["id"]
    flat = repr((entry.details, entry.target_id, entry.actor_email))
    assert "Amina" not in flat and "MR-1001" not in flat


def test_fr07_3_refused_downloads_are_not_audited(client, doctor_headers, patient, db):
    failed = upload(client, doctor_headers, patient["id"], [("short.zip", scan_zip(30))]).json()
    assert download(client, doctor_headers, failed["id"]).status_code == 404
    assert audit_entries(db, AuditAction.REPORT_DOWNLOADED) == []


# --- Section 9.3: doctors only ---------------------------------------------------------------


def test_report_is_for_doctors_only(client, admin_headers, doctor_headers, patient, analysis_queue):
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    assert download(client, admin_headers, case["id"]).status_code == 403
    assert download(client, {}, case["id"]).status_code == 401
