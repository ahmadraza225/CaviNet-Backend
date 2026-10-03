"""Seed demo data: python -m app.seed (run by `make seed`). Safe to run repeatedly."""

from app.core.config import get_settings
from app.core.database import get_sessionmaker
from app.services.errors import ServiceError
from app.services.users import ensure_demo_doctor


def main() -> int:
    settings = get_settings()
    if not settings.demo_doctor_password:
        print("DEMO_DOCTOR_PASSWORD is not set in .env; cannot create the demo doctor.")
        return 1
    try:
        with get_sessionmaker()() as db:
            doctor, created = ensure_demo_doctor(db, settings)
    except ServiceError as error:
        print(f"Could not create the demo doctor: {error.detail}")
        return 1
    if created:
        print(f"Created demo doctor {doctor.email} (password: DEMO_DOCTOR_PASSWORD in .env).")
    else:
        print(f"Demo doctor {doctor.email} already exists; nothing to do.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
