"""Doctor dashboard (M-02, FR-02.1 and FR-02.2). Doctor role only (section 9.3)."""

from fastapi import APIRouter, Depends

from app.api.deps import DbSession, DoctorUser, require_doctor
from app.schemas.dashboard import DashboardStats, RecentCase
from app.services import dashboard as dashboard_service

router = APIRouter(prefix="/dashboard", tags=["dashboard"], dependencies=[Depends(require_doctor)])


@router.get("/stats", response_model=DashboardStats)
def get_stats(_: DoctorUser, db: DbSession) -> DashboardStats:
    return dashboard_service.stats(db)


@router.get("/recent-cases", response_model=list[RecentCase])
def get_recent_cases(_: DoctorUser, db: DbSession) -> list[RecentCase]:
    return dashboard_service.recent_cases(db)
