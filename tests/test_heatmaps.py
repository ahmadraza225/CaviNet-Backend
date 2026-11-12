"""M-11 heatmap explanation in the application (FR-11.1 to FR-11.3)."""

import io
import uuid

import pypdf
import pytest
from PIL import Image

from app.models import Case
from tests.conftest import create_patient
from tests.test_reports import download, pdf_image_placements, pdf_text
from tests.test_results import completed_case, result_of

LABEL = "Regions that most influenced the prediction. This is not a lesion marker."


@pytest.fixture
def patient(client, doctor_headers) -> dict:
    return create_patient(client, doctor_headers)


def test_fr11_every_completed_case_has_a_heatmap_with_the_required_label(
    client, doctor_headers, patient, analysis_queue
):
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    result = result_of(client, doctor_headers, case["id"])
    heatmap = result["heatmap"]
    assert heatmap["label"] == LABEL  # FR-11.3, word for word
    assert heatmap["target"] == result["predicted_class"]
    assert heatmap["count"] == result["preview_count"] == 48
    assert len(heatmap["top"]) == 3 and len(set(heatmap["top"])) == 3
    assert all(0 <= i < 48 for i in heatmap["top"])
    assert 0.0 <= heatmap["lung_share"] <= 1.0
    # The heatmap time is part of the analysis time (NFR-1).
    assert result["step_seconds"]["heatmap"] > 0
    assert result["processing_seconds"] == pytest.approx(result["step_seconds"]["total"])


def test_fr11_2_heatmap_overlays_line_up_with_the_previews(
    client, doctor_headers, admin_headers, patient, analysis_queue
):
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    url = f"/api/cases/{case['id']}"
    response = client.get(f"{url}/heatmaps/24", headers=doctor_headers)
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert "private" in response.headers["cache-control"]
    overlay = Image.open(io.BytesIO(response.content))
    preview = Image.open(
        io.BytesIO(client.get(f"{url}/previews/24", headers=doctor_headers).content)
    )
    assert overlay.mode == "RGBA" and overlay.size == preview.size
    assert client.get(f"{url}/heatmaps/48", headers=doctor_headers).status_code == 404
    assert client.get(f"{url}/heatmaps/0", headers=admin_headers).status_code == 403


def test_a_failed_heatmap_never_fails_the_analysis(
    client, doctor_headers, patient, analysis_queue, monkeypatch
):
    from cavinet_ml.analysis import Analyser

    def broken(*args, **kwargs):
        raise RuntimeError("no heatmap")

    monkeypatch.setattr(Analyser, "explain", broken)
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    result = result_of(client, doctor_headers, case["id"])
    assert result["heatmap"] is None and "heatmap" not in result["step_seconds"]
    assert (
        client.get(f"/api/cases/{case['id']}/heatmaps/0", headers=doctor_headers).status_code == 404
    )
    text = pdf_text(download(client, doctor_headers, case["id"], tz="UTC").content)
    assert LABEL not in text and "Representative slices" in text


def test_fr11_2_the_report_shows_the_top_3_heatmap_slices_on_one_page(
    client, doctor_headers, patient, analysis_queue
):
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    result = result_of(client, doctor_headers, case["id"])
    data = download(client, doctor_headers, case["id"], tz="UTC").content
    text = pdf_text(data)
    assert f"Heatmap: what the model looked at (result {result['predicted_class']})" in text
    assert LABEL in text
    for index in result["heatmap"]["top"]:
        assert f"Slice {index + 1} of 48" in text
    assert pdf_image_placements(data) == 6  # 3 representative slices + 3 heatmap slices
    assert len(pypdf.PdfReader(io.BytesIO(data)).pages) == 1


def test_cases_analysed_before_the_heatmap_still_show(
    client, doctor_headers, patient, analysis_queue, db
):
    case = completed_case(client, doctor_headers, patient["id"], analysis_queue)
    stored = db.get(Case, uuid.UUID(case["id"]))
    stored.result_details = {k: v for k, v in stored.result_details.items() if k != "heatmap"}
    db.commit()
    assert result_of(client, doctor_headers, case["id"])["heatmap"] is None
    assert download(client, doctor_headers, case["id"], tz="UTC").status_code == 200
