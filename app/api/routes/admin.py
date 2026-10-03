"""Administration: users (FR-09.1) and the audit log (FR-09.3). Admin role only."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import AdminUser, DbSession, require_admin
from app.models import AuditAction
from app.schemas.audit import AuditLogOut, AuditLogPage
from app.schemas.users import PasswordReset, UserCreate, UserOut, UserUpdate
from app.services import audit as audit_service
from app.services import users as users_service
from app.services.errors import InvalidInput

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/users", response_model=list[UserOut])
def list_users(_: AdminUser, db: DbSession) -> list[UserOut]:
    return [UserOut.from_user(user) for user in users_service.list_users(db)]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, admin: AdminUser, db: DbSession) -> UserOut:
    user = users_service.create_user(
        db,
        admin,
        email=body.email,
        full_name=body.full_name,
        role=body.role,
        temporary_password=body.temporary_password,
    )
    return UserOut.from_user(user)


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(user_id: uuid.UUID, body: UserUpdate, admin: AdminUser, db: DbSession) -> UserOut:
    user = users_service.get_or_404(db, user_id)
    updated = users_service.update_user(db, admin, user, full_name=body.full_name, role=body.role)
    return UserOut.from_user(updated)


@router.post("/users/{user_id}/deactivate", response_model=UserOut)
def deactivate_user(user_id: uuid.UUID, admin: AdminUser, db: DbSession) -> UserOut:
    user = users_service.get_or_404(db, user_id)
    return UserOut.from_user(users_service.deactivate_user(db, admin, user))


@router.post("/users/{user_id}/reactivate", response_model=UserOut)
def reactivate_user(user_id: uuid.UUID, admin: AdminUser, db: DbSession) -> UserOut:
    user = users_service.get_or_404(db, user_id)
    return UserOut.from_user(users_service.reactivate_user(db, admin, user))


@router.post("/users/{user_id}/reset-password", response_model=UserOut)
def reset_password(
    user_id: uuid.UUID, body: PasswordReset, admin: AdminUser, db: DbSession
) -> UserOut:
    user = users_service.get_or_404(db, user_id)
    return UserOut.from_user(users_service.reset_password(db, admin, user, body.temporary_password))


@router.get("/audit-logs", response_model=AuditLogPage)
def list_audit_logs(
    _: AdminUser,
    db: DbSession,
    user_id: uuid.UUID | None = None,
    action: AuditAction | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AuditLogPage:
    if date_from and date_to and date_from > date_to:
        raise InvalidInput("'From' date must be on or before the 'to' date.", code="bad_range")
    items, total = audit_service.list_entries(
        db,
        user_id=user_id,
        action=action.value if action else None,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )
    return AuditLogPage(
        items=[AuditLogOut.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/audit-logs/actions", response_model=list[str])
def list_audit_actions(_: AdminUser) -> list[str]:
    return [action.value for action in AuditAction]
