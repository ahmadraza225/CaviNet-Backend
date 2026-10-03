"""In-app notifications for the uploading doctor (FR-08.2, FR-08.3)."""

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.models import Case, CaseStatus, Notification, NotificationKind, Patient, User
from app.services.errors import NotFound

PAGE_SIZE = 20


def notify_case_finished(db: Session, case: Case) -> Notification | None:
    """Tell the doctor who uploaded the case that it completed or failed."""
    if case.uploaded_by_id is None:
        return None
    kind = (
        NotificationKind.CASE_COMPLETED
        if case.status == CaseStatus.COMPLETED
        else NotificationKind.CASE_FAILED
    )
    notification = Notification(user_id=case.uploaded_by_id, case_id=case.id, kind=kind.value)
    db.add(notification)
    return notification


def message_for(kind: str, patient: Patient | None) -> str:
    """Built when read, so the patient's name is never stored outside the patients table."""
    who = f"{patient.full_name} ({patient.mr_number})" if patient else "a deleted patient"
    if kind == NotificationKind.CASE_COMPLETED:
        return f"Scan analysis completed for {who}."
    return f"Scan analysis failed for {who}. Open the case to see why."


def _own(user: User):
    return Notification.user_id == user.id


def list_for_user(
    db: Session, user: User, *, unread_only: bool = False, page: int = 1
) -> tuple[list[tuple[Notification, Patient | None]], int]:
    conditions = [_own(user)]
    if unread_only:
        conditions.append(Notification.read_at.is_(None))
    total = db.scalar(select(func.count()).select_from(Notification).where(*conditions)) or 0
    rows = db.execute(
        select(Notification, Patient)
        .outerjoin(Case, Case.id == Notification.case_id)
        .outerjoin(Patient, Patient.id == Case.patient_id)
        .where(*conditions)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()
    return [(row[0], row[1]) for row in rows], total


def unread_count(db: Session, user: User) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(_own(user), Notification.read_at.is_(None))
        )
        or 0
    )


def mark_read(db: Session, user: User, notification_id: int) -> Notification:
    notification = db.get(Notification, notification_id)
    if notification is None or notification.user_id != user.id:
        # Another user's notification is reported as missing, not forbidden.
        raise NotFound("Notification not found.")
    if notification.read_at is None:
        notification.read_at = utcnow()
        db.commit()
    return notification


def mark_all_read(db: Session, user: User) -> int:
    result = db.execute(
        update(Notification)
        .where(_own(user), Notification.read_at.is_(None))
        .values(read_at=utcnow())
    )
    db.commit()
    return result.rowcount or 0
