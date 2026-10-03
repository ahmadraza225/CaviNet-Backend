"""Audit log service (FR-09.2, FR-09.3). Records who did what and when, never medical content."""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.request_context import current_client_ip
from app.models import AuditAction, AuditLog, User


def record(
    db: Session,
    action: AuditAction,
    *,
    actor: User | None = None,
    actor_email: str | None = None,
    target: User | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Add an audit entry to the session; the caller commits with its own changes."""
    if target is not None:
        target_type, target_id = "user", str(target.id)
        details = {"target_email": target.email, **(details or {})}
    entry = AuditLog(
        action=action.value,
        user_id=actor.id if actor else None,
        actor_email=actor.email if actor else actor_email,
        target_type=target_type,
        target_id=target_id,
        details=details or None,
        ip_address=current_client_ip(),
    )
    db.add(entry)
    return entry


def list_entries(
    db: Session,
    *,
    user_id: uuid.UUID | None = None,
    action: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[AuditLog], int]:
    """Newest first. `user_id` matches entries by or about that user. Dates are inclusive
    whole days in UTC."""
    conditions = []
    if user_id is not None:
        # Entries the user performed, or that were performed on their account.
        conditions.append(
            or_(
                AuditLog.user_id == user_id,
                and_(AuditLog.target_type == "user", AuditLog.target_id == str(user_id)),
            )
        )
    if action:
        conditions.append(AuditLog.action == action)
    if date_from is not None:
        conditions.append(AuditLog.created_at >= datetime.combine(date_from, time.min, UTC))
    if date_to is not None:
        end = datetime.combine(date_to + timedelta(days=1), time.min, UTC)
        conditions.append(AuditLog.created_at < end)

    total = db.scalar(select(func.count()).select_from(AuditLog).where(*conditions)) or 0
    items = db.scalars(
        select(AuditLog)
        .where(*conditions)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(items), total
