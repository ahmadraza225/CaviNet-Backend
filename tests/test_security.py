"""FR-01.1 (Argon2id, password policy) and FR-01.2 (access-token lifetime) building blocks."""

from datetime import timedelta

import jwt
import pytest

from app.core import clock
from app.core.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    ensure_secret_key,
    hash_password,
    hash_token,
    new_refresh_token,
    password_policy_error,
    verify_password,
)


def test_fr01_1_passwords_are_hashed_with_argon2id():
    password_hash = hash_password("Passw0rd123")
    assert password_hash.startswith("$argon2id$")
    assert "Passw0rd123" not in password_hash
    assert verify_password(password_hash, "Passw0rd123")
    assert not verify_password(password_hash, "passw0rd123")


def test_verify_password_rejects_malformed_hash():
    assert verify_password("not-a-hash", "anything") is False


@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("Ab1", "at least 10 characters"),
        ("abcdefghij", "at least one digit"),
        ("1234567890", "at least one letter"),
        ("a1" * 65, "at most 128 characters"),
    ],
)
def test_fr01_1_password_policy_rejects(password, message):
    assert message in password_policy_error(password)


@pytest.mark.parametrize("password", ["Passw0rd123", "abcdefghi1", "1234567890x"])
def test_fr01_1_password_policy_accepts(password):
    assert password_policy_error(password) is None


def test_access_token_round_trip():
    import uuid

    user_id = uuid.uuid4()
    token, lifetime = create_access_token(user_id)
    assert lifetime == 30 * 60
    decoded_id, issued_at = decode_access_token(token)
    assert decoded_id == user_id
    assert abs((clock.utcnow() - issued_at).total_seconds()) < 5


def test_fr01_2_access_token_expires_after_30_minutes():
    import uuid

    token, _ = create_access_token(uuid.uuid4())
    clock.advance(timedelta(minutes=29, seconds=50))
    decode_access_token(token)
    clock.advance(timedelta(seconds=11))
    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


def test_tampered_or_foreign_tokens_are_rejected():
    import uuid

    token, _ = create_access_token(uuid.uuid4())
    with pytest.raises(InvalidTokenError):
        decode_access_token(token[:-2] + ("AA" if not token.endswith("AA") else "BB"))
    foreign = jwt.encode(
        {"sub": str(uuid.uuid4()), "type": "access", "iat": 1, "exp": 9e9},
        "another-secret-key-that-is-long-enough!!",
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(foreign)
    with pytest.raises(InvalidTokenError):
        decode_access_token("not.a.token")


def test_token_of_wrong_type_is_rejected():
    import uuid

    from app.core.config import get_settings

    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "type": "refresh", "iat": 1, "exp": 9e9},
        get_settings().secret_key,
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


def test_refresh_tokens_are_random_and_stored_hashed():
    raw_a, hash_a = new_refresh_token()
    raw_b, _ = new_refresh_token()
    assert raw_a != raw_b
    assert hash_a == hash_token(raw_a) and raw_a not in hash_a and len(hash_a) == 64


def test_missing_or_short_secret_key_fails_fast(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("SECRET_KEY", "too-short")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        ensure_secret_key()
