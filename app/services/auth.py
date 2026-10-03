"""Login, lockout, sessions (refresh tokens), logout and password change (M-01)."""

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import as_utc, utcnow
from app.core.config import get_settings
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    create_access_token,
    hash_password,
    hash_token,
    new_refresh_token,
    password_needs_rehash,
    verify_password,
)
from app.models import AuditAction, RefreshToken, User
from app.services import audit
from app.services.errors import Forbidden, InvalidInput, Locked, Unauthorized
from app.services.users import check_password_policy, get_by_email, revoke_all_refresh_tokens

INVALID_CREDENTIALS = "Incorrect email or password."


@dataclass
class AuthSession:
    user: User
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_expires_at: datetime
    refresh_token_id: uuid.UUID


def _lock_message(locked_until: datetime, now: datetime) -> str:
    minutes = max(1, math.ceil((as_utc(locked_until) - now).total_seconds() / 60))
    return f"Too many failed sign-in attempts. Try again in {minutes} minute(s)."


def authenticate(db: Session, email: str, password: str) -> User:
    settings = get_settings()
    now = utcnow()
    user = get_by_email(db, email)

    if user is None:
        verify_password(DUMMY_PASSWORD_HASH, password)  # uniform timing
        audit.record(
            db, AuditAction.LOGIN_FAILURE, actor_email=email, details={"reason": "unknown_email"}
        )
        db.commit()
        raise Unauthorized(INVALID_CREDENTIALS, code="invalid_credentials")

    if user.is_locked(now):
        audit.record(db, AuditAction.LOGIN_FAILURE, actor=user, details={"reason": "locked"})
        db.commit()
        raise Locked(_lock_message(user.locked_until, now))

    if not verify_password(user.password_hash, password):
        user.failed_login_attempts += 1
        audit.record(
            db,
            AuditAction.LOGIN_FAILURE,
            actor=user,
            details={"reason": "wrong_password", "attempt": user.failed_login_attempts},
        )
        if user.failed_login_attempts >= settings.lockout_threshold:
            user.failed_login_attempts = 0
            user.locked_until = now + timedelta(minutes=settings.lockout_minutes)
            audit.record(
                db,
                AuditAction.ACCOUNT_LOCKED,
                actor=user,
                details={"minutes": settings.lockout_minutes},
            )
            db.commit()
            raise Locked(_lock_message(user.locked_until, now))
        db.commit()
        raise Unauthorized(INVALID_CREDENTIALS, code="invalid_credentials")

    if not user.is_active:
        audit.record(db, AuditAction.LOGIN_FAILURE, actor=user, details={"reason": "inactive"})
        db.commit()
        raise Forbidden(
            "This account has been deactivated. Contact an administrator.",
            code="account_inactive",
        )

    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    audit.record(db, AuditAction.LOGIN_SUCCESS, actor=user)
    db.commit()
    return user


def _issue(db: Session, user: User, refresh_expires_at: datetime) -> AuthSession:
    raw, token_hash = new_refresh_token()
    token = RefreshToken(
        id=uuid.uuid4(), user_id=user.id, token_hash=token_hash, expires_at=refresh_expires_at
    )
    db.add(token)
    access_token, expires_in = create_access_token(user.id)
    return AuthSession(user, access_token, expires_in, raw, refresh_expires_at, token.id)


def start_session(db: Session, user: User) -> AuthSession:
    """FR-01.2: 30-minute access token + 8-hour refresh token."""
    expires = utcnow() + timedelta(hours=get_settings().refresh_token_hours)
    session = _issue(db, user, expires)
    db.commit()
    return session


def refresh_session(db: Session, raw_token: str | None) -> AuthSession:
    """Rotate the refresh token. The session keeps its original 8-hour expiry."""
    if not raw_token:
        raise Unauthorized("Your session has ended. Please sign in again.", code="no_session")
    now = utcnow()
    stored = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_token)))
    if stored is None:
        raise Unauthorized("Your session has ended. Please sign in again.", code="no_session")
    user = db.get(User, stored.user_id)
    if stored.revoked_at is not None:
        # Reuse of a *rotated* token suggests it was stolen: end every session of this user.
        # (Tokens revoked by sign-out or a password change are simply refused.)
        if stored.replaced_by_id is not None and user is not None:
            revoke_all_refresh_tokens(db, user)
            db.commit()
        raise Unauthorized("Your session has ended. Please sign in again.", code="no_session")
    if as_utc(stored.expires_at) <= now or user is None or not user.is_active:
        raise Unauthorized("Your session has ended. Please sign in again.", code="no_session")
    stored.revoked_at = now
    session = _issue(db, user, as_utc(stored.expires_at))
    stored.replaced_by_id = session.refresh_token_id
    db.commit()
    return session


def logout(db: Session, raw_token: str | None) -> None:
    """FR-01.2: revoke the refresh token from the cookie."""
    if not raw_token:
        return
    stored = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_token)))
    if stored is None or stored.revoked_at is not None:
        return
    stored.revoked_at = utcnow()
    user = db.get(User, stored.user_id)
    audit.record(db, AuditAction.LOGOUT, actor=user)
    db.commit()


def change_password(
    db: Session, user: User, current_password: str, new_password: str
) -> AuthSession:
    """Change own password (also completes a forced change). Ends all other sessions."""
    if not verify_password(user.password_hash, current_password):
        raise InvalidInput("Current password is incorrect.", code="wrong_current_password")
    check_password_policy(new_password)
    if verify_password(user.password_hash, new_password):
        raise InvalidInput(
            "New password must be different from the current one.", code="password_reused"
        )
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    user.password_changed_at = utcnow()
    revoke_all_refresh_tokens(db, user)
    audit.record(db, AuditAction.PASSWORD_CHANGED, actor=user)
    db.flush()
    return start_session(db, user)
