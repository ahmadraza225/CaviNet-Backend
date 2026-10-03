"""GET /api/health: reports whether the API, database and Redis are working."""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

from app import __version__
from app.core.database import check_database
from app.core.redis import check_redis

router = APIRouter(tags=["health"])

ComponentStatus = Literal["ok", "error"]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    database: ComponentStatus
    redis: ComponentStatus


def get_database_check() -> Callable[[], bool]:
    return check_database


def get_redis_check() -> Callable[[], bool]:
    return check_redis


@router.get("/health", response_model=HealthResponse)
def health(
    response: Response,
    database_check: Annotated[Callable[[], bool], Depends(get_database_check)],
    redis_check: Annotated[Callable[[], bool], Depends(get_redis_check)],
) -> HealthResponse:
    database_ok = database_check()
    redis_ok = redis_check()
    healthy = database_ok and redis_ok
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if healthy else "degraded",
        version=__version__,
        database="ok" if database_ok else "error",
        redis="ok" if redis_ok else "error",
    )
