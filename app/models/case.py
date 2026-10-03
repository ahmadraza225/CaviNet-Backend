"""Cases (M-04, M-08): one uploaded CT scan of a patient and its analysis.

A case belongs to a patient (ON DELETE CASCADE, so deleting the patient removes their cases,
timelines and notifications). Its de-identified files live in DATA_DIR/cases/<case id>/.
No patient identity is stored here (NFR-3): names and MR numbers stay in the patients table.
"""

import enum
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.clock import utcnow
from app.models.base import Base, TimestampMixin


class CaseStatus(enum.StrEnum):
    """FR-08.1, in order. COMPLETED and FAILED are final."""

    UPLOADED = "uploaded"
    VALIDATING = "validating"
    QUEUED = "queued"
    PREPROCESSING = "preprocessing"
    ANALYSING = "analysing"
    COMPLETED = "completed"
    FAILED = "failed"


FINAL_STATUSES = frozenset({CaseStatus.COMPLETED, CaseStatus.FAILED})
IN_PROGRESS_STATUSES = frozenset(CaseStatus) - FINAL_STATUSES

_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in CaseStatus)


class Case(TimestampMixin, Base):
    __tablename__ = "cases"
    __table_args__ = (CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_valid"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("patients.id", ondelete="CASCADE"), index=True, nullable=False
    )
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)

    # What was uploaded (FR-04.1): "zip" or "dcm", number of files and total bytes.
    upload_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    upload_files: Mapped[int] = mapped_column(Integer, nullable=False)
    upload_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Series selection (FR-04.3): how many image series were found and which one was used.
    series_found: Mapped[int | None] = mapped_column(Integer)
    series_number: Mapped[int | None] = mapped_column(Integer)

    # Scan metadata (FR-04.5), read from the selected series.
    study_date: Mapped[date | None] = mapped_column(Date)
    num_slices: Mapped[int | None] = mapped_column(Integer)
    slice_thickness_mm: Mapped[float | None] = mapped_column(Float)
    slice_spacing_mm: Mapped[float | None] = mapped_column(Float)
    pixel_spacing_row_mm: Mapped[float | None] = mapped_column(Float)
    pixel_spacing_col_mm: Mapped[float | None] = mapped_column(Float)
    rows: Mapped[int | None] = mapped_column(Integer)
    columns: Mapped[int | None] = mapped_column(Integer)
    manufacturer: Mapped[str | None] = mapped_column(String(64))
    manufacturer_model: Mapped[str | None] = mapped_column(String(64))
    convolution_kernel: Mapped[str | None] = mapped_column(String(64))

    # Analysis result. Phase 4 stores the stub analyser's placeholder (label "STUB").
    result_label: Mapped[str | None] = mapped_column(String(16))
    result_is_stub: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    analyser: Mapped[str | None] = mapped_column(String(64))
    result_details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    events: Mapped[list["CaseEvent"]] = relationship(
        back_populates="case",
        order_by="CaseEvent.id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES


class CaseEvent(Base):
    """One step of the status timeline (FR-08.1)."""

    __tablename__ = "case_events"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cases.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Plain-language note shown on the timeline (e.g. the failure reason or series choice).
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    case: Mapped[Case] = relationship(back_populates="events")
