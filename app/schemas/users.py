import re
import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.core.clock import as_utc, utcnow
from app.models import Role, User

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: str) -> str:
    return value.strip().lower()


def validate_email(value: str) -> str:
    value = normalize_email(value)
    if len(value) > 254 or not _EMAIL_RE.match(value):
        raise ValueError("Enter a valid email address.")
    return value


def validate_full_name(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ValueError("Full name is required.")
    return value


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    is_active: bool
    must_change_password: bool
    is_locked: bool
    last_login_at: datetime | None
    created_at: datetime

    @classmethod
    def from_user(cls, user: User) -> "UserOut":
        return cls(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=Role(user.role),
            is_active=user.is_active,
            must_change_password=user.must_change_password,
            is_locked=user.is_locked(utcnow()),
            last_login_at=as_utc(user.last_login_at),
            created_at=as_utc(user.created_at),
        )


class UserCreate(BaseModel):
    email: str = Field(max_length=254)
    full_name: str = Field(max_length=120)
    role: Role
    temporary_password: str = Field(max_length=128)

    _email = field_validator("email")(validate_email)
    _name = field_validator("full_name")(validate_full_name)


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=120)
    role: Role | None = None

    @field_validator("full_name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return None if value is None else validate_full_name(value)


class PasswordReset(BaseModel):
    temporary_password: str = Field(max_length=128)
