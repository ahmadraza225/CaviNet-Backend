"""The result of a completed case (M-06, FR-06.1 to FR-06.4)."""

from pathlib import Path

from app.core.clock import as_utc
from app.models import Case, CaseStatus, Patient
from app.schemas.cases import CasePatient, ResultModel, ResultOut, ValidatedPerformance
from app.services import storage
from app.services.errors import NotFound
from app.services.model_store import DEMO_BANNER

DISCLAIMER = "Decision support only. Not a diagnosis. Confirm with laboratory tests."


def _no_result() -> NotFound:
    return NotFound("This case has no AI result yet.", code="no_result")


def has_result(case: Case) -> bool:
    return (
        case.status == CaseStatus.COMPLETED
        and not case.result_is_stub
        and case.probability_tb is not None
    )


def result_for(case: Case, patient: Patient) -> ResultOut:
    if not has_result(case):
        raise _no_result()
    details = case.result_details or {}
    model = details.get("model", {})
    test = (model.get("metrics") or {}).get("locked_test") or {}
    return ResultOut(
        case_id=case.id,
        patient=CasePatient(
            id=patient.id, full_name=patient.full_name, mr_number=patient.mr_number
        ),
        predicted_class=case.result_label,
        probability_tb=case.probability_tb,
        probability_tb_pct=details.get("probability_tb_pct", round(case.probability_tb * 100, 1)),
        confidence_pct=details.get("confidence_pct", round((case.confidence or 0) * 100, 1)),
        band=case.confidence_band,
        inconclusive=bool(details.get("inconclusive", case.confidence_band == "Low")),
        explanation=details.get("explanation", ""),
        disclaimer=DISCLAIMER,
        is_demo=case.model_is_demo,
        demo_banner=DEMO_BANNER if case.model_is_demo else None,
        model=ResultModel(
            name=case.model_name or model.get("name", "unknown"),
            version=case.model_version or str(model.get("version", "")),
            trained_at=model.get("created_at"),
            folds=model.get("folds"),
            is_demo=case.model_is_demo,
        ),
        validated_performance=ValidatedPerformance(
            auc=test.get("auc"),
            sensitivity=test.get("sensitivity"),
            specificity=test.get("specificity"),
            cases=test.get("n"),
            dataset=(model.get("metrics") or {}).get("dataset"),
        ),
        warnings=case.warnings or [],
        processing_seconds=case.processing_seconds,
        step_seconds=details.get("seconds", {}),
        lung_volume_ml=details.get("lung_volume_ml"),
        preview_count=int((details.get("previews") or {}).get("count", 0)),
        completed_at=as_utc(case.completed_at),
    )


def preview_path(case: Case, index: int) -> Path:
    """FR-06.4 slice viewer image `index` (0 = nearest the head)."""
    previews = (case.result_details or {}).get("previews") or {}
    files = previews.get("files") or []
    if not has_result(case) or not 0 <= index < len(files):
        raise NotFound("Preview not found.")
    path = storage.case_dir(case.id) / "previews" / files[index]
    if not path.is_file():
        raise NotFound("Preview not found.")
    return path
