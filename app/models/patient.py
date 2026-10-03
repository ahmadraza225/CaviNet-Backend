"""Patients (M-03). The only table that holds patient identity (NFR-3).

Scans and cases (Phase 4) reference patients.id with ON DELETE CASCADE, so deleting a
patient removes their scans, results and reports from the database; the patient service
removes their files (FR-03.2).
"""

import enum
import uuid
from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Sex(enum.StrEnum):
    MALE = "male"
    FEMALE = "female"


class Patient(TimestampMixin, Base):
    __tablename__ = "patients"
    __table_args__ = (CheckConstraint("sex IN ('male', 'female')", name="sex_valid"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Stored trimmed and upper-cased, so uniqueness ignores case.
    mr_number: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    date_of_birth: Mapped[date] = mapped_column(Date, nullable=False)
    sex: Mapped[str] = mapped_column(String(8), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
