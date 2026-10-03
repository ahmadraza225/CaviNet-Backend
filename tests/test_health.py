from app import __version__


def test_health_ok_when_database_and_redis_respond(make_client):
    response = make_client().get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "database": "ok",
        "redis": "ok",
    }


def test_health_degraded_when_database_down(make_client):
    response = make_client(database_ok=False).get("/api/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] == "error"
    assert body["redis"] == "ok"


def test_health_degraded_when_redis_down(make_client):
    response = make_client(redis_ok=False).get("/api/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] == "ok"
    assert body["redis"] == "error"


def test_openapi_served_under_api_prefix(make_client):
    response = make_client().get("/api/openapi.json")
    assert response.status_code == 200
    assert "/api/health" in response.json()["paths"]
