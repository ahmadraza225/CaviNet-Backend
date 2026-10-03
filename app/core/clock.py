"""Single source of the current time. Tests shift it with `advance()` (e.g. to expire a
lockout or a token) instead of sleeping."""

from datetime import UTC, datetime, timedelta

_offset = timedelta(0)


def utcnow() -> datetime:
    return datetime.now(UTC) + _offset


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes (stored as UTC); make every datetime timezone-aware."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def advance(delta: timedelta) -> None:
    """Move the application clock forward (tests only)."""
    global _offset
    _offset += delta


def reset() -> None:
    global _offset
    _offset = timedelta(0)
