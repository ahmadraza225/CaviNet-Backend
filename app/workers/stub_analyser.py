"""TEMPORARY stub analyser (Phase 4 only). It performs NO AI analysis.

It walks a queued case through Preprocessing → Analysing → Completed so the upload workflow,
timeline and notifications can be used and tested before the real pipeline arrives in
Phase 5. Its result is the placeholder label "STUB", flagged `result_is_stub`, and every
screen says so.
"""

import logging
import time
import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Case, CaseStatus
from app.services import cases, storage

logger = logging.getLogger(__name__)

STUB_ANALYSER = "stub-analyser (Phase 4 placeholder)"
STUB_LABEL = "STUB"
STUB_NOTE = "Placeholder from the Phase 4 stub analyser. No AI analysis was performed."
MISSING_FILES = "The stored scan files are missing or incomplete, so the scan cannot be analysed."
UNEXPECTED = "The analysis stopped because of an unexpected error."


class StubAnalysisError(Exception):
    """A failure whose message is safe to show to the doctor."""


def run(db: Session, case_id: uuid.UUID, *, sleep: Callable[[float], None] = time.sleep) -> str:
    """Analyse one case with the stub. Returns the final status (or why it was skipped)."""
    case = db.get(Case, case_id)
    if case is None:
        return "missing"  # the patient (and the case) was deleted meanwhile
    if case.status != CaseStatus.QUEUED:
        return f"skipped ({case.status})"
    pause = get_settings().stub_step_seconds
    try:
        cases.set_status(
            db, case, CaseStatus.PREPROCESSING, "Stub analyser: no preprocessing until Phase 5."
        )
        db.commit()
        sleep(pause)
        stored = list(storage.case_dicom_dir(case.id).glob("*.dcm"))
        if len(stored) != case.num_slices:
            raise StubAnalysisError(MISSING_FILES)
        cases.set_status(db, case, CaseStatus.ANALYSING, "Stub analyser: no AI model is used.")
        db.commit()
        sleep(pause)
        case.result_label = STUB_LABEL
        case.result_is_stub = True
        case.analyser = STUB_ANALYSER
        case.result_details = {"note": STUB_NOTE}
        cases.set_status(
            db, case, CaseStatus.COMPLETED, "STUB result: placeholder, not a diagnosis."
        )
        db.commit()
    except StubAnalysisError as error:
        db.rollback()
        return _fail(db, case_id, str(error))
    except Exception as error:  # noqa: BLE001 - a failed job must never leave a case hanging
        logger.error("Stub analysis failed for case %s: %s", case_id, type(error).__name__)
        db.rollback()
        return _fail(db, case_id, UNEXPECTED)
    return case.status


def _fail(db: Session, case_id: uuid.UUID, reason: str) -> str:
    case = db.get(Case, case_id)
    if case is None:
        return "missing"
    cases.fail(db, case, reason)
    return case.status
