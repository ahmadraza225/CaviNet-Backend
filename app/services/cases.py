"""Cases and their status workflow (M-04, M-08).

    Uploaded → Validating → Queued → Preprocessing → Analysing → Completed
                    └──────────┴───────────┴──────────────┴─────→ Failed (with a reason)

The API handles Uploaded → Validating → Queued (or Failed) while the upload request is open:
the files are checked and de-identified before anything is stored (FR-04.2, FR-04.4). The
worker takes the case from Queued to the end (FR-04.6; a stub analyser in Phase 4).
"""

import logging
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.models import (
    FINAL_STATUSES,
    IN_PROGRESS_STATUSES,
    AuditAction,
    Case,
    CaseEvent,
    CaseStatus,
    Patient,
    User,
)
from app.services import audit, notifications, storage
from app.services.dicom_intake import ScanRejected, intake
from app.services.errors import NotFound
from app.services.uploads import StagedUpload
from app.workers.queue import get_analysis_queue

logger = logging.getLogger(__name__)

ANALYSIS_JOB = "app.workers.jobs.analyse_case"
ANALYSIS_TIMEOUT_SECONDS = 15 * 60

# FR-08.1: the only allowed moves. Any step can fail; Completed and Failed are final.
NEXT_STATUSES: dict[CaseStatus, frozenset[CaseStatus]] = {
    CaseStatus.UPLOADED: frozenset({CaseStatus.VALIDATING, CaseStatus.FAILED}),
    CaseStatus.VALIDATING: frozenset({CaseStatus.QUEUED, CaseStatus.FAILED}),
    CaseStatus.QUEUED: frozenset({CaseStatus.PREPROCESSING, CaseStatus.FAILED}),
    CaseStatus.PREPROCESSING: frozenset({CaseStatus.ANALYSING, CaseStatus.FAILED}),
    CaseStatus.ANALYSING: frozenset({CaseStatus.COMPLETED, CaseStatus.FAILED}),
    CaseStatus.COMPLETED: frozenset(),
    CaseStatus.FAILED: frozenset(),
}

UNEXPECTED_FAILURE = "The scan could not be processed because of an unexpected error."
QUEUE_UNAVAILABLE = (
    "The analysis queue is not available, so the scan could not be queued. "
    "Please upload it again later."
)
INTERRUPTED = "The upload was interrupted before the scan was checked. Please upload it again."


class InvalidTransition(Exception):
    pass


def set_status(db: Session, case: Case, status: CaseStatus, message: str | None = None) -> None:
    """Move the case to `status`, add the timeline entry and, when the case ends, notify the
    uploading doctor (FR-08.2). The caller commits."""
    current = CaseStatus(case.status)
    if status not in NEXT_STATUSES[current]:
        raise InvalidTransition(f"{current} -> {status}")
    case.status = status.value
    db.add(CaseEvent(case_id=case.id, status=status.value, message=message))
    if status == CaseStatus.FAILED:
        case.failure_reason = message
    if status in FINAL_STATUSES:
        case.completed_at = utcnow()
        notifications.notify_case_finished(db, case)


def fail(db: Session, case: Case, reason: str) -> None:
    """Fail the case unless it already ended, and commit."""
    if not case.is_final:
        set_status(db, case, CaseStatus.FAILED, reason)
        db.commit()


def get_or_404(db: Session, case_id: uuid.UUID) -> Case:
    case = db.get(Case, case_id)
    if case is None:
        raise NotFound("Case not found.")
    return case


def create_case(db: Session, actor: User, patient: Patient, staged: StagedUpload) -> Case:
    """Record the upload (status Uploaded) and audit it (FR-09.2)."""
    case = Case(
        patient_id=patient.id,
        uploaded_by_id=actor.id,
        status=CaseStatus.UPLOADED.value,
        upload_kind=staged.upload.kind,
        upload_files=staged.files,
        upload_bytes=staged.total_bytes,
    )
    db.add(case)
    db.flush()
    db.add(CaseEvent(case_id=case.id, status=CaseStatus.UPLOADED.value))
    audit.record(
        db,
        AuditAction.SCAN_UPLOADED,
        actor=actor,
        target_type="case",
        target_id=str(case.id),
        details={
            "kind": staged.upload.kind,
            "files": staged.files,
            "size_mb": round(staged.total_bytes / 1024**2, 1),
        },
    )
    db.commit()
    return case


