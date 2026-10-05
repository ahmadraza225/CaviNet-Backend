"""NFR-2/NFR-3: API responses carry patient data and are never cached."""

from fastapi.responses import Response
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import create_patient


def test_api_responses_are_not_cached(client, doctor_headers):
    patient = create_patient(client, doctor_headers)
    for response in (
        client.get("/api/health"),
        client.get("/api/patients", headers=doctor_headers),
        client.get(f"/api/patients/{patient['id']}", headers=doctor_headers),
        client.get("/api/patients"),  # 401 responses too
        client.get("/api/no-such-endpoint"),
    ):
        assert response.headers["cache-control"] == "no-store", response.request.url


def test_endpoints_that_choose_their_caching_keep_it():
    app = create_app()

    @app.get("/api/test-cached")
    def cached() -> Response:
        return Response("x", headers={"Cache-Control": "private, max-age=60"})

    with TestClient(app) as test_client:
        response = test_client.get("/api/test-cached")
    assert response.headers.get_list("cache-control") == ["private, max-age=60"]
