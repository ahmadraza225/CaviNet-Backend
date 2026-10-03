"""Doctor dashboard figures (M-02). Case counts and recent cases stay zero/empty until
Phase 4 adds scans and cases."""

from sqlalchemy.orm import Session

from app.schemas.dashboard import DashboardStats, RecentCase
from app.services import patients

RECENT_CASES_LIMIT = 10  # FR-02.2


def stats(db: Session) -> DashboardStats:
    """FR-02.1."""
    return DashboardStats(
        total_patients=patients.count_patients(db),
        scans_last_7_days=0,
        cases_in_progress=0,
        completed_cases=0,
        failed_cases=0,
    )


def recent_cases(db: Session, limit: int = RECENT_CASES_LIMIT) -> list[RecentCase]:
    """FR-02.2: newest first, at most `limit`."""
    return []
