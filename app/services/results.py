"""The result of a completed case (M-06, FR-06.1 to FR-06.4)."""

from pathlib import Path

from app.core.clock import as_utc
from app.models import Case, CaseStatus, Patient
from app.schemas.cases import (
    CasePatient,
    CombinedResult,
    HeatmapInfo,
    ResultModel,
    ResultOut,
    ValidatedPerformance,
    symptom_names,
)
from app.services import storage
from app.services.errors import NotFound
from app.services.model_store import DEMO_BANNER

DISCLAIMER = "Decision support only. Not a diagnosis. Confirm with laboratory tests."
NO_COMBINED = (
    "Symptoms were entered, but no symptom-assisted model is installed for this AI model, "
    "so only the CT result is shown."
)


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
        heatmap=heatmap_info(details),
        symptoms=symptom_names(case.symptoms),
        combined=combined_info(details),
        combined_note=NO_COMBINED
        if case.symptoms is not None and not details.get("combined")
        else None,
        completed_at=as_utc(case.completed_at),
    )


def combined_info(details: dict) -> CombinedResult | None:
    """M-12: present when symptoms were entered and a fusion model was installed."""
    combined = details.get("combined")
    if not combined:
        return None
    inputs = combined.get("inputs") or {}
    return CombinedResult(
        predicted_class=combined["predicted_class"],
        probability_tb_pct=combined["probability_tb_pct"],
        confidence_pct=combined["confidence_pct"],
        band=combined["band"],
        inconclusive=bool(combined["inconclusive"]),
        explanation=combined["explanation"],
        age=inputs.get("age"),
        sex=inputs.get("sex"),
        symptoms=symptom_names(inputs.get("symptoms") or []) or [],
        fusion_version=str(combined.get("fusion_version", "")),
        is_demo=bool(combined.get("is_demo")),
    )


def heatmap_info(details: dict) -> HeatmapInfo | None:
    """M-11: present for cases analysed since the heatmap was added (Phase 10)."""
    heatmap = details.get("heatmap")
    if not heatmap or not heatmap.get("files"):
        return None
    return HeatmapInfo(
        label=heatmap["label"],
        target=heatmap["target"],
        count=len(heatmap["files"]),
        top=list(heatmap.get("top") or []),
        lung_share=heatmap.get("lung_share"),
    )


def heatmap_path(case: Case, index: int) -> Path:
    """FR-11.2: the transparent heatmap overlay of preview `index`."""
    files = ((case.result_details or {}).get("heatmap") or {}).get("files") or []
    if not has_result(case) or not 0 <= index < len(files):
        raise NotFound("Heatmap not found.")
    path = storage.case_dir(case.id) / "previews" / files[index]
    if not path.is_file():
        raise NotFound("Heatmap not found.")
    return path


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
