"""Password hashing (Argon2id), password policy and token helpers (FR-01.1, FR-01.2)."""

import hashlib
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.clock import utcnow
from app.core.config import MIN_SECRET_KEY_LENGTH, get_settings

# argon2-cffi's PasswordHasher uses Argon2id with OWASP-recommended parameters.
_hasher = PasswordHasher()

PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128
PASSWORD_RULES = (
    f"at least {PASSWORD_MIN_LENGTH} characters, including at least one letter and one digit"
)

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"


class InvalidTokenError(Exception):
    """The access token is missing, malformed, expired or not an access token."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


# Verifying against this hash for unknown emails keeps login timing uniform.
DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))


def password_policy_error(password: str) -> str | None:
    """Return a human-readable reason if the password breaks the policy, else None."""
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters long."
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"Password must be at most {PASSWORD_MAX_LENGTH} characters long."
    if not re.search(r"[A-Za-z]", password):
        return "Password must contain at least one letter."
    if not re.search(r"\d", password):
        return "Password must contain at least one digit."
    return None


def _secret_key() -> str:
    key = get_settings().secret_key
    if len(key) < MIN_SECRET_KEY_LENGTH:
        raise RuntimeError(
            f"SECRET_KEY must be set to at least {MIN_SECRET_KEY_LENGTH} characters "
            "(make up generates one in .env)."
        )
    return key


def ensure_secret_key() -> None:
    """Fail fast at startup when SECRET_KEY is missing or too short."""
    _secret_key()


def create_access_token(user_id: uuid.UUID) -> tuple[str, int]:
    """Return (token, lifetime in seconds)."""
    lifetime = timedelta(minutes=get_settings().access_token_minutes)
    now = utcnow()
    claims = {
        "sub": str(user_id),
        "type": ACCESS_TOKEN_TYPE,
        # Millisecond precision so a password change invalidates tokens issued just before it.
        "iat": round(now.timestamp(), 3),
        "exp": int((now + lifetime).timestamp()),
        "jti": secrets.token_hex(8),
    }
    token = jwt.encode(claims, _secret_key(), algorithm=JWT_ALGORITHM)
    return token, int(lifetime.total_seconds())


def decode_access_token(token: str) -> tuple[uuid.UUID, datetime]:
    """Return (user id, issued-at) or raise InvalidTokenError."""
    try:
        claims = jwt.decode(
            token,
            _secret_key(),
            algorithms=[JWT_ALGORITHM],
            # exp/iat are checked below against the clock module (not the system clock),
            # so tests can move time.
            options={
                "require": ["sub", "exp", "iat", "type"],
                "verify_exp": False,
                "verify_iat": False,
            },
        )
    except jwt.PyJWTError as error:
        raise InvalidTokenError(str(error)) from error
    if claims["exp"] <= int(utcnow().timestamp()):
        raise InvalidTokenError("token expired")
    if claims.get("type") != ACCESS_TOKEN_TYPE:
        raise InvalidTokenError("not an access token")
    try:
        user_id = uuid.UUID(str(claims["sub"]))
    except ValueError as error:
        raise InvalidTokenError("invalid subject") from error
    try:
        issued_at = datetime.fromtimestamp(float(claims["iat"]), tz=UTC)
    except (TypeError, ValueError) as error:
        raise InvalidTokenError("invalid issued-at") from error
    return user_id, issued_at


def new_refresh_token() -> tuple[str, str]:
    """Return (raw token for the cookie, SHA-256 hash for the database)."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
