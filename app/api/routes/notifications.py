"""Notifications for the signed-in doctor (FR-08.2, FR-08.3). Doctor role only (section 9.3)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import DbSession, DoctorUser, require_doctor
from app.core.clock import as_utc
from app.schemas.notifications import MarkedRead, NotificationOut, NotificationPage, UnreadCount
from app.services import notifications as notifications_service

router = APIRouter(
    prefix="/notifications", tags=["notifications"], dependencies=[Depends(require_doctor)]
)


@router.get("", response_model=NotificationPage)
def list_notifications(
    doctor: DoctorUser,
    db: DbSession,
    unread_only: bool = False,
    page: Annotated[int, Query(ge=1)] = 1,
) -> NotificationPage:
    rows, total = notifications_service.list_for_user(
        db, doctor, unread_only=unread_only, page=page
    )
    return NotificationPage(
        items=[
            NotificationOut(
                id=notification.id,
                kind=notification.kind,
                message=notifications_service.message_for(notification.kind, patient),
                case_id=notification.case_id,
                created_at=as_utc(notification.created_at),
                read=notification.read_at is not None,
            )
            for notification, patient in rows
        ],
        total=total,
        page=page,
        page_size=notifications_service.PAGE_SIZE,
    )


@router.get("/unread-count", response_model=UnreadCount)
def unread_count(doctor: DoctorUser, db: DbSession) -> UnreadCount:
    """Polled by the bell icon every 10 seconds (FR-08.2)."""
    return UnreadCount(count=notifications_service.unread_count(db, doctor))


@router.post("/read-all", response_model=MarkedRead)
def mark_all_read(doctor: DoctorUser, db: DbSession) -> MarkedRead:
    return MarkedRead(marked=notifications_service.mark_all_read(db, doctor))


@router.post("/{notification_id}/read", response_model=MarkedRead)
def mark_read(notification_id: int, doctor: DoctorUser, db: DbSession) -> MarkedRead:
    notifications_service.mark_read(db, doctor, notification_id)
    return MarkedRead(marked=1)
