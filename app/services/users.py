"""User administration (FR-09.1) and account bootstrap (FR-01.6, FR-01.7)."""

import logging
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.security import hash_password, password_policy_error
from app.models import AuditAction, RefreshToken, Role, User
from app.schemas.users import normalize_email
from app.services import audit
from app.services.errors import Conflict, InvalidInput, NotFound

logger = logging.getLogger(__name__)


def get_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == normalize_email(email)))


def get_or_404(db: Session, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise NotFound("User not found.")
    return user


def list_users(db: Session) -> list[User]:
    return list(db.scalars(select(User).order_by(User.role, User.full_name)).all())


def check_password_policy(password: str) -> None:
    error = password_policy_error(password)
    if error:
        raise InvalidInput(error, code="weak_password")


def revoke_all_refresh_tokens(db: Session, user: User) -> None:
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


def build_user(
    email: str, full_name: str, role: Role, password: str, *, must_change_password: bool
) -> User:
    now = utcnow()
    return User(
        email=normalize_email(email),
        full_name=full_name,
        role=role.value,
        password_hash=hash_password(password),
        is_active=True,
        must_change_password=must_change_password,
        failed_login_attempts=0,
        password_changed_at=now,
    )


def create_user(
    db: Session, actor: User, *, email: str, full_name: str, role: Role, temporary_password: str
) -> User:
    check_password_policy(temporary_password)
    if get_by_email(db, email) is not None:
        raise Conflict("A user with this email already exists.", code="email_taken")
    user = build_user(email, full_name, role, temporary_password, must_change_password=True)
    db.add(user)
    db.flush()
    audit.record(db, AuditAction.USER_CREATED, actor=actor, target=user, details={"role": role})
    db.commit()
    return user


def _active_admin_count(db: Session) -> int:
    return (
        db.scalar(
            select(func.count()).select_from(User).where(User.role == Role.ADMIN, User.is_active)
        )
        or 0
    )


def _ensure_not_last_admin(db: Session, user: User, verb: str) -> None:
    if user.role == Role.ADMIN and user.is_active and _active_admin_count(db) <= 1:
        raise Conflict(f"Cannot {verb} the last active administrator.", code="last_admin")


def update_user(
    db: Session, actor: User, user: User, *, full_name: str | None, role: Role | None
) -> User:
    changes: dict[str, str] = {}
    if role is not None and role.value != user.role:
        if user.id == actor.id:
            raise Conflict("You cannot change your own role.", code="self_action")
        if role != Role.ADMIN:
            _ensure_not_last_admin(db, user, "demote")
        changes["role"] = f"{user.role} -> {role.value}"
        user.role = role.value
    if full_name is not None and full_name != user.full_name:
        changes["full_name"] = "changed"
        user.full_name = full_name
    if changes:
        audit.record(db, AuditAction.USER_UPDATED, actor=actor, target=user, details=changes)
        db.commit()
    return user


def deactivate_user(db: Session, actor: User, user: User) -> User:
    if user.id == actor.id:
        raise Conflict("You cannot deactivate your own account.", code="self_action")
    if user.is_active:
        _ensure_not_last_admin(db, user, "deactivate")
        user.is_active = False
        revoke_all_refresh_tokens(db, user)
        audit.record(db, AuditAction.USER_DEACTIVATED, actor=actor, target=user)
        db.commit()
    return user


def reactivate_user(db: Session, actor: User, user: User) -> User:
    if not user.is_active:
        user.is_active = True
        user.failed_login_attempts = 0
        user.locked_until = None
        audit.record(db, AuditAction.USER_REACTIVATED, actor=actor, target=user)
        db.commit()
    return user


def reset_password(db: Session, actor: User, user: User, temporary_password: str) -> User:
    """FR-01.6: set a temporary password; the user must change it at next login."""
    check_password_policy(temporary_password)
    user.password_hash = hash_password(temporary_password)
    user.must_change_password = True
    user.password_changed_at = utcnow()  # invalidates existing access tokens
    user.failed_login_attempts = 0
    user.locked_until = None
    revoke_all_refresh_tokens(db, user)
    audit.record(db, AuditAction.PASSWORD_RESET, actor=actor, target=user)
    db.commit()
    return user


def ensure_initial_admin(db: Session, settings: Settings) -> User | None:
    """FR-01.7: on first start, create the admin from ADMIN_EMAIL / ADMIN_PASSWORD.

    Does nothing once any admin exists. The admin must change the password at first login.
    """
    if db.scalar(select(User.id).where(User.role == Role.ADMIN).limit(1)) is not None:
        return None
    if not settings.admin_email or not settings.admin_password:
        logger.warning("No admin account exists and ADMIN_EMAIL/ADMIN_PASSWORD are not set.")
        return None
    error = password_policy_error(settings.admin_password)
    if error:
        logger.error("ADMIN_PASSWORD does not meet the password policy: %s", error)
        return None
    admin = build_user(
        settings.admin_email,
        settings.admin_full_name,
        Role.ADMIN,
        settings.admin_password,
        must_change_password=True,
    )
    db.add(admin)
    db.flush()
    audit.record(
        db,
        AuditAction.USER_CREATED,
        actor_email="system",
        target=admin,
        details={"role": Role.ADMIN, "source": "initial admin"},
    )
    db.commit()
    logger.info("Created the initial admin account %s", admin.email)
    return admin


def ensure_demo_doctor(db: Session, settings: Settings) -> tuple[User, bool]:
    """Create the demo doctor used for demonstrations (`make seed`). Returns (user, created)."""
    existing = get_by_email(db, settings.demo_doctor_email)
    if existing is not None:
        return existing, False
    check_password_policy(settings.demo_doctor_password)
    doctor = build_user(
        settings.demo_doctor_email,
        settings.demo_doctor_full_name,
        Role.DOCTOR,
        settings.demo_doctor_password,
        must_change_password=False,
    )
    db.add(doctor)
    db.flush()
    audit.record(
        db,
        AuditAction.USER_CREATED,
        actor_email="system",
        target=doctor,
        details={"role": Role.DOCTOR, "source": "make seed"},
    )
    db.commit()
    return doctor, True
