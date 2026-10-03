"""In-app notifications (FR-08.2, FR-08.3).

Only the kind and the case are stored; the text naming the patient is built when the
notification is read, so patient identity stays in the patients table (NFR-3).
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.clock import utcnow
from app.models.base import Base


class NotificationKind(enum.StrEnum):
    CASE_COMPLETED = "case_completed"
    CASE_FAILED = "case_failed"


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("cases.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
