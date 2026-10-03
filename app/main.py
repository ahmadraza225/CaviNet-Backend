"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.routes import health
from app.core.database import dispose_engine
from app.core.redis import close_redis


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    dispose_engine()
    close_redis()


def create_app() -> FastAPI:
    app = FastAPI(
        title="CaviNet API",
        version=__version__,
        description="Decision support: pulmonary TB vs NTM lung disease on chest CT.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.include_router(health.router, prefix="/api")
    return app


app = create_app()
