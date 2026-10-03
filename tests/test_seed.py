"""`make seed` creates one demo doctor account (Phase 2)."""

from sqlalchemy import select

from app.models import AuditAction, Role, User
from app.seed import main
from tests.conftest import audit_entries, set_env


def test_seed_creates_the_demo_doctor_once(monkeypatch, db, capsys):
    set_env(monkeypatch, DEMO_DOCTOR_PASSWORD="Demo-doctor1")

    assert main() == 0
    assert "Created demo doctor doctor@cavinet.local" in capsys.readouterr().out
    db.expire_all()
    doctor = db.scalar(select(User).where(User.email == "doctor@cavinet.local"))
    assert doctor.role == Role.DOCTOR and doctor.must_change_password is False
    assert audit_entries(db, AuditAction.USER_CREATED)[0].details["source"] == "make seed"

    assert main() == 0
    assert "already exists" in capsys.readouterr().out
    assert len(list(db.scalars(select(User)))) == 1


def test_seed_needs_a_password(db, capsys):
    assert main() == 1
    assert "DEMO_DOCTOR_PASSWORD" in capsys.readouterr().out


def test_seed_refuses_a_weak_password(monkeypatch, db, capsys):
    set_env(monkeypatch, DEMO_DOCTOR_PASSWORD="weak")
    assert main() == 1
    assert "Could not create the demo doctor" in capsys.readouterr().out
