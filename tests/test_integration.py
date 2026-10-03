"""Integration tests against real PostgreSQL and Redis (run in CI service containers).

Set TEST_DATABASE_URL and TEST_REDIS_URL to run them locally.
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.database import get_engine, get_sessionmaker
from app.main import create_app
from app.models import Role
from app.services import patients as patients_service
from app.services.users import build_user
from tests.conftest import (
    PASSWORD,
    bearer,
    create_patient,
    login,
    run_queued_jobs,
    scan_zip,
    set_env,
    token_for,
    upload,
)

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")
BACKEND_DIR = Path(__file__).resolve().parents[1]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (DATABASE_URL and REDIS_URL),
        reason="TEST_DATABASE_URL and TEST_REDIS_URL not set",
    ),
]


def alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


@pytest.fixture
def live_env(monkeypatch):
    set_env(monkeypatch, DATABASE_URL=DATABASE_URL, REDIS_URL=REDIS_URL)


@pytest.fixture
def clean_db(live_env):
    command.upgrade(alembic_config(), "head")
    with get_sessionmaker()() as session:
        tables = ("notifications", "case_events", "cases", "patients", "audit_logs")
        for table in (*tables, "refresh_tokens", "users"):
            session.execute(text(f"DELETE FROM {table}"))
        session.commit()


def test_health_ok_against_real_services(live_env):
    response = TestClient(create_app()).get("/api/health")
    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "ok"


def test_migrations_upgrade_downgrade_and_match_the_models(live_env):
    config = alembic_config()
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    # Fails if the models and the migrations disagree (autogenerate would produce changes).
    command.check(config)


def test_account_flow_on_postgres(clean_db, monkeypatch):
    set_env(monkeypatch, ADMIN_EMAIL="admin@hospital.org", ADMIN_PASSWORD="First-admin123")
    with TestClient(create_app()) as client:
        first = login(client, "admin@hospital.org", "First-admin123")
        assert first.status_code == 200 and first.json()["user"]["must_change_password"]
        changed = client.post(
            "/api/auth/change-password",
            json={"current_password": "First-admin123", "new_password": "Admin-own-pass1"},
            headers=bearer(first.json()["access_token"]),
        )
        assert changed.status_code == 200
        admin_headers = bearer(changed.json()["access_token"])

        created = client.post(
            "/api/admin/users",
            json={
                "email": "dr@hospital.org",
                "full_name": "Dr Pg",
                "role": "doctor",
                "temporary_password": "Temp-pass123",
            },
            headers=admin_headers,
        )
        assert created.status_code == 201
        doctor_id = created.json()["id"]

        for _ in range(4):
            assert login(client, "dr@hospital.org", "Wrong-passw0rd").status_code == 401
        assert login(client, "dr@hospital.org", "Wrong-passw0rd").status_code == 423

        reset = client.post(
            f"/api/admin/users/{doctor_id}/reset-password",
            json={"temporary_password": "Reset-pass123"},
            headers=admin_headers,
        )
        assert reset.status_code == 200 and reset.json()["is_locked"] is False
        doctor = login(client, "dr@hospital.org", "Reset-pass123")
        assert doctor.status_code == 200 and doctor.json()["user"]["must_change_password"]
        assert client.post("/api/auth/refresh").status_code == 200
        assert client.post("/api/auth/logout").status_code == 204
        assert client.post("/api/auth/refresh").status_code == 401

        audit = client.get(
            f"/api/admin/audit-logs?user_id={doctor_id}&action=account_locked",
            headers=admin_headers,
        )
        assert audit.status_code == 200 and audit.json()["total"] == 1
        everything = client.get("/api/admin/audit-logs?page_size=100", headers=admin_headers)
        actions = {item["action"] for item in everything.json()["items"]}
        assert {
            "user_created",
            "password_changed",
            "login_failure",
            "password_reset",
            "logout",
        } <= actions


def test_patient_flow_on_postgres(clean_db, monkeypatch, tmp_path):
    """FR-03.1 to FR-03.3 and the delete cascade on the production database engine."""
    set_env(monkeypatch, DATA_DIR=str(tmp_path / "data"))
    with get_sessionmaker()() as session:
        session.add(
            build_user(
                "dr@hospital.org", "Dr Pg", Role.DOCTOR, PASSWORD, must_change_password=False
            )
        )
        session.commit()
    with TestClient(create_app()) as client:
        headers = token_for(client, "dr@hospital.org")
        people = [("zainab Ali", "B-2"), ("Ahmed Raza", "C-3"), ("Maryam Noor", "A-1")]
        ids = {}
        for name, mr in people:
            response = client.post(
                "/api/patients",
                json={
                    "full_name": name,
                    "mr_number": mr.lower(),
                    "date_of_birth": "1980-05-05",
                    "sex": "female",
                },
                headers=headers,
            )
            assert response.status_code == 201, response.json()
            ids[mr] = response.json()["id"]

        def names(**params):
            body = client.get("/api/patients", params=params, headers=headers).json()
            return [item["full_name"] for item in body["items"]]

        assert names(sort="full_name", order="asc") == ["Ahmed Raza", "Maryam Noor", "zainab Ali"]
        assert names(q="ZAINAB") == ["zainab Ali"]
        assert names(q="c-3") == ["Ahmed Raza"]
        assert names(q="%") == []

        duplicate = {
            "full_name": "X Y",
            "mr_number": "a-1",
            "date_of_birth": "1980-05-05",
            "sex": "male",
        }
        response = client.post("/api/patients", json=duplicate, headers=headers)
        assert response.status_code == 409
        # Simultaneous requests: the unique index on PostgreSQL has the final say.
        monkeypatch.setattr(patients_service, "_mr_number_in_use", lambda *a, **k: False)
        response = client.post("/api/patients", json=duplicate, headers=headers)
        assert response.status_code == 409 and response.json()["code"] == "mr_number_taken"

        engine = get_engine()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE phase4_scan_stand_in (id serial PRIMARY KEY, "
                    "patient_id uuid NOT NULL REFERENCES patients(id) ON DELETE CASCADE)"
                )
            )
            connection.execute(
                text("INSERT INTO phase4_scan_stand_in (patient_id) VALUES (:p)"),
                {"p": ids["A-1"]},
            )
        try:
            response = client.delete(
                f"/api/patients/{ids['A-1']}", params={"confirm": "A-1"}, headers=headers
            )
            assert response.status_code == 204
            with engine.connect() as connection:
                remaining = connection.execute(text("SELECT count(*) FROM phase4_scan_stand_in"))
                assert remaining.scalar() == 0
        finally:
            with engine.begin() as connection:
                connection.execute(text("DROP TABLE phase4_scan_stand_in"))

        stats = client.get("/api/dashboard/stats", headers=headers).json()
        assert stats["total_patients"] == 2


def test_upload_and_analysis_on_postgres_and_redis(clean_db, monkeypatch, tmp_path):
    """Phase 5 acceptance: synthetic scan → upload (PostgreSQL) → job via real Redis → RQ
    worker runs the AI pipeline → stored result with the demo flag."""
    import uuid as uuid_module

    from redis import Redis

    from app.services import cases as cases_service
    from app.workers.queue import get_analysis_queue

    set_env(
        monkeypatch, DATA_DIR=str(tmp_path / "data"), ANALYSIS_QUEUE=f"it-{uuid_module.uuid4().hex}"
    )
    redis = Redis.from_url(REDIS_URL)
    queue = get_analysis_queue(connection=redis)
    monkeypatch.setattr(cases_service, "get_analysis_queue", lambda: queue)
    with get_sessionmaker()() as session:
        session.add(
            build_user(
                "dr@hospital.org", "Dr Pg", Role.DOCTOR, PASSWORD, must_change_password=False
            )
        )
        session.commit()
    try:
        with TestClient(create_app()) as client:
            headers = token_for(client, "dr@hospital.org")
            patient = create_patient(client, headers)
            response = upload(client, headers, patient["id"], [("scan.zip", scan_zip(60))])
            assert response.status_code == 201, response.json()
            case = response.json()
            assert case["status"] == "queued"
            assert queue.count == 1

            run_queued_jobs(queue)

            body = client.get(f"/api/cases/{case['id']}", headers=headers).json()
            assert body["status"] == "completed"
            assert [s["status"] for s in body["timeline"]] == [
                "uploaded",
                "validating",
                "queued",
                "preprocessing",
                "analysing",
                "completed",
            ]
            assert body["result"]["label"] in {"TB", "NTM"} and body["result"]["is_demo"]
            result = client.get(f"/api/cases/{case['id']}/result", headers=headers).json()
            assert result["demo_banner"] == "DEMO MODEL: NOT FOR CLINICAL USE"
            assert result["preview_count"] == 48
            count = client.get("/api/notifications/unread-count", headers=headers).json()
            assert count == {"count": 1}
            stats = client.get("/api/dashboard/stats", headers=headers).json()
            assert (stats["scans_last_7_days"], stats["completed_cases"]) == (1, 1)
            scans = client.get(f"/api/patients/{patient['id']}", headers=headers).json()["scans"]
            assert [s["status"] for s in scans] == ["completed"] and scans[0]["result_is_demo"]

            # Deleting the patient cascades on PostgreSQL too.
            deleted = client.delete(
                f"/api/patients/{patient['id']}?confirm=MR-1001", headers=headers
            )
            assert deleted.status_code == 204
            with get_engine().connect() as connection:
                for table in ("cases", "case_events", "notifications"):
                    assert connection.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0
    finally:
        queue.delete(delete_jobs=True)
        redis.close()
