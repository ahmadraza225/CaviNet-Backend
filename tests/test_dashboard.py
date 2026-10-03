"""M-02 Doctor dashboard: FR-02.1 and FR-02.2. Doctor role only (section 9.3)."""

from tests.test_patients import create_ok


def test_fr02_1_stats_start_at_zero(client, doctor_headers):
    response = client.get("/api/dashboard/stats", headers=doctor_headers)
    assert response.status_code == 200
    assert response.json() == {
        "total_patients": 0,
        "scans_last_7_days": 0,
        "cases_in_progress": 0,
        "completed_cases": 0,
        "failed_cases": 0,
    }


def test_fr02_1_total_patients_follows_creates_and_deletes(client, doctor_headers):
    first = create_ok(client, doctor_headers, mr_number="MR-1")
    create_ok(client, doctor_headers, mr_number="MR-2")
    create_ok(client, doctor_headers, mr_number="MR-3")
    stats = client.get("/api/dashboard/stats", headers=doctor_headers).json()
    assert stats["total_patients"] == 3

    client.delete(f"/api/patients/{first['id']}?confirm=MR-1", headers=doctor_headers)
    stats = client.get("/api/dashboard/stats", headers=doctor_headers).json()
    assert stats["total_patients"] == 2
    # Scans and cases arrive in Phase 4.
    assert stats["scans_last_7_days"] == stats["cases_in_progress"] == 0
    assert stats["completed_cases"] == stats["failed_cases"] == 0


def test_fr02_2_recent_cases_is_empty_until_scans_exist(client, doctor_headers):
    create_ok(client, doctor_headers)
    response = client.get("/api/dashboard/recent-cases", headers=doctor_headers)
    assert response.status_code == 200
    assert response.json() == []


def test_dashboard_is_doctor_only(client, admin_headers):
    for path in ("/api/dashboard/stats", "/api/dashboard/recent-cases"):
        response = client.get(path, headers=admin_headers)
        assert response.status_code == 403
        assert response.json()["code"] == "forbidden"
