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


def test_role_matrix_for_admin_endpoints_matches_section_9_3():
    admin_endpoints = [e for e in PROTECTED if e[1].startswith("/api/admin/")]
    assert admin_endpoints, "expected admin endpoints"
    assert all(roles == {Role.ADMIN} for _, _, roles, _ in admin_endpoints)


def test_role_matrix_for_doctor_endpoints_matches_section_9_3():
    """Patients, uploads, cases, dashboard and notifications are for doctors only: admins
    deliberately have no access to patient data."""
    doctor_endpoints = [
        e
        for e in PROTECTED
        if e[1].startswith(("/api/patients", "/api/dashboard", "/api/cases", "/api/notifications"))
    ]
    assert len(doctor_endpoints) == 15
    assert all(roles == {Role.DOCTOR} for _, _, roles, _ in doctor_endpoints)


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
