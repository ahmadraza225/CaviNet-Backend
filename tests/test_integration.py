"""Integration tests against real PostgreSQL and Redis (run in CI service containers).

Set TEST_DATABASE_URL and TEST_REDIS_URL to run them locally.
"""

import os

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.main import create_app

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (DATABASE_URL and REDIS_URL),
        reason="TEST_DATABASE_URL and TEST_REDIS_URL not set",
    ),
]


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)


def test_health_ok_against_real_services(live_env):
    response = TestClient(create_app()).get("/api/health")
    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "ok"


def test_alembic_upgrade_and_downgrade(live_env):
    config = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    config.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "alembic")
    )
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
