"""The analysis job (M-05): Queued → Preprocessing → Analysing → Completed / Failed.

FR-05.5 failure handling:
- the scan cannot be analysed (unreadable series) or no model is installed: Failed at once,
  with the reason;
- anything else is unexpected and may be transient: the analysis is retried once, then the
  case fails with a plain reason. A case the worker was in the middle of when it stopped is
  resumed once when the worker starts again (see `recover_interrupted`).
"""

import logging
import time
import uuid
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Case, CaseStatus
from app.services import cases, model_store, storage
from app.services.model_store import ModelUnavailable

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2  # the first try and one retry (FR-05.5)
RETRYING = "A temporary error interrupted the analysis; retrying once."
UNEXPECTED = "The analysis stopped because of an unexpected error. Please upload the scan again."
RESUMED = "The analysis was interrupted when the worker stopped; resuming once."
GAVE_UP = "The analysis was interrupted twice when the worker stopped, so it was abandoned."
PREPROCESSING_NOTE = "Loading the scan, finding the lungs and preparing the model input."


def _analysing_note(analyser) -> str:
    ensemble = analyser.ensemble
    note = f"Running the AI model (ensemble of {len(ensemble.models)})."
    return f"{note} DEMO MODEL: NOT FOR CLINICAL USE." if ensemble.is_demo else note


def _store_result(case: Case, result) -> None:
    decision = result.decision
    case.result_label = decision["predicted_class"]
    case.result_is_stub = False
    case.probability_tb = round(float(decision["probability_tb"]), 6)
    case.confidence = round(float(decision["confidence"]), 6)
    case.confidence_band = decision["band"]
    case.model_name = result.model["name"][:120]
    case.model_version = str(result.model["version"])[:64]
    case.model_is_demo = bool(result.model["is_demo"])
    case.analyser = case.model_name[:64]
    case.processing_seconds = result.seconds["total"]
    case.warnings = list(result.warnings)
    case.result_details = {
        "explanation": decision["explanation"],
        "probability_tb_pct": decision["probability_tb_pct"],
        "confidence_pct": decision["confidence_pct"],
        "inconclusive": decision["inconclusive"],
        "logit": round(result.logit, 6),
        "fold_logits": [round(v, 6) for v in result.fold_logits],
        "seconds": result.seconds,
        "lung_volume_ml": result.lung_volume_ml,
        "used_fallback_crop": result.used_fallback_crop,
        "previews": {
            "count": result.previews.count,
            "files": result.previews.files,
            "representative": result.previews.representative,
        },
        "model": {
            "name": result.model["name"],
            "version": result.model["version"],
            "created_at": result.model["created_at"],
            "folds": result.model["folds"],
            "metrics": result.model["metrics"],
        },
    }


def _analyse(db: Session, case: Case, analyser) -> str:
    preview_dir = storage.case_dir(case.id) / "previews"
    storage.remove_paths([preview_dir])  # nothing left over from an interrupted attempt
    if case.status == CaseStatus.QUEUED:
        cases.set_status(db, case, CaseStatus.PREPROCESSING, PREPROCESSING_NOTE)
        db.commit()
    prepared = analyser.prepare(storage.case_dicom_dir(case.id), preview_dir)
    if case.status == CaseStatus.PREPROCESSING:
        cases.set_status(db, case, CaseStatus.ANALYSING, _analysing_note(analyser))
        db.commit()
    result = analyser.infer(prepared)
    _store_result(case, result)
    decision = result.decision
    cases.set_status(
        db,
        case,
        CaseStatus.COMPLETED,
        f"Result: {decision['predicted_class']} (probability of TB "
        f"{decision['probability_tb_pct']:.1f}%, confidence {decision['band']}).",
    )
    db.commit()
    return case.status


def run(
    db: Session,
    case_id: uuid.UUID,
    *,
    get_analyser: Callable[[], object] = model_store.get_analyser,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Analyse one case. Returns the final status, or why the job did nothing."""
    from cavinet_ml.analysis import PipelineError

    case = db.get(Case, case_id)
    if case is None:
        return "missing"  # the patient (and so the case) was deleted meanwhile
    if case.status not in (CaseStatus.QUEUED, CaseStatus.PREPROCESSING, CaseStatus.ANALYSING):
        return f"skipped ({case.status})"
    try:
        analyser = get_analyser()
    except ModelUnavailable as error:
        cases.fail(db, case, str(error))
        return case.status

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return _analyse(db, case, analyser)
        except PipelineError as error:
            db.rollback()
            case = db.get(Case, case_id)
            if case is None:
                return "missing"
            cases.fail(db, case, str(error))
            return case.status
        except Exception as error:  # noqa: BLE001 - must never leave a case hanging
            # The type only: messages could quote file contents (NFR-3).
            logger.error(
                "Analysis attempt %d failed for case %s: %s", attempt, case_id, type(error).__name__
            )
            db.rollback()
            case = db.get(Case, case_id)
            if case is None:
                return "missing"
            if attempt < MAX_ATTEMPTS and not case.is_final:
                cases.note(db, case, RETRYING)
                db.commit()
                sleep(get_settings().analysis_retry_delay_seconds)
                continue
            cases.fail(db, case, UNEXPECTED)
            return case.status
    return case.status  # pragma: no cover - the loop always returns


def recover_interrupted(db: Session, enqueue: Callable[[Session, Case], None]) -> int:
    """At worker start-up: cases left in Preprocessing or Analysing by a stopped worker are
    resumed once; a second interruption fails them. (CaviNet runs one worker.)"""
    stuck = db.scalars(
        select(Case).where(Case.status.in_([CaseStatus.PREPROCESSING, CaseStatus.ANALYSING]))
    ).all()
    for case in stuck:
        case.analysis_attempts += 1
        if case.analysis_attempts >= MAX_ATTEMPTS:
            cases.fail(db, case, GAVE_UP)
            continue
        cases.note(db, case, RESUMED)
        db.commit()
        enqueue(db, case)
    return len(stuck)