def process_upload(db: Session, case: Case, staged: StagedUpload) -> Case:
    """Validating → Queued, or → Failed with the reason. Runs while the upload request is
    open; the caller deletes the staged (identifiable) files afterwards in any case."""
    set_status(db, case, CaseStatus.VALIDATING)
    db.commit()
    dicom_dir = storage.case_dicom_dir(case.id)
    try:
        scan = intake(staged.upload, dicom_dir)
    except ScanRejected as rejected:
        storage.remove_paths([storage.case_dir(case.id)])
        fail(db, case, rejected.reason)
        return case
    except Exception as error:  # noqa: BLE001 - any bug must still end the case cleanly
        # The exception type only: messages could quote file contents (NFR-3).
        logger.error("Scan intake failed for case %s: %s", case.id, type(error).__name__)
        storage.remove_paths([storage.case_dir(case.id)])
        db.rollback()
        fail(db, case, UNEXPECTED_FAILURE)
        return case

    case.series_found = scan.series_found
    case.series_number = scan.series_number
    case.num_slices = scan.num_slices
    case.slice_spacing_mm = scan.slice_spacing_mm
    case.study_date = scan.study_date
    case.slice_thickness_mm = scan.slice_thickness_mm
    if scan.pixel_spacing_mm:
        case.pixel_spacing_row_mm, case.pixel_spacing_col_mm = scan.pixel_spacing_mm
    case.rows = scan.rows
    case.columns = scan.columns
    case.manufacturer = scan.manufacturer
    case.manufacturer_model = scan.model
    case.convolution_kernel = scan.kernel
    set_status(db, case, CaseStatus.QUEUED, scan.series_description)
    db.commit()
    enqueue_analysis(db, case)
    return case


def enqueue_analysis(db: Session, case: Case) -> None:
    """FR-04.6: queue the analysis job; if the queue is down the case fails clearly."""
    try:
        get_analysis_queue().enqueue(
            ANALYSIS_JOB,
            str(case.id),
            job_timeout=ANALYSIS_TIMEOUT_SECONDS,
            result_ttl=24 * 3600,
            failure_ttl=7 * 24 * 3600,
        )
    except Exception as error:  # noqa: BLE001 - Redis down, timeouts, …
        logger.error("Could not queue case %s: %s", case.id, type(error).__name__)
        db.refresh(case)
        if case.status == CaseStatus.QUEUED:
            fail(db, case, QUEUE_UNAVAILABLE)


def recover_interrupted_uploads(db: Session) -> int:
    """At start-up: uploads that were being checked when the API stopped can never finish.
    Fail them and clear the staging area (identifiable files must not linger)."""
    stuck = db.scalars(
        select(Case).where(Case.status.in_([CaseStatus.UPLOADED, CaseStatus.VALIDATING]))
    ).all()
    for case in stuck:
        storage.remove_paths([storage.case_dir(case.id)])
        fail(db, case, INTERRUPTED)
    root = storage.staging_root()
    if root.exists():
        storage.remove_paths(list(root.iterdir()))
    return len(stuck)


# --- Queries for the dashboard and patient pages ------------------------------------------


def cases_for_patient(db: Session, patient: Patient) -> list[Case]:
    return list(
        db.scalars(
            select(Case)
            .where(Case.patient_id == patient.id)
            .order_by(Case.created_at.desc(), Case.id)
        )
    )


def case_dirs_for_patient(db: Session, patient: Patient) -> list[Path]:
    ids = db.scalars(select(Case.id).where(Case.patient_id == patient.id)).all()
    return [storage.case_dir(case_id) for case_id in ids]


def recent_cases(db: Session, limit: int) -> list[tuple[Case, Patient]]:
    rows = db.execute(
        select(Case, Patient)
        .join(Patient, Patient.id == Case.patient_id)
        .order_by(Case.created_at.desc(), Case.id)
        .limit(limit)
    ).all()
    return [(row[0], row[1]) for row in rows]


def counts(db: Session) -> dict[str, int]:
    """FR-02.1 case counts."""

    def count(*conditions) -> int:
        return db.scalar(select(func.count()).select_from(Case).where(*conditions)) or 0

    since = utcnow() - timedelta(days=7)
    return {
        "scans_last_7_days": count(Case.created_at >= since),
        "cases_in_progress": count(Case.status.in_([s.value for s in IN_PROGRESS_STATUSES])),
        "completed_cases": count(Case.status == CaseStatus.COMPLETED),
        "failed_cases": count(Case.status == CaseStatus.FAILED),
    }
