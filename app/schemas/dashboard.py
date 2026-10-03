"""Doctor dashboard (M-02, FR-02.1 and FR-02.2)."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class DashboardStats(BaseModel):
    total_patients: int
    scans_last_7_days: int
    cases_in_progress: int
    completed_cases: int
    failed_cases: int


class RecentCase(BaseModel):
    """One of the most recent cases."""

    case_id: uuid.UUID
    patient_id: uuid.UUID
    patient_name: str
    uploaded_at: datetime
    status: str
    result: str | None = None
    result_is_demo: bool = False
