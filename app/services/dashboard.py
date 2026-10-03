"""Doctor dashboard figures (M-02, FR-02.1 and FR-02.2)."""

from sqlalchemy.orm import Session

from app.core.clock import as_utc
from app.schemas.dashboard import DashboardStats, RecentCase
from app.services import cases, patients

RECENT_CASES_LIMIT = 10  # FR-02.2


def stats(db: Session) -> DashboardStats:
    """FR-02.1."""
    return DashboardStats(total_patients=patients.count_patients(db), **cases.counts(db))


def recent_cases(db: Session, limit: int = RECENT_CASES_LIMIT) -> list[RecentCase]:
    """FR-02.2: newest first, at most `limit`."""
    return [
        RecentCase(
            case_id=case.id,
            patient_id=patient.id,
            patient_name=patient.full_name,
            uploaded_at=as_utc(case.created_at),
            status=case.status,
            result=case.result_label,
            result_is_demo=case.model_is_demo,
        )
        for case, patient in cases.recent_cases(db, limit)
    ]
