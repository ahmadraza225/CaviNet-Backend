"""FastAPI application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.api.routes import admin, auth, cases, dashboard, health, notifications, patients
from app.core.config import get_settings
from app.core.database import dispose_engine, get_sessionmaker
from app.core.logging import install_log_filters
from app.core.redis import close_redis
from app.core.request_context import RequestContextMiddleware
from app.core.security import ensure_secret_key
from app.services.cases import recover_interrupted_uploads
from app.services.errors import ServiceError
from app.services.users import ensure_initial_admin

logger = logging.getLogger(__name__)

# Endpoints anyone may call without signing in. Every other endpoint checks the role.
PUBLIC_PATHS = frozenset(
    {"/api/health", "/api/auth/login", "/api/auth/refresh", "/api/auth/logout"}
)


def _bootstrap_admin() -> None:
    try:
        with get_sessionmaker()() as db:
            ensure_initial_admin(db, get_settings())
    except SQLAlchemyError:
        logger.exception("Could not check or create the initial admin account")


def _recover_uploads() -> None:
    try:
        with get_sessionmaker()() as db:
            recovered = recover_interrupted_uploads(db)
        if recovered:
            logger.warning("Marked %d interrupted upload(s) as failed", recovered)
    except SQLAlchemyError:
        logger.exception("Could not check for interrupted uploads")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    ensure_secret_key()
    _bootstrap_admin()
    _recover_uploads()
    yield
    dispose_engine()
    close_redis()


async def _service_error_handler(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ServiceError)
    return JSONResponse(
        status_code=error.status_code,
        content={"detail": error.detail, "code": error.code},
        headers=error.headers,
    )


def create_app() -> FastAPI:
    install_log_filters()
    app = FastAPI(
        title="CaviNet API",
        version=__version__,
        description="Decision support: pulmonary TB vs NTM lung disease on chest CT.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(ServiceError, _service_error_handler)
    app.include_router(health.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(admin.router, prefix="/api")
    app.include_router(patients.router, prefix="/api")
    app.include_router(dashboard.router, prefix="/api")
    app.include_router(cases.router, prefix="/api")
    app.include_router(notifications.router, prefix="/api")
    return app


app = create_app()
