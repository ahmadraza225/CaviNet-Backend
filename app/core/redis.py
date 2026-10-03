"""Redis connection and the health check."""

import logging
from functools import lru_cache

from redis import Redis

from app.core.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def get_redis() -> Redis:
    settings = get_settings()
    timeout = settings.health_check_timeout_seconds
    return Redis.from_url(
        settings.redis_url, socket_connect_timeout=timeout, socket_timeout=timeout
    )


def check_redis() -> bool:
    """Return True if Redis answers PING."""
    try:
        return bool(get_redis().ping())
    except Exception:  # noqa: BLE001 - any failure means "not healthy"
        logger.warning("Redis health check failed", exc_info=True)
        return False


def close_redis() -> None:
    """Close the Redis connection pool and forget the client (app shutdown, tests)."""
    if get_redis.cache_info().currsize:
        get_redis().close()
    get_redis.cache_clear()
