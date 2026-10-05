"""FR-01.3: every API endpoint checks the role server-side.

This test discovers endpoints automatically, so it also covers endpoints added in later
phases: a new endpoint without a role check (and not listed in PUBLIC_PATHS) fails here.
"""

import re
import uuid

import pytest
from fastapi.routing import APIRoute, iter_route_contexts

from app.main import PUBLIC_PATHS, create_app
from app.models import Role
from tests.conftest import token_for


def _roles_of(dependant) -> tuple[frozenset[Role] | None, bool]:
    """(allowed roles, allows pending password change) from the dependency tree."""
    found: list = []

    def walk(node):
        for dependency in node.dependencies:
            if hasattr(dependency.call, "allowed_roles"):
                found.append(dependency.call)
            walk(dependency)

    walk(dependant)
    if not found:
        return None, False
    roles = frozenset.intersection(*(frozenset(call.allowed_roles) for call in found))
    allows_pending = all(call.allows_pending_password_change for call in found)
    return roles, allows_pending


def _endpoints():
    app = create_app()
    for context in iter_route_contexts(app.routes):
        if isinstance(context.route, APIRoute):
            for method in sorted(context.methods):
                roles, allows_pending = _roles_of(context.route.dependant)
                yield method, context.path, roles, allows_pending


ENDPOINTS = list(_endpoints())
PROTECTED = [e for e in ENDPOINTS if e[1] not in PUBLIC_PATHS]


def _url(path: str) -> str:
    return re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)


def test_fr01_3_every_endpoint_is_public_by_design_or_role_checked():
    unchecked = [f"{m} {p}" for m, p, roles, _ in ENDPOINTS if p not in PUBLIC_PATHS and not roles]
    assert unchecked == [], f"Endpoints without a role check: {unchecked}"
    assert {p for _, p, _, _ in ENDPOINTS} >= PUBLIC_PATHS, "PUBLIC_PATHS lists a missing route"


DOCTOR, ADMIN = frozenset({Role.DOCTOR}), frozenset({Role.ADMIN})
BOTH = DOCTOR | ADMIN

# Section 9.3, endpoint by endpoint. Admins deliberately have no access to patient data;
# doctors have no access to user management, the audit log or the admin model page.
ROLE_MATRIX = {
    # Log in, change own password: both roles.
    "GET /api/auth/me": BOTH,
    "POST /api/auth/change-password": BOTH,
    # Create / deactivate users, assign roles, reset passwords: admin.
    "GET /api/admin/users": ADMIN,
    "POST /api/admin/users": ADMIN,
    "PATCH /api/admin/users/{user_id}": ADMIN,
    "POST /api/admin/users/{user_id}/deactivate": ADMIN,
    "POST /api/admin/users/{user_id}/reactivate": ADMIN,
    "POST /api/admin/users/{user_id}/reset-password": ADMIN,
    # View audit log and model information: admin.
    "GET /api/admin/audit-logs": ADMIN,
    "GET /api/admin/audit-logs/actions": ADMIN,
    "GET /api/admin/model": ADMIN,
    # Create / view / edit / delete patients: doctor.
    "GET /api/patients": DOCTOR,
    "POST /api/patients": DOCTOR,
    "GET /api/patients/{patient_id}": DOCTOR,
    "PATCH /api/patients/{patient_id}": DOCTOR,
    "DELETE /api/patients/{patient_id}": DOCTOR,
    "GET /api/dashboard/stats": DOCTOR,
    "GET /api/dashboard/recent-cases": DOCTOR,
    # Upload CT scans, view results, download reports: doctor.
    "POST /api/patients/{patient_id}/cases": DOCTOR,
    "GET /api/cases/{case_id}": DOCTOR,
    "GET /api/cases/{case_id}/result": DOCTOR,
    "GET /api/cases/{case_id}/report": DOCTOR,
    "GET /api/cases/{case_id}/previews/{index}": DOCTOR,
    # Receive case notifications: doctor.
    "GET /api/notifications": DOCTOR,
    "GET /api/notifications/unread-count": DOCTOR,
    "POST /api/notifications/read-all": DOCTOR,
    "POST /api/notifications/{notification_id}/read": DOCTOR,
    # Whether a model is installed and the DEMO banner (FR-05.6): shown to everyone.
    "GET /api/model/status": BOTH,
}


def test_fr01_3_role_matrix_matches_section_9_3():
    """Every protected endpoint, and the roles allowed to call it, exactly as section 9.3."""
    discovered = {f"{m} {p}": roles for m, p, roles, _ in PROTECTED}
    assert discovered.keys() == ROLE_MATRIX.keys(), (
        "Endpoints added or removed: add each new endpoint to ROLE_MATRIX with its roles. "
        f"Unlisted: {sorted(discovered.keys() - ROLE_MATRIX.keys())}; "
        f"missing: {sorted(ROLE_MATRIX.keys() - discovered.keys())}"
    )
    wrong = {key: roles for key, roles in discovered.items() if roles != ROLE_MATRIX[key]}
    assert wrong == {}, f"Roles differ from section 9.3: {wrong}"


@pytest.mark.parametrize(
    ("method", "path", "roles", "allows_pending"),
    PROTECTED,
    ids=[f"{m} {p}" for m, p, _, _ in PROTECTED],
)
def test_fr01_3_allowed_roles_pass_the_role_check(
    client, make_user, method, path, roles, allows_pending
):
    """The other half of the matrix: each allowed role gets past the role check (the
    request may still fail for other reasons, e.g. 404 for the made-up record id)."""
    for role in roles:
        user = make_user(f"ok-{role}-{uuid.uuid4().hex[:6]}@example.org", role)
        response = client.request(
            method, _url(path), json={}, headers=token_for(client, user.email)
        )
        assert response.status_code not in (401, 403), (role, response.json())


@pytest.mark.parametrize(
    ("method", "path", "roles", "allows_pending"),
    PROTECTED,
    ids=[f"{m} {p}" for m, p, _, _ in PROTECTED],
)
def test_fr01_3_protected_endpoint_without_token_is_401(
    client, method, path, roles, allows_pending
):
    response = client.request(method, _url(path), json={})
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "roles", "allows_pending"),
    PROTECTED,
    ids=[f"{m} {p}" for m, p, _, _ in PROTECTED],
)
def test_fr01_3_wrong_role_gets_403(client, make_user, method, path, roles, allows_pending):
    wrong_roles = [role for role in Role if role not in roles]
    if not wrong_roles:
        pytest.skip("endpoint is open to every role")
    for role in wrong_roles:
        user = make_user(f"{role}-{uuid.uuid4().hex[:6]}@example.org", role)
        response = client.request(
            method, _url(path), json={}, headers=token_for(client, user.email)
        )
        assert response.status_code == 403, (role, response.json())
        assert response.json()["code"] == "forbidden"


@pytest.mark.parametrize(
    ("method", "path", "roles", "allows_pending"),
    PROTECTED,
    ids=[f"{m} {p}" for m, p, _, _ in PROTECTED],
)
def test_fr01_6_pending_password_change_blocks_protected_endpoints(
    client, make_user, method, path, roles, allows_pending
):
    if allows_pending:
        pytest.skip("account endpoint usable during a forced password change")
    role = next(iter(roles))
    user = make_user(f"pending-{uuid.uuid4().hex[:6]}@example.org", role, must_change_password=True)
    response = client.request(method, _url(path), json={}, headers=token_for(client, user.email))
    assert response.status_code == 403
    assert response.json()["code"] == "password_change_required"
