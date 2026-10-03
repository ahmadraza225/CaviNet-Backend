"""Access to the RQ job queue used for scan analysis."""

from redis import Redis
from rq import Queue

from app.core.config import get_settings
from app.core.redis import get_redis


def get_analysis_queue(connection: Redis | None = None) -> Queue:
    settings = get_settings()
    return Queue(settings.analysis_queue, connection=connection or get_redis())
