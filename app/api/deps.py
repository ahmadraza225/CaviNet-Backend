"""Authentication and role checks shared by every endpoint (FR-01.3).

Every non-public endpoint depends on `require_roles(...)` (or `current_user` for the few
account endpoints that must work during a forced password change). The role-coverage test
walks all routes and fails if one is missing these checks.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.clock import as_utc
from app.core.database import get_db
from app.core.security import InvalidTokenError, decode_access_token
from app.models import Role, User
from app.services.errors import Forbidden, Unauthorized

_bearer = HTTPBearer(auto_error=False)

DbSession = Annotated[Session, Depends(get_db)]


def current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """Any signed-in, active user, including one who must still change their password."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise Unauthorized()
    try:
        user_id, issued_at = decode_access_token(credentials.credentials)
    except InvalidTokenError:
        raise Unauthorized("Your session has expired. Please sign in again.") from None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise Unauthorized("Your session has expired. Please sign in again.")
    changed = as_utc(user.password_changed_at)
    if round(issued_at.timestamp(), 3) < round(changed.timestamp(), 3):
        # The token predates a password change or an admin reset.
        raise Unauthorized("Your session has expired. Please sign in again.")
    return user


# Marks endpoints that any signed-in user may call even before changing a temporary password.
current_user.allowed_roles = frozenset(Role)  # type: ignore[attr-defined]
current_user.allows_pending_password_change = True  # type: ignore[attr-defined]


def require_roles(*roles: Role) -> Callable[..., User]:
    """Dependency factory: the signed-in user must have one of `roles` and must not have a
    pending forced password change (FR-01.6)."""

    def dependency(user: Annotated[User, Depends(current_user)]) -> User:
        if user.must_change_password:
            raise Forbidden(
                "You must change your temporary password first.",
                code="password_change_required",
            )
        if user.role not in roles:
            raise Forbidden("You do not have permission to do this.", code="forbidden")
        return user

    dependency.allowed_roles = frozenset(roles)  # type: ignore[attr-defined]
    dependency.allows_pending_password_change = False  # type: ignore[attr-defined]
    return dependency


require_admin = require_roles(Role.ADMIN)
require_doctor = require_roles(Role.DOCTOR)
require_any_role = require_roles(Role.DOCTOR, Role.ADMIN)

CurrentUser = Annotated[User, Depends(current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
DoctorUser = Annotated[User, Depends(require_doctor)]
